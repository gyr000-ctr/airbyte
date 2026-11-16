"""Stored-configuration access.

The detector works against any object implementing :class:`ConfigStore`;
:class:`JsonConfigStore` is a file-backed implementation for offline
analysis and tests. A production deployment would implement the same
protocol over the Airbyte platform's config database / API, keeping the
detector itself storage-agnostic (and multi-workspace by construction).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol


@dataclass
class StoredConnection:
    workspace_id: str
    connection_id: str
    name: str
    docker_repository: str
    docker_image_tag: str
    configuration: dict = field(default_factory=dict)


class ConfigStore(Protocol):
    def connections(self) -> Iterator[StoredConnection]:
        """Yield every stored connection, across all workspaces."""
        ...


class JsonConfigStore:
    """Reads a JSON document of the shape::

        {"workspaces": [{"id": "...", "connections": [{
            "id": "...", "name": "...",
            "docker_repository": "airbyte/source-x",
            "docker_image_tag": "1.2.3",
            "configuration": {...}}]}]}
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def connections(self) -> Iterator[StoredConnection]:
        doc = json.loads(self._path.read_text(encoding="utf-8"))
        for workspace in doc.get("workspaces", []):
            workspace_id = str(workspace.get("id", ""))
            for conn in workspace.get("connections", []):
                yield StoredConnection(
                    workspace_id=workspace_id,
                    connection_id=str(conn.get("id", "")),
                    name=str(conn.get("name", conn.get("id", ""))),
                    docker_repository=str(conn["docker_repository"]),
                    docker_image_tag=str(conn["docker_image_tag"]),
                    configuration=dict(conn.get("configuration", {})),
                )
