# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
"""The base SQL Cache implementation."""

from __future__ import annotations

import abc
import contextlib
import enum
from collections import defaultdict
from contextlib import contextmanager
from functools import cached_property
from typing import TYPE_CHECKING, Any, cast, final

import pandas as pd
import sqlalchemy
import ulid
from pandas import Index
from pydantic import BaseModel, Field
from sqlalchemy import (
    Column,
    Table,
    and_,
    create_engine,
    insert,
    null,
    select,
    text,
    update,
)

from airbyte_protocol.models import (
    AirbyteMessage,
    AirbyteRecordMessage,
    AirbyteStateMessage,
    AirbyteStateType,
    AirbyteStreamState,
    AirbyteTraceMessage,
    Type,
)

from airbyte import exceptions as exc
from airbyte._util.hashing import one_way_hash
from airbyte._util.name_normalizers import LowerCaseNormalizer
from airbyte.constants import (
    AB_EXTRACTED_AT_COLUMN,
    AB_META_COLUMN,
    AB_RAW_ID_COLUMN,
    DEBUG_MODE,
)
from airbyte.records import StreamRecordHandler
from airbyte.secrets.base import SecretString
from airbyte.shared.state_writers import StdOutStateWriter
from airbyte.strategies import WriteMethod, WriteStrategy
from airbyte.types import SQLTypeConverter


if TYPE_CHECKING:
    from collections.abc import Generator, Iterable
    from pathlib import Path

    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.engine.cursor import CursorResult
    from sqlalchemy.engine.reflection import Inspector
    from sqlalchemy.sql.base import Executable
    from sqlalchemy.sql.elements import TextClause
    from sqlalchemy.sql.type_api import TypeEngine

    from airbyte._batch_handles import BatchHandle
    from airbyte._writers.jsonl import FileWriterBase
    from airbyte.progress import ProgressTracker
    from airbyte.shared.catalog_providers import CatalogProvider
    from airbyte.shared.state_writers import StateWriterBase


class RecordDedupeMode(enum.Enum):
    """The deduplication mode to use when writing records."""

    APPEND = "append"
    REPLACE = "replace"


class SQLRuntimeError(Exception):
    """Raised when an SQL operation fails."""


class SqlConfig(BaseModel, abc.ABC):
    """Common configuration for SQL connections."""

    schema_name: str = Field(default="airbyte_raw")
    """The name of the schema to write to."""

    table_prefix: str | None = ""
    """A prefix to add to created table names."""

    @abc.abstractmethod
    def get_sql_alchemy_url(self) -> SecretString:
        """Returns a SQL Alchemy URL."""
        ...

    @abc.abstractmethod
    def get_database_name(self) -> str:
        """Return the name of the database."""
        ...

    @property
    def config_hash(self) -> str | None:
        """Return a unique one-way hash of the configuration.

        The generic implementation uses the SQL Alchemy URL, schema name, and table prefix. Some
        inputs may be redundant with the SQL Alchemy URL, but this does not hurt the hash
        uniqueness.

        In most cases, subclasses do not need to override this method.
        """
        return one_way_hash(
            SecretString(
                ":".join(
                    [
                        str(self.get_sql_alchemy_url()),
                        self.schema_name or "",
                        self.table_prefix or "",
                    ]
                )
            )
        )

    def get_create_table_extra_clauses(self) -> list[str]:
        """Return a list of clauses to append on CREATE TABLE statements."""
        return []

    def get_sql_alchemy_connect_args(self) -> dict[str, Any]:
        """Return the SQL Alchemy connect_args."""
        return {}

    def get_sql_engine(self) -> Engine:
        """Return a new SQL engine to use."""
        return create_engine(
            url=self.get_sql_alchemy_url(),
            echo=DEBUG_MODE,
            execution_options={
                "schema_translate_map": {None: self.schema_name},
            },
            future=True,
            connect_args=self.get_sql_alchemy_connect_args(),
        )

    def get_vendor_client(self) -> object:
        """Return the vendor-specific client object.

        This is used for vendor-specific operations.

        Raises `NotImplementedError` if a custom vendor client is not defined.
        """
        raise NotImplementedError(
            f"The type '{type(self).__name__}' does not define a custom client."
        )


class SqlProcessorBase(abc.ABC):
    """A base class to be used for SQL Caches."""

    type_converter_class: type[SQLTypeConverter] = SQLTypeConverter
    """The type converter class to use for converting JSON schema types to SQL types."""

    normalizer = LowerCaseNormalizer
    """The name normalizer to user for table and column name normalization."""

    file_writer_class: type[FileWriterBase]
    """The file writer class to use for writing files to the cache."""

    supports_merge_insert = False
    """True if the database supports the MERGE INTO syntax."""

    def __init__(
        self,
        *,
        sql_config: SqlConfig,
        catalog_provider: CatalogProvider,
        state_writer: StateWriterBase | None = None,
        file_writer: FileWriterBase | None = None,
        temp_dir: Path | None = None,
        temp_file_cleanup: bool,
    ) -> None:
        """Create a new SQL processor."""
        if not temp_dir and not file_writer:
            raise exc.PyAirbyteInternalError(
                message="Either `temp_dir` or `file_writer` must be provided.",
            )

        state_writer = state_writer or StdOutStateWriter()

        self._sql_config: SqlConfig = sql_config

        self._catalog_provider: CatalogProvider | None = catalog_provider
        self._state_writer: StateWriterBase | None = state_writer or StdOutStateWriter()

        self._pending_state_messages: dict[str, list[AirbyteStateMessage]] = defaultdict(list, {})
        self._finalized_state_messages: dict[
            str,
            list[AirbyteStateMessage],
        ] = defaultdict(list, {})

        self._setup()
        self.file_writer = file_writer or self.file_writer_class(
            cache_dir=cast("Path", temp_dir),
            cleanup=temp_file_cleanup,
        )
        self.type_converter = self.type_converter_class()
        self._cached_table_definitions: dict[str, sqlalchemy.Table] = {}

        self._known_schemas_list: list[str] = []
        self._ensure_schema_exists()

    @property
    def catalog_provider(
        self,
    ) -> CatalogProvider:
        """Return the catalog manager.

        Subclasses should set this property to a valid catalog manager instance if one
        is not explicitly passed to the constructor.

        Raises:
            PyAirbyteInternalError: If the catalog manager is not set.
        """
        if not self._catalog_provider:
            raise exc.PyAirbyteInternalError(
                message="Catalog manager should exist but does not.",
            )

        return self._catalog_provider

    @property
    def state_writer(
        self,
    ) -> StateWriterBase:
        """Return the state writer instance.

        Subclasses should set this property to a valid state manager instance if one
        is not explicitly passed to the constructor.

        Raises:
            PyAirbyteInternalError: If the state manager is not set.
        """
        if not self._state_writer:
            raise exc.PyAirbyteInternalError(
                message="State manager should exist but does not.",
            )

        return self._state_writer

    @final
    def process_airbyte_messages(
        self,
        messages: Iterable[AirbyteMessage],
        *,
        write_strategy: WriteStrategy = WriteStrategy.AUTO,
        progress_tracker: ProgressTracker,
    ) -> None:
        """Process a stream of Airbyte messages.

        This method assumes that the catalog is already registered with the processor.
        """
        if not isinstance(write_strategy, WriteStrategy):
            raise exc.AirbyteInternalError(
                message="Invalid `write_strategy` argument. Expected instance of WriteStrategy.",
                context={"write_strategy": write_strategy},
            )

        stream_record_handlers: dict[str, StreamRecordHandler] = {}

        # Process messages, writing to batches as we go
        for message in messages:
            if message.type is Type.RECORD:
                record_msg = cast("AirbyteRecordMessage", message.record)
                stream_name = record_msg.stream

                if stream_name not in stream_record_handlers:
                    stream_record_handlers[stream_name] = StreamRecordHandler(
                        json_schema=self.catalog_provider.get_stream_json_schema(
                            stream_name=stream_name,
                        ),
                        normalize_keys=True,
                        prune_extra_fields=True,
                    )

                self.process_record_message(
                    record_msg,
                    stream_record_handler=stream_record_handlers[stream_name],
                    progress_tracker=progress_tracker,
                )

            elif message.type is Type.STATE:
                state_msg = cast("AirbyteStateMessage", message.state)
                if state_msg.type in {AirbyteStateType.GLOBAL, AirbyteStateType.LEGACY}:
                    self._pending_state_messages[f"_{state_msg.type}"].append(state_msg)
                else:
                    stream_state = cast("AirbyteStreamState", state_msg.stream)
                    stream_name = stream_state.stream_descriptor.name
                    self._pending_state_messages[stream_name].append(state_msg)

            elif message.type is Type.TRACE:
                trace_msg: AirbyteTraceMessage = cast("AirbyteTraceMessage", message.trace)
                if trace_msg.stream_status and trace_msg.stream_status.status == "SUCCEEDED":
                    # This stream has completed successfully, so go ahead and write the data.
                    # This will also finalize any pending state messages.
                    self.write_stream_data(
                        stream_name=trace_msg.stream_status.stream_descriptor.name,
                        write_strategy=write_strategy,
                        progress_tracker=progress_tracker,
                    )

            else:
                # Ignore unexpected or unhandled message types:
                # Type.LOG, Type.CONTROL, etc.
                pass

        # We've finished processing input data.
        # Finalize all received records and state messages:
        self._write_all_stream_data(
            write_strategy=write_strategy,
            progress_tracker=progress_tracker,
        )

        self.cleanup_all()

    def _write_all_stream_data(
        self,
        write_strategy: WriteStrategy,
        progress_tracker: ProgressTracker,
    ) -> None:
        """Finalize any pending writes."""
        for stream_name in self.catalog_provider.stream_names:
            self.write_stream_data(
                stream_name,
                write_strategy=write_strategy,
                progress_tracker=progress_tracker,
            )

    def _finalize_state_messages(
        self,
        state_messages: list[AirbyteStateMessage],
    ) -> None:
        """Handle state messages by passing them to the catalog manager."""
        if state_messages:
            self.state_writer.write_state(
                state_message=state_messages[-1],
            )

    def _setup(self) -> None:  # noqa: B027  # Intentionally empty, not abstract
        """Create the database.

        By default this is a no-op but subclasses can override this method to prepare
        any necessary resources.
        """
        pass

    def _do_checkpoint(  # noqa: B027  # Intentionally empty, not abstract
        self,
        connection: Connection | None = None,
    ) -> None:
        """Checkpoint the given connection.

        If the WAL log needs to be, it will be flushed.

        For most SQL databases, this is a no-op. However, it exists so that
        subclasses can override this method to perform a checkpoint operation.
        """
        pass

    # Public interface:

    @property
    def sql_config(self) -> SqlConfig:
        """Return the SQL configuration."""
        return self._sql_config

    def get_sql_alchemy_url(self) -> SecretString:
        """Return the SQLAlchemy URL to use."""
        return self.sql_config.get_sql_alchemy_url()

    @final
    @cached_property
    def database_name(self) -> str:
        """Return the name of the database."""
        return self.sql_config.get_database_name()

    @final
    def get_sql_engine(self) -> Engine:
        """Return a new SQL engine to use."""
        return self.sql_config.get_sql_engine()

    @contextmanager
    def get_sql_connection(self) -> Generator[sqlalchemy.engine.Connection, None, None]:
        """A context manager which returns a new SQL connection for running queries.

        If the connection needs to close, it will be closed automatically.
        """
        with self.get_sql_engine().begin() as connection:
            self._init_connection_settings(connection)
            yield connection

        connection.close()
        del connection

    def get_sql_table_name(
        self,
        stream_name: str,
    ) -> str:
        """Return the name of the SQL table for the given stream."""
        table_prefix = self.sql_config.table_prefix
        return self.normalizer.normalize(
            f"{table_prefix}{stream_name}",
        )

    @final
    def get_sql_table(
        self,
        stream_name: str,
    ) -> sqlalchemy.Table:
        """Return the main table object for the stream."""
        return self._get_table_by_name(
            self.get_sql_table_name(stream_name),
        )

    # Record processing:

    def process_record_message(
        self,
        record_msg: AirbyteRecordMessage,
        stream_record_handler: StreamRecordHandler,
        progress_tracker: ProgressTracker,
    ) -> None:
        """Write a record to the cache.

        This method is called for each record message, before the batch is written.

        In most cases, the SQL processor will not perform any action, but will pass this along to to
        the file processor.
        """
        self.file_writer.process_record_message(
            record_msg,
            stream_record_handler=stream_record_handler,
            progress_tracker=progress_tracker,
        )

    # Protected members (non-public interface):

    def _init_connection_settings(self, connection: Connection) -> None:  # noqa: B027  # Intentionally empty, not abstract
        """This is called automatically whenever a new connection is created.

        By default this is a no-op. Subclasses can use this to set connection settings, such as
        timezone, case-sensitivity settings, and other session-level variables.
        """
        pass

    def _invalidate_table_cache(
        self,
        table_name: str,
    ) -> None:
        """Invalidate the the named table cache.

        This should be called whenever the table schema is known to have changed.
        """
        if table_name in self._cached_table_definitions:
            del self._cached_table_definitions[table_name]

    def _get_table_by_name(
        self,
        table_name: str,
        *,
        force_refresh: bool = False,
        shallow_okay: bool = False,
    ) -> sqlalchemy.Table:
        """Return a table object from a table name.

        If 'shallow_okay' is True, the table will be returned without requiring properties to
        be read from the database.

        To prevent unnecessary round-trips to the database, the table is cached after the first
        query. To ignore the cache and force a refresh, set 'force_refresh' to True.
        """
        if force_refresh and shallow_okay:
            raise exc.PyAirbyteInternalError(
                message="Cannot force refresh and use shallow query at the same time."
            )

        if force_refresh and table_name in self._cached_table_definitions:
            self._invalidate_table_cache(table_name)

        if table_name not in self._cached_table_definitions:
            if shallow_okay:
                # Return a shallow instance, without column declarations. Do not cache
                # the table definition in this case.
                return sqlalchemy.Table(
                    table_name,
                    sqlalchemy.MetaData(schema=self.sql_config.schema_name),
                )

            self._cached_table_definitions[table_name] = sqlalchemy.Table(
                table_name,
                sqlalchemy.MetaData(schema=self.sql_config.schema_name),
                autoload_with=self.get_sql_engine(),
            )

        return self._cached_table_definitions[table_name]

    def _ensure_schema_exists(
        self,
    ) -> None:
        schema_name = self.normalizer.normalize(self.sql_config.schema_name)
        known_schemas_list = self.normalizer.normalize_list(self._known_schemas_list)
        if known_schemas_list and schema_name in known_schemas_list:
            return  # Already exists

        schemas_list = self.normalizer.normalize_list(self._get_schemas_list())
        if schema_name in schemas_list:
            return

        sql = f"CREATE SCHEMA IF NOT EXISTS {schema_name}"

        try:
            self._execute_sql(sql)
        except Exception as ex:
            # Ignore schema exists errors.
            if "already exists" not in str(ex):
                raise

        if DEBUG_MODE:
            found_schemas = schemas_list
            assert (
                schema_name in found_schemas
            ), f"Schema {schema_name} was not created. Found: {found_schemas}"

    def _quote_identifier(self, identifier: str) -> str:
        """Return the given identifier, quoted."""
        return f'"{identifier}"'

    @final
    def _get_temp_table_name(
        self,
        stream_name: str,
        batch_id: str | None = None,  # ULID of the batch
    ) -> str:
        """Return a new (unique) temporary table name."""
        if not batch_id:
            batch_id = str(ulid.ULID())

        # Use the first 6 and last 3 characters of the ULID. This gives great uniqueness while
        # limiting the table name suffix to 10 characters, including the underscore.
        suffix = (
            f"{batch_id[:6]}{batch_id[-3:]}"
            if len(batch_id) > 9  # noqa: PLR2004  # Allow magic int value
            else batch_id
        )

        # Note: The normalizer may truncate the table name if the database has a name length limit.
        # For instance, the Postgres normalizer will enforce a 63-character limit on table names.
        return self.normalizer.normalize(f"{stream_name}_{suffix}")

    def _fully_qualified(
        self,
        table_name: str,
    ) -> str:
        """Return the fully qualified name of the given table."""
        return f"{self.sql_config.schema_name}.{self._quote_identifier(table_name)}"

    @final
    def _create_table_for_loading(
        self,
        /,
        stream_name: str,
        batch_id: str,
    ) -> str:
        """Create a new table for loading data."""
        temp_table_name = self._get_temp_table_name(stream_name, batch_id)
        column_definition_str = ",\n  ".join(
            f"{self._quote_identifier(column_name)} {sql_type}"
            for column_name, sql_type in self._get_sql_column_definitions(stream_name).items()
        )
        self._create_table(temp_table_name, column_definition_str)

        return temp_table_name

    def _get_tables_list(
        self,
    ) -> list[str]:
        """Return a list of all tables in the database."""
        with self.get_sql_connection() as conn:
            inspector: Inspector = sqlalchemy.inspect(conn)
            return inspector.get_table_names(schema=self.sql_config.schema_name)

    def _get_schemas_list(
        self,
        database_name: str | None = None,
        *,
        force_refresh: bool = False,
    ) -> list[str]:
        """Return a list of all tables in the database."""
        if not force_refresh and self._known_schemas_list:
            return self._known_schemas_list

        inspector: Inspector = sqlalchemy.inspect(self.get_sql_engine())
        database_name = database_name or self.database_name
        found_schemas = inspector.get_schema_names()
        self._known_schemas_list = [
            found_schema.split(".")[-1].strip('"')
            for found_schema in found_schemas
            if "." not in found_schema
            or (found_schema.split(".")[0].lower().strip('"') == database_name.lower())
        ]
        return self._known_schemas_list

    def _ensure_final_table_exists(
        self,
        stream_name: str,
        *,
        create_if_missing: bool = True,
    ) -> str:
        """Create the final table if it doesn't already exist.

        Return the table name.
        """
        table_name = self.get_sql_table_name(stream_name)
        did_exist = self._table_exists(table_name)
        if not did_exist and create_if_missing:
            column_definition_str = ",\n  ".join(
                f"{self._quote_identifier(column_name)} {sql_type}"
                for column_name, sql_type in self._get_sql_column_definitions(
                    stream_name,
                ).items()
            )
            self._create_table(table_name, column_definition_str)

        return table_name

    def _ensure_compatible_table_schema(
        self,
        stream_name: str,
        table_name: str,
    ) -> None:
        """Return true if the given table is compatible with the stream's schema.

        Raises an exception if the table schema is not compatible with the schema of the
        input stream.
        """
        # TODO: Expand this to check for column types and sizes.
        # https://github.com/airbytehq/pyairbyte/issues/321
        self._add_missing_columns_to_table(
            stream_name=stream_name,
            table_name=table_name,
        )

    @final
    def _create_table(
        self,
        table_name: str,
        column_definition_str: str,
        primary_keys: list[str] | None = None,
    ) -> None:
        if primary_keys:
            pk_str = ", ".join(primary_keys)
            column_definition_str += f",\n  PRIMARY KEY ({pk_str})"

        extra_clauses = "\n".join(self.sql_config.get_create_table_extra_clauses())

        cmd = f"""
        CREATE TABLE {self._fully_qualified(table_name)} (
            {column_definition_str}
        )
        {extra_clauses}
        """
        _ = self._execute_sql(cmd)

    @final
    def _get_sql_column_definitions(
        self,
        stream_name: str,
    ) -> dict[str, sqlalchemy.types.TypeEngine]:
        """Return the column definitions for the given stream."""
        columns: dict[str, sqlalchemy.types.TypeEngine] = {}
        properties = self.catalog_provider.get_stream_properties(stream_name)
        for property_name, json_schema_property_def in properties.items():
            clean_prop_name = self.normalizer.normalize(property_name)
            columns[clean_prop_name] = self.type_converter.to_sql_type(
                json_schema_property_def,
            )

        columns[AB_RAW_ID_COLUMN] = self.type_converter_class.get_string_type()
        columns[AB_EXTRACTED_AT_COLUMN] = sqlalchemy.TIMESTAMP()
        columns[AB_META_COLUMN] = self.type_converter_class.get_json_type()

        return columns

    @final
    def write_stream_data(
        self,
        stream_name: str,
        *,
        write_method: WriteMethod | None = None,
        write_strategy: WriteStrategy | None = None,
        progress_tracker: ProgressTracker,
    ) -> list[BatchHandle]:
        """Finalize all uncommitted batches.

        This is a generic 'final' SQL implementation, which should not be overridden.

        Returns a mapping of batch IDs to batch handles, for those processed batches.

        TODO: Add a dedupe step here to remove duplicates from the temp table.
              Some sources will send us duplicate records within the same stream,
              although this is a fairly rare edge case we can ignore in V1.
        """
        if write_method and write_strategy and write_strategy != WriteStrategy.AUTO:
            raise exc.PyAirbyteInternalError(
                message=(
                    "Both `write_method` and `write_strategy` were provided. "
                    "Only one should be set."
                ),
            )
        if not write_method:
            write_method = self.catalog_provider.resolve_write_method(
                stream_name=stream_name,
                write_strategy=write_strategy or WriteStrategy.AUTO,
            )
        # Flush any pending writes
        self.file_writer.flush_active_batches(
            progress_tracker=progress_tracker,
        )

        with self.finalizing_batches(
            stream_name=stream_name,
            progress_tracker=progress_tracker,
        ) as batches_to_finalize:
            # Make sure the target schema and target table exist.
            self._ensure_schema_exists()
            final_table_name = self._ensure_final_table_exists(
                stream_name,
                create_if_missing=True,
            )

            if not batches_to_finalize:
                # If there are no batches to finalize, return after ensuring the table exists.
                return []

            files: list[Path] = []
            # Get a list of all files to finalize from all pending batches.
            for batch_handle in batches_to_finalize:
                files += batch_handle.files
            # Use the max batch ID as the batch ID for table names.
            max_batch_id = max(batch.batch_id for batch in batches_to_finalize)

            temp_table_name = self._write_files_to_new_table(
                files=files,
                stream_name=stream_name,
                batch_id=max_batch_id,
            )
            try:
                self._write_temp_table_to_final_table(
                    stream_name=stream_name,
                    temp_table_name=temp_table_name,
                    final_table_name=final_table_name,
                    write_method=write_method,
                )
            finally:
                self._drop_temp_table(temp_table_name, if_exists=True)

        progress_tracker.log_stream_finalized(stream_name)

        # Return the batch handles as measure of work completed.
        return batches_to_finalize

    @final
    def cleanup_all(self) -> None:
        """Clean resources."""
        self.file_writer.cleanup_all()

    # Finalizing context manager

    @final
    @contextlib.contextmanager
    def finalizing_batches(
        self,
        stream_name: str,
        progress_tracker: ProgressTracker,
    ) -> Generator[list[BatchHandle], str, None]:
        """Context manager to use for finalizing batches, if applicable.

        Returns a mapping of batch IDs to batch handles, for those processed batches.
        """
        batches_to_finalize: list[BatchHandle] = self.file_writer.get_pending_batches(stream_name)
        state_messages_to_finalize: list[AirbyteStateMessage] = self._pending_state_messages[
            stream_name
        ].copy()
        self._pending_state_messages[stream_name].clear()

        progress_tracker.log_batches_finalizing(stream_name, len(batches_to_finalize))
        yield batches_to_finalize
        self._finalize_state_messages(state_messages_to_finalize)
        progress_tracker.log_batches_finalized(stream_name, len(batches_to_finalize))

        for batch_handle in batches_to_finalize:
            batch_handle.finalized = True

        self._finalized_state_messages[stream_name] += state_messages_to_finalize

    def _execute_sql(self, sql: str | TextClause | Executable) -> CursorResult:
        """Execute the given SQL statement."""
        if isinstance(sql, str):
            sql = text(sql)

        with self.get_sql_connection() as conn:
            try:
                result = conn.execute(sql)
            except (
                sqlalchemy.exc.ProgrammingError,
                sqlalchemy.exc.SQLAlchemyError,
            ) as ex:
                msg = f"Error when executing SQL:\n{sql}\n{type(ex).__name__}{ex!s}"
                raise SQLRuntimeError(msg) from None  # from ex

        return result

    def _drop_temp_table(
        self,
        table_name: str,
        *,
        if_exists: bool = True,
    ) -> None:
        """Drop the given table."""
        exists_str = "IF EXISTS" if if_exists else ""
        self._execute_sql(f"DROP TABLE {exists_str} {self._fully_qualified(table_name)}")

    def _write_files_to_new_table(
        self,
        files: list[Path],
        stream_name: str,
        batch_id: str,
    ) -> str:
        """Write a file(s) to a new table.

        This is a generic implementation, which can be overridden by subclasses
        to improve performance.
        """
        temp_table_name = self._create_table_for_loading(stream_name, batch_id)
        for file_path in files:
            dataframe = pd.read_json(file_path, lines=True)

            sql_column_definitions: dict[str, TypeEngine] = self._get_sql_column_definitions(
                stream_name
            )

            # Remove fields that are not in the schema
            for col_name in dataframe.columns:
                if col_name not in sql_column_definitions:
                    dataframe = dataframe.drop(columns=col_name)

            # Pandas will auto-create the table if it doesn't exist, which we don't want.
            if not self._table_exists(temp_table_name):
                raise exc.PyAirbyteInternalError(
                    message="Table does not exist after creation.",
                    context={
                        "temp_table_name": temp_table_name,
                    },
                )

            # Normalize all column names to lower case.
            dataframe.columns = Index([self.normalizer.normalize(col) for col in dataframe.columns])

            # Write the data to the table.
            dataframe.to_sql(
                temp_table_name,
                self.get_sql_alchemy_url(),
                schema=self.sql_config.schema_name,
                if_exists="append",
                index=False,
                dtype=sql_column_definitions,  # type: ignore[arg-type]
            )
        return temp_table_name

    def _add_column_to_table(
        self,
        table: Table,
        column_name: str,
        column_type: sqlalchemy.types.TypeEngine,
    ) -> None:
        """Add a column to the given table."""
        self._execute_sql(
            text(
                f"ALTER TABLE {self._fully_qualified(table.name)} "
                f"ADD COLUMN {column_name} {column_type}"
            ),
        )

    def _add_missing_columns_to_table(
        self,
        stream_name: str,
        table_name: str,
    ) -> None:
        """Add missing columns to the table.

        This is a no-op if all columns are already present.
        """
        columns = self._get_sql_column_definitions(stream_name)
        # First check without forcing a refresh of the cache (faster). If nothing is missing,
        # then we're done.
        table = self._get_table_by_name(
            table_name,
            force_refresh=False,
        )
        missing_columns: bool = any(column_name not in table.columns for column_name in columns)

        if missing_columns:
            # If we found missing columns, refresh the cache and then take action on anything
            # that's still confirmed missing.
            columns_added = False
            table = self._get_table_by_name(
                table_name,
                force_refresh=True,
            )
            for column_name, column_type in columns.items():
                if column_name not in table.columns:
                    self._add_column_to_table(table, column_name, column_type)
                    columns_added = True

            if columns_added:
                # We've added columns, so invalidate the cache.
                self._invalidate_table_cache(table_name)

    @final
    def _write_temp_table_to_final_table(
        self,
        stream_name: str,
        temp_table_name: str,
        final_table_name: str,
        write_method: WriteMethod,
    ) -> None:
        """Write the temp table into the final table using the provided write strategy."""
        if write_method == WriteMethod.REPLACE:
            # Note: No need to check for schema compatibility
            # here, because we are fully replacing the table.
            self._swap_temp_table_with_final_table(
                stream_name=stream_name,
                temp_table_name=temp_table_name,
                final_table_name=final_table_name,
            )
            return

        if write_method == WriteMethod.APPEND:
            self._ensure_compatible_table_schema(
                stream_name=stream_name,
                table_name=final_table_name,
            )
            self._append_temp_table_to_final_table(
                stream_name=stream_name,
                temp_table_name=temp_table_name,
                final_table_name=final_table_name,
            )
            return

        if write_method == WriteMethod.MERGE:
            self._ensure_compatible_table_schema(
                stream_name=stream_name,
                table_name=final_table_name,
            )
            if not self.supports_merge_insert:
                # Fallback to emulated merge if the database does not support merge natively.
                self._emulated_merge_temp_table_to_final_table(
                    stream_name=stream_name,
                    temp_table_name=temp_table_name,
                    final_table_name=final_table_name,
                )
                return

            self._merge_temp_table_to_final_table(
                stream_name=stream_name,
                temp_table_name=temp_table_name,
                final_table_name=final_table_name,
            )
            return

        raise exc.PyAirbyteInternalError(
            message="Write method is not supported.",
            context={
                "write_method": write_method,
            },
        )

    def _append_temp_table_to_final_table(
        self,
        temp_table_name: str,
        final_table_name: str,
        stream_name: str,
    ) -> None:
        nl = "\n"
        columns = [self._quote_identifier(c) for c in self._get_sql_column_definitions(stream_name)]
        self._execute_sql(
            f"""
            INSERT INTO {self._fully_qualified(final_table_name)} (
            {f',{nl}  '.join(columns)}
            )
            SELECT
            {f',{nl}  '.join(columns)}
            FROM {self._fully_qualified(temp_table_name)}
            """,
        )

    def _swap_temp_table_with_final_table(
        self,
        stream_name: str,
        temp_table_name: str,
        final_table_name: str,
    ) -> None:
        """Merge the temp table into the main one.

        This implementation requires MERGE support in the SQL DB.
        Databases that do not support this syntax can override this method.
        """
        if final_table_name is None:
            raise exc.PyAirbyteInternalError(message="Arg 'final_table_name' cannot be None.")
        if temp_table_name is None:
            raise exc.PyAirbyteInternalError(message="Arg 'temp_table_name' cannot be None.")

        _ = stream_name
        deletion_name = f"{final_table_name}_deleteme"
        commands = "\n".join(
            [
                f"ALTER TABLE {self._fully_qualified(final_table_name)} RENAME "
                f"TO {deletion_name};",
                f"ALTER TABLE {self._fully_qualified(temp_table_name)} RENAME "
                f"TO {final_table_name};",
                f"DROP TABLE {self._fully_qualified(deletion_name)};",
            ]
        )
        self._execute_sql(commands)

    def _merge_temp_table_to_final_table(
        self,
        stream_name: str,
        temp_table_name: str,
        final_table_name: str,
    ) -> None:
        """Merge the temp table into the main one.

        This implementation requires MERGE support in the SQL DB.
        Databases that do not support this syntax can override this method.
        """
        nl = "\n"
        columns = {self._quote_identifier(c) for c in self._get_sql_column_definitions(stream_name)}
        pk_columns = {
            self._quote_identifier(c) for c in self.catalog_provider.get_primary_keys(stream_name)
        }
        non_pk_columns = columns - pk_columns
        join_clause = f"{nl} AND ".join(f"tmp.{pk_col} = final.{pk_col}" for pk_col in pk_columns)
        set_clause = f"{nl}  , ".join(f"{col} = tmp.{col}" for col in non_pk_columns)
        self._execute_sql(
            f"""
            MERGE INTO {self._fully_qualified(final_table_name)} final
            USING (
            SELECT *
            FROM {self._fully_qualified(temp_table_name)}
            ) AS tmp
            ON {join_clause}
            WHEN MATCHED THEN UPDATE
            SET
                {set_clause}
            WHEN NOT MATCHED THEN INSERT
            (
                {f',{nl}    '.join(columns)}
            )
            VALUES (
                tmp.{f',{nl}    tmp.'.join(columns)}
            );
            """,
        )

    def _get_column_by_name(self, table: str | Table, column_name: str) -> Column:
        """Return the column object for the given column name.

        This method is case-insensitive.
        """
        if isinstance(table, str):
            table = self._get_table_by_name(table)
        try:
            # Try to get the column in a case-insensitive manner
            return next(col for col in table.c if col.name.lower() == column_name.lower())
        except StopIteration:
            raise exc.PyAirbyteInternalError(
                message="Could not find matching column.",
                context={
                    "table": table,
                    "column_name": column_name,
                },
            ) from None

    def _emulated_merge_temp_table_to_final_table(
        self,
        stream_name: str,
        temp_table_name: str,
        final_table_name: str,
    ) -> None:
        """Emulate the merge operation using a series of SQL commands.

        This is a fallback implementation for databases that do not support MERGE.
        """
        final_table = self._get_table_by_name(final_table_name)
        temp_table = self._get_table_by_name(temp_table_name)
        pk_columns = self.catalog_provider.get_primary_keys(stream_name)

        columns_to_update: set[str] = self._get_sql_column_definitions(
            stream_name=stream_name
        ).keys() - set(pk_columns)

        # Create a dictionary mapping columns in users_final to users_stage for updating
        update_values = {
            self._get_column_by_name(final_table, column): (
                self._get_column_by_name(temp_table, column)
            )
            for column in columns_to_update
        }

        # Craft the WHERE clause for composite primary keys
        join_conditions = [
            self._get_column_by_name(final_table, pk_column)
            == self._get_column_by_name(temp_table, pk_column)
            for pk_column in pk_columns
        ]
        join_clause = and_(*join_conditions)

        # Craft the UPDATE statement
        update_stmt = update(final_table).values(update_values).where(join_clause)

        # Define a join between temp_table and final_table
        joined_table = temp_table.outerjoin(final_table, join_clause)

        # Define a condition that checks for records in temp_table that do not have a corresponding
        # record in final_table
        where_not_exists_clause = self._get_column_by_name(final_table, pk_columns[0]) == null()

        # Select records from temp_table that are not in final_table
        select_new_records_stmt = (
            select(temp_table).select_from(joined_table).where(where_not_exists_clause)
        )

        # Craft the INSERT statement using the select statement
        insert_new_records_stmt = insert(final_table).from_select(
            names=[column.name for column in temp_table.columns], select=select_new_records_stmt
        )

        if DEBUG_MODE:
            print(str(update_stmt))
            print(str(insert_new_records_stmt))

        with self.get_sql_connection() as conn:
            conn.execute(update_stmt)
            conn.execute(insert_new_records_stmt)

    def _table_exists(
        self,
        table_name: str,
    ) -> bool:
        """Return true if the given table exists.

        Subclasses may override this method to provide a more efficient implementation.
        """
        return table_name in self._get_tables_list()
