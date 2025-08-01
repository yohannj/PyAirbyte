# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
"""Connectivity to the connector catalog registry."""

from __future__ import annotations

import json
import logging
import os
import warnings
from copy import copy
from enum import Enum
from pathlib import Path

import requests
from pydantic import BaseModel

from airbyte import exceptions as exc
from airbyte._util.meta import is_docker_installed
from airbyte.constants import AIRBYTE_OFFLINE_MODE
from airbyte.logs import warn_once
from airbyte.version import get_version


logger = logging.getLogger("airbyte")


__cache: dict[str, ConnectorMetadata] | None = None


_REGISTRY_ENV_VAR = "AIRBYTE_LOCAL_REGISTRY"
_REGISTRY_URL = "https://connectors.airbyte.com/files/registries/v0/oss_registry.json"

_PYTHON_LANGUAGE = "python"
_MANIFEST_ONLY_LANGUAGE = "manifest-only"

_PYTHON_LANGUAGE_TAG = f"language:{_PYTHON_LANGUAGE}"
_MANIFEST_ONLY_TAG = f"language:{_MANIFEST_ONLY_LANGUAGE}"


class InstallType(str, Enum):
    """The type of installation for a connector."""

    YAML = "yaml"
    PYTHON = "python"
    DOCKER = "docker"
    JAVA = "java"


class Language(str, Enum):
    """The language of a connector."""

    PYTHON = InstallType.PYTHON.value
    JAVA = InstallType.JAVA.value
    MANIFEST_ONLY = _MANIFEST_ONLY_LANGUAGE


class ConnectorMetadata(BaseModel):
    """Metadata for a connector."""

    name: str
    """Connector name. For example, "source-google-sheets"."""

    latest_available_version: str | None
    """The latest available version of the connector."""

    pypi_package_name: str | None
    """The name of the PyPI package for the connector, if it exists."""

    language: Language | None
    """The language of the connector."""

    install_types: set[InstallType]
    """The supported install types for the connector."""

    suggested_streams: list[str] | None = None
    """A list of suggested streams for the connector, if available."""

    @property
    def default_install_type(self) -> InstallType:
        """Return the default install type for the connector."""
        if self.language == Language.MANIFEST_ONLY and InstallType.YAML in self.install_types:
            return InstallType.YAML

        if InstallType.PYTHON in self.install_types:
            return InstallType.PYTHON

        # Else: Java or Docker
        return InstallType.DOCKER


def _get_registry_url() -> str:
    if _REGISTRY_ENV_VAR in os.environ:
        return str(os.environ.get(_REGISTRY_ENV_VAR))

    return _REGISTRY_URL


def _is_registry_disabled(url: str) -> bool:
    return url.upper() in {"0", "F", "FALSE"} or AIRBYTE_OFFLINE_MODE


def _registry_entry_to_connector_metadata(entry: dict) -> ConnectorMetadata:
    name = entry["dockerRepository"].replace("airbyte/", "")
    latest_version: str | None = entry.get("dockerImageTag")
    tags = entry.get("tags", [])
    language: Language | None = None

    if "language" in entry and entry["language"] is not None:
        try:
            language = Language(entry["language"])
        except Exception:
            warnings.warn(
                message=f"Invalid language for connector {name}: {entry['language']}",
                stacklevel=2,
            )
    if not language and _PYTHON_LANGUAGE_TAG in tags:
        language = Language.PYTHON
    if not language and _MANIFEST_ONLY_TAG in tags:
        language = Language.MANIFEST_ONLY

    remote_registries: dict = entry.get("remoteRegistries", {})
    pypi_registry: dict = remote_registries.get("pypi", {})
    pypi_package_name: str = pypi_registry.get("packageName", None)
    pypi_enabled: bool = pypi_registry.get("enabled", False)
    install_types: set[InstallType] = {
        x
        for x in [
            InstallType.DOCKER,  # Always True
            InstallType.PYTHON if language == Language.PYTHON and pypi_enabled else None,
            InstallType.JAVA if language == Language.JAVA else None,
            InstallType.YAML if language == Language.MANIFEST_ONLY else None,
        ]
        if x
    }

    return ConnectorMetadata(
        name=name,
        latest_available_version=latest_version,
        pypi_package_name=pypi_package_name if pypi_enabled else None,
        language=language,
        install_types=install_types,
        suggested_streams=entry.get("suggestedStreams", {}).get("streams", None),
    )


def _get_registry_cache(*, force_refresh: bool = False) -> dict[str, ConnectorMetadata]:
    """Return the registry cache."""
    global __cache
    if __cache and not force_refresh:
        return __cache

    registry_url = _get_registry_url()

    if _is_registry_disabled(registry_url):
        return {}

    if registry_url.startswith("http"):
        response = requests.get(
            registry_url,
            headers={"User-Agent": f"PyAirbyte/{get_version()}"},
        )
        response.raise_for_status()
        data = response.json()
    else:
        # Assume local file
        with Path(registry_url).open(encoding="utf-8") as f:
            data = json.load(f)

    new_cache: dict[str, ConnectorMetadata] = {}

    for connector in data["sources"]:
        connector_metadata = _registry_entry_to_connector_metadata(connector)
        new_cache[connector_metadata.name] = connector_metadata

    for connector in data["destinations"]:
        connector_metadata = _registry_entry_to_connector_metadata(connector)
        new_cache[connector_metadata.name] = connector_metadata

    if len(new_cache) == 0:
        # This isn't necessarily fatal, since users can bring their own
        # connector definitions.
        warn_once(
            message=f"Connector registry is empty: {registry_url}",
            with_stack=False,
        )

    __cache = new_cache
    return __cache


def get_connector_metadata(name: str) -> ConnectorMetadata | None:
    """Check the cache for the connector.

    If the cache is empty, populate by calling update_cache.
    """
    registry_url = _get_registry_url()

    if _is_registry_disabled(registry_url):
        return None

    cache = copy(_get_registry_cache())

    if not cache:
        raise exc.PyAirbyteInternalError(
            message="Connector registry could not be loaded.",
            context={
                "registry_url": _get_registry_url(),
            },
        )
    if name not in cache:
        raise exc.AirbyteConnectorNotRegisteredError(
            connector_name=name,
            context={
                "registry_url": _get_registry_url(),
                "available_connectors": get_available_connectors(),
            },
        )
    return cache[name]


def get_available_connectors(install_type: InstallType | str | None = None) -> list[str]:
    """Return a list of all available connectors.

    Connectors will be returned in alphabetical order, with the standard prefix "source-".
    """
    if install_type is None:
        # No install type specified. Filter for whatever is runnable.
        if is_docker_installed():
            logger.info("Docker is detected. Returning all connectors.")
            # If Docker is available, return all connectors.
            return sorted(conn.name for conn in _get_registry_cache().values())

        logger.info("Docker was not detected. Returning only Python and Manifest-only connectors.")

        # If Docker is not available, return only Python and Manifest-based connectors.
        return sorted(
            conn.name
            for conn in _get_registry_cache().values()
            if conn.language in {Language.PYTHON, Language.MANIFEST_ONLY}
        )

    if not isinstance(install_type, InstallType):
        install_type = InstallType(install_type)

    if install_type == InstallType.PYTHON:
        return sorted(
            conn.name
            for conn in _get_registry_cache().values()
            if conn.pypi_package_name is not None
        )

    if install_type == InstallType.JAVA:
        warnings.warn(
            message="Java connectors are not yet supported.",
            stacklevel=2,
        )
        return sorted(
            conn.name for conn in _get_registry_cache().values() if conn.language == Language.JAVA
        )

    if install_type == InstallType.DOCKER:
        return sorted(conn.name for conn in _get_registry_cache().values())

    if install_type == InstallType.YAML:
        return sorted(
            conn.name
            for conn in _get_registry_cache().values()
            if InstallType.YAML in conn.install_types
        )

    # pragma: no cover  # Should never be reached.
    raise exc.PyAirbyteInputError(
        message="Invalid install type.",
        context={
            "install_type": install_type,
        },
    )
