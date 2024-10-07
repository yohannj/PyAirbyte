# Copyright (c) 2024 Airbyte, Inc., all rights reserved.
"""CLI for PyAirbyte.

The PyAirbyte CLI provides a command-line interface for testing connectors and running benchmarks.

PyAirbyte CLI can be invoked with the `pyairbyte` CLI executable, or the
shorter `pyab` alias.

These are equivalent:

    ```bash
    python -m airbyte.cli --help
    pyairbyte --help
    pyab --help
    ```

You can also use the fast and powerful `uv` tool to run the CLI without pre-installing:

    ```
    # Install `uv` if you haven't already:
    brew install uv

    # Run the PyAirbyte CLI using `uvx`:
    uvx --from=airbyte pyab --help
    ```
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
import yaml

from airbyte.destinations.util import get_destination, get_noop_destination
from airbyte.exceptions import PyAirbyteInputError
from airbyte.secrets.util import get_secret
from airbyte.sources.util import get_benchmark_source, get_source


if TYPE_CHECKING:
    from airbyte.destinations.base import Destination
    from airbyte.sources.base import Source


CLI_GUIDANCE = """
----------------------

PyAirbyte CLI Guidance

Providing connector configuration:

When providing configuration via `--config`, you can providing any of the following:

1. A path to a configuration file, in yaml or json format.

2. An inline yaml string, e.g. `--config='{key: value}'`, --config='{key: {nested: value}}'.

When providing an inline yaml string, it is recommended to use single quotes to avoid shell
interpolation.

Providing secrets:

You can provide secrets in your configuration file by prefixing the secret value with `SECRET:`.
For example, --config='{password: "SECRET:my_password"'} will look for a secret named `my_password`
in the secret store. By default, PyAirbyte will look for secrets in environment variables and
dotenv (.env) files. If a secret is not found, you'll be prompted to provide the secret value
interactively in the terminal.

It is highly recommended to use secrets when using inline yaml strings, in order to avoid
exposing secrets in plain text in the terminal history. Secrets provided interactively will
not be echoed to the terminal.
"""

# Add the CLI guidance to the module docstring.
globals()["__doc__"] = globals().get("__doc__", "") + CLI_GUIDANCE

CONFIG_HELP = (
    "Either a path to a configuration file for the named source or destination, "
    "or an inline yaml string. If providing an inline yaml string, use single quotes "
    "to avoid shell interpolation. For example, --config='{key: value}' or "
    "--config='{key: {nested: value}}'. \n"
    "PyAirbyte secrets can be accessed by prefixing the secret name with 'SECRET:'. "
    """For example, --config='{password: "SECRET:MY_PASSWORD"}'."""
)


def _resolve_config(
    config: str,
) -> dict[str, Any]:
    """Resolve the configuration file into a dictionary."""

    def _inject_secrets(config_dict: dict[str, Any]) -> None:
        """Inject secrets into the configuration dictionary."""
        for key, value in config_dict.items():
            if isinstance(value, dict):
                _inject_secrets(value)
            elif isinstance(value, str) and value.startswith("SECRET:"):
                config_dict[key] = get_secret(value.removeprefix("SECRET:").strip())

    config_dict: dict[str, Any]
    if config.startswith("{"):
        # Treat this as an inline yaml string:
        config_dict = yaml.safe_load(config)
    else:
        # Treat this as a path to a config file:
        config_path = Path(config)
        if not config_path.exists():
            raise PyAirbyteInputError(
                message="Config file not found.",
                input_value=str(config_path),
            )
        config_dict = json.loads(config_path.read_text(encoding="utf-8"))

    _inject_secrets(config_dict)
    return config_dict


def _resolve_source_job(
    *,
    source: str | None = None,
    config: str | None = None,
    streams: str | None = None,
) -> Source:
    """Resolve the source job into a configured Source object.

    Args:
        source: The source name, with an optional version declaration.
            If a path is provided, the source will be loaded from the local path.
            If the string `'.'` is provided, the source will be loaded from the current
            working directory.
        config: The path to a configuration file for the named source or destination.
        streams: A comma-separated list of stream names to select for reading. If set to "*",
            all streams will be selected. If not provided, all streams will be selected.
    """
    source_obj: Source
    if source and (source.startswith(".") or "/" in source):
        # Treat the source as a path.
        source_executable = Path(source)
        if not source_executable.exists():
            raise PyAirbyteInputError(
                message="Source executable not found.",
                context={
                    "source": source,
                },
            )
        source_obj = get_source(
            name=source_executable.stem,
            local_executable=source_executable,
        )
        return source_obj
    if not config:
        raise PyAirbyteInputError(
            message="No configuration found.",
        )
    if not source or not source.startswith("source-"):
        raise PyAirbyteInputError(
            message="Expected a source name or path to executable.",
            input_value=source,
        )

    source_name: str = source
    streams_list: str | list[str] = streams or "*"
    if isinstance(streams, str) and streams != "*":
        streams_list = [stream.strip() for stream in streams.split(",")]

    return get_source(
        name=source_name,
        config=_resolve_config(config) if config else {},
        streams=streams_list,
    )


def _resolve_destination_job(
    *,
    destination: str,
    config: str | None = None,
) -> Destination:
    """Resolve the destination job into a configured Destination object.

    Args:
        destination: The destination name, with an optional version declaration.
            If a path is provided, the destination will be loaded from the local path.
            If the string `'.'` is provided, the destination will be loaded from the current
            working directory.
        config: The path to a configuration file for the named source or destination.
    """
    if not config:
        raise PyAirbyteInputError(
            message="No configuration found.",
        )

    config_dict = _resolve_config(config)

    if destination and (destination.startswith(".") or "/" in destination):
        # Treat the destination as a path.
        destination_executable = Path(destination)
        if not destination_executable.exists():
            raise PyAirbyteInputError(
                message="Destination executable not found.",
                context={
                    "destination": destination,
                },
            )
        return get_destination(
            name=destination_executable.stem,
            local_executable=destination_executable,
            config=config_dict,
        )

    # else: # Treat the destination as a name.

    return get_destination(
        name=destination,
        config=config_dict,
    )


@click.command(
    help=(
        "Validate the connector has a valid CLI and is able to run `spec`. "
        "If 'config' is provided, we will also run a `check` on the connector "
        "with the provided config.\n\n" + CLI_GUIDANCE
    ),
)
@click.option(
    "--connector",
    type=str,
    help="The connector name or a path to the local executable.",
)
@click.option(
    "--config",
    type=str,
    required=False,
    help=CONFIG_HELP,
)
@click.option(
    "--install",
    is_flag=True,
    default=False,
    help=(
        "Whether to install the connector if it is not available locally. "
        "Defaults to False, meaning the connector is expected to be already be installed."
    ),
)
def validate(
    connector: str | None = None,
    config: str | None = None,
    *,
    install: bool = False,
) -> None:
    """Validate the connector."""
    local_executable: Path | None = None
    if not connector:
        raise PyAirbyteInputError(
            message="No connector provided.",
        )
    if connector.startswith(".") or "/" in connector:
        # Treat the connector as a path.
        local_executable = Path(connector)
        if not local_executable.exists():
            raise PyAirbyteInputError(
                message="Connector executable not found.",
                context={
                    "connector": connector,
                },
            )
        connector_name = local_executable.stem
    else:
        connector_name = connector

    if not connector_name.startswith("source-") and not connector_name.startswith("destination-"):
        raise PyAirbyteInputError(
            message=(
                "Expected a connector name or path to executable. "
                "Connector names are expected to begin with 'source-' or 'destination-'."
            ),
            input_value=connector,
        )

    connector_obj: Source | Destination
    if connector_name.startswith("source-"):
        connector_obj = get_source(
            name=connector_name,
            local_executable=local_executable,
            install_if_missing=install,
        )
    else:  # destination
        connector_obj = get_destination(
            name=connector_name,
            local_executable=local_executable,
            install_if_missing=install,
        )

    print("Getting `spec` output from connector...")
    connector_obj.print_config_spec()

    if config:
        print("Running connector check...")
        config_dict: dict[str, Any] = _resolve_config(config)
        connector_obj.set_config(config_dict)
        connector_obj.check()


@click.command()
@click.option(
    "--source",
    type=str,
    help=(
        "The source name, with an optional version declaration. "
        "If a path is provided, it will be interpreted as a path to the local executable. "
    ),
)
@click.option(
    "--streams",
    type=str,
    default="*",
    help=(
        "A comma-separated list of stream names to select for reading. If set to '*', all streams "
        "will be selected. Defaults to '*'."
    ),
)
@click.option(
    "--num-records",
    type=str,
    default="5e5",
    help=(
        "The number of records to generate for the benchmark. Ignored if a source is provided. "
        "You can specify the number of records to generate using scientific notation. "
        "For example, `5e6` will generate 5 million records. By default, 500,000 records will "
        "be generated (`5e5` records). If underscores are providing within a numeric a string, "
        "they will be ignored."
    ),
)
@click.option(
    "--destination",
    type=str,
    help=(
        "The destination name, with an optional version declaration. "
        "If a path is provided, it will be interpreted as a path to the local executable. "
    ),
)
@click.option(
    "--config",
    type=str,
    help=CONFIG_HELP,
)
def benchmark(
    source: str | None = None,
    streams: str = "*",
    num_records: int | str = "5e5",  # 500,000 records
    destination: str | None = None,
    config: str | None = None,
) -> None:
    """Run benchmarks.

    You can provide either a source or a destination, but not both. If a destination is being
    benchmarked, you can use `--num-records` to specify the number of records to generate for the
    benchmark.

    If a source is being benchmarked, you can provide a configuration file or a job
    definition file to run the source job.
    """
    if source and destination:
        raise PyAirbyteInputError(
            message="For benchmarking, source or destination can be provided, but not both.",
        )
    destination_obj: Destination
    source_obj: Source

    source_obj = (
        _resolve_source_job(
            source=source,
            config=config,
            streams=streams,
        )
        if source
        else get_benchmark_source(
            num_records=num_records,
        )
    )
    destination_obj = (
        _resolve_destination_job(
            destination=destination,
            config=config,
        )
        if destination
        else get_noop_destination()
    )

    click.echo("Running benchmarks...")
    destination_obj.write(
        source_data=source_obj,
        cache=False,
        state_cache=False,
    )


@click.group()
def cli() -> None:
    """PyAirbyte CLI."""
    pass


cli.add_command(validate)
cli.add_command(benchmark)

if __name__ == "__main__":
    cli()