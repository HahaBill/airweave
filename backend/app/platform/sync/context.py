"""Module for sync context."""

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, schemas
from app.core import credentials
from app.core.exceptions import NotFoundException
from app.platform.auth.schemas import AuthType
from app.platform.auth.services import oauth2_service
from app.platform.destinations._base import BaseDestination
from app.platform.destinations.weaviate import WeaviateDestination
from app.platform.embedding_models._base import BaseEmbeddingModel
from app.platform.embedding_models.local_text2vec import LocalText2Vec
from app.platform.locator import resource_locator
from app.platform.sources._base import BaseSource
from app.platform.sync.pubsub import SyncProgress
from app.platform.sync.router import SyncDAGRouter


class SyncContext:
    """Context container for a sync.

    Contains all the necessary components for a sync:
    - source - the source instance
    - destination - the destination instance (still assumes single destination)
    - embedding model - the embedding model used for the sync
    - transformers - a dictionary of transformer callables
    - sync - the main sync object
    - sync job - the sync job that is created for the sync
    - dag - the DAG that is created for the sync
    - progress - the progress tracker, interfaces with PubSub
    - router - the DAG router
    - white label (optional)
    """

    source: BaseSource
    destination: BaseDestination  # still assumes single destination
    embedding_model: BaseEmbeddingModel
    transformers: dict[str, callable]
    sync: schemas.Sync
    sync_job: schemas.SyncJob
    dag: schemas.SyncDag
    progress: SyncProgress
    router: SyncDAGRouter

    white_label: Optional[schemas.WhiteLabel] = None

    def __init__(
        self,
        source: BaseSource,
        destination: BaseDestination,  # still assumes single destination
        embedding_model: BaseEmbeddingModel,
        transformers: dict[str, callable],
        sync: schemas.Sync,
        sync_job: schemas.SyncJob,
        dag: schemas.SyncDag,
        progress: SyncProgress,
        router: SyncDAGRouter,
        white_label: Optional[schemas.WhiteLabel] = None,
    ):
        """Initialize the sync context."""
        self.source = source
        self.destination = destination
        self.embedding_model = embedding_model
        self.transformers = transformers
        self.sync = sync
        self.sync_job = sync_job
        self.dag = dag
        self.progress = progress
        self.router = router
        self.white_label = white_label


class SyncContextFactory:
    """Factory for sync context."""

    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        sync: schemas.Sync,
        sync_job: schemas.SyncJob,
        dag: schemas.SyncDag,
        current_user: schemas.User,
        white_label: Optional[schemas.WhiteLabel] = None,
    ) -> SyncContext:
        """Create a sync context."""
        source = await cls._create_source_instance(db, sync, current_user)
        embedding_model = cls._get_embedding_model(sync)
        destination = await cls._create_destination_instance(sync, embedding_model)
        transformers = await cls._get_transformer_callables(db, sync)
        entity_map = await cls._get_entity_map(db, sync)

        progress = SyncProgress(sync_job.id)
        router = SyncDAGRouter(dag)

        return SyncContext(
            source=source,
            destination=destination,
            embedding_model=embedding_model,
            transformers=transformers,
            sync=sync,
            sync_job=sync_job,
            dag=dag,
            progress=progress,
            router=router,
            white_label=white_label,
        )

    @classmethod
    async def _create_source_instance(
        cls,
        db: AsyncSession,
        sync: schemas.Sync,
        current_user: schemas.User,
    ) -> BaseSource:
        """Create and configure the source instance based on authentication type."""
        source_connection = await crud.connection.get(db, sync.source_connection_id, current_user)
        if not source_connection:
            raise NotFoundException("Source connection not found")

        source_model = await crud.source.get_by_short_name(db, source_connection.short_name)
        if not source_model:
            raise NotFoundException("Source not found")

        source_class = resource_locator.get_source(source_model)

        if source_model.auth_type == AuthType.none:
            return await source_class.create()

        if source_model.auth_type in [
            AuthType.oauth2_with_refresh,
            AuthType.oauth2_with_refresh_rotating,
        ]:
            return await cls._create_oauth2_with_refresh_source(
                db, source_model, source_class, current_user, source_connection
            )

        if source_model.auth_type == AuthType.oauth2:
            return await cls._create_oauth2_source(
                db, source_class, current_user, source_connection
            )

        return await cls._create_other_auth_source(
            db, source_model, source_class, current_user, source_connection
        )

    @classmethod
    async def _create_oauth2_with_refresh_source(
        cls,
        db: AsyncSession,
        source_model: schemas.Source,
        source_class,
        current_user: schemas.User,
        source_connection: schemas.Connection,
    ) -> BaseSource:
        """Create source instance for OAuth2 with refresh token."""
        oauth2_response = await oauth2_service.refresh_access_token(
            db, source_model.short_name, current_user, source_connection.id
        )
        return await source_class.create(oauth2_response.access_token)

    @classmethod
    async def _create_oauth2_source(
        cls,
        db: AsyncSession,
        source_class,
        current_user: schemas.User,
        source_connection: schemas.Connection,
    ) -> BaseSource:
        """Create source instance for regular OAuth2."""
        if not source_connection.integration_credential_id:
            raise NotFoundException("Source connection has no integration credential")

        credential = await cls._get_integration_credential(db, source_connection, current_user)
        decrypted_credential = credentials.decrypt(credential.encrypted_credentials)
        return await source_class.create(decrypted_credential["access_token"])

    @classmethod
    async def _create_other_auth_source(
        cls,
        db: AsyncSession,
        source_model: schemas.Source,
        source_class,
        current_user: schemas.User,
        source_connection: schemas.Connection,
    ) -> BaseSource:
        """Create source instance for other authentication types."""
        if not source_connection.integration_credential_id:
            raise NotFoundException("Source connection has no integration credential")

        credential = await cls._get_integration_credential(db, source_connection, current_user)
        decrypted_credential = credentials.decrypt(credential.encrypted_credentials)

        if not source_model.auth_config_class:
            raise ValueError(f"Auth config class required for auth type {source_model.auth_type}")

        auth_config = resource_locator.get_auth_config(source_model.auth_config_class)
        source_credentials = auth_config.model_validate(decrypted_credential)
        return await source_class.create(source_credentials)

    @classmethod
    async def _get_integration_credential(
        cls,
        db: AsyncSession,
        source_connection: schemas.Connection,
        current_user: schemas.User,
    ) -> schemas.IntegrationCredential:
        """Get integration credential."""
        credential = await crud.integration_credential.get(
            db, source_connection.integration_credential_id, current_user
        )
        if not credential:
            raise NotFoundException("Source integration credential not found")
        return credential

    @classmethod
    def _get_embedding_model(cls, sync: schemas.Sync) -> BaseEmbeddingModel:
        """Get embedding model instance."""
        if not sync.embedding_model_connection_id:
            return LocalText2Vec()
        return LocalText2Vec()  # TODO: Handle other embedding models

    @classmethod
    async def _create_destination_instance(
        cls, sync: schemas.Sync, embedding_model: BaseEmbeddingModel
    ) -> BaseDestination:
        """Create destination instance."""
        if not sync.destination_connection_id:
            return await WeaviateDestination.create(sync.id, embedding_model)
        return await WeaviateDestination.create(
            sync.id, embedding_model
        )  # TODO: Handle other destinations

    @classmethod
    async def _get_transformer_callables(
        cls, db: AsyncSession, sync: schemas.Sync
    ) -> dict[str, callable]:
        """Get transformers instance."""
        transformers = {}

        transformer_functions = await crud.transformer.get_all(db)
        for transformer in transformer_functions:
            transformers[transformer.method_name] = resource_locator.get_transformer(transformer)
        return transformers

    @classmethod
    async def _get_entity_definition_map(
        cls, db: AsyncSession, sync: schemas.Sync
    ) -> dict[str, type[schemas.EntityDefinition]]:
        """Get entity definition map.

        Loops over all nodes in the DAG and returns a dictionary of entity definition ids to
        entity definitions.
        """
        entity_map = {}

        entity_definitions = await crud.entity_definition.get_all(db)

        for entity_definition in entity_definitions:
            entity_map[entity_definition.class_name] = entity_definition

        for node in sync.dag.nodes:
            entity_map[node.entity_definition_id] = node.entity_type
        return entity_map
