"""Data model for drift detection results.

Reports never contain secret values: ``FieldChange.old_value`` and
``FieldChange.new_value`` are always ``None`` for secret fields (see
:mod:`airbyte_config_drift.secrets`).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class ChangeKind(str, enum.Enum):
    ADDED_REQUIRED = "added_required"
    ADDED_OPTIONAL = "added_optional"
    REMOVED = "removed"
    UNKNOWN_FIELD = "unknown_field"
    TYPE_CHANGED = "type_changed"
    DEFAULT_CHANGED = "default_changed"
    ENUM_CHANGED = "enum_changed"


class Severity(str, enum.Enum):
    # Breaking: the stored config will fail validation against the new spec.
    BREAKING = "breaking"
    # Warning: behavior may change (e.g. a default changed) but the config
    # still validates.
    WARNING = "warning"
    # Info: fully backward compatible (e.g. new optional field).
    INFO = "info"


# Order used to pick the worst severity of a report.
_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.BREAKING: 2}


class ConnectionStatus(str, enum.Enum):
    OK = "ok"  # no drift at all
    COMPATIBLE = "compatible"  # only INFO-level drift
    WARNING = "warning"  # worst drift is WARNING
    BREAKING = "breaking"  # worst drift is BREAKING
    BLOCKED = "blocked"  # spec could not be fetched; fail safe, do not sync


@dataclass
class MigrationOp:
    """One executable migration step.

    ``op`` is one of ``set`` (write ``value`` at ``path``), ``remove``
    (delete the key at ``path``) or ``manual`` (no safe automatic step;
    ``suggestion`` describes what a human must do). Secret fields always
    produce ``manual`` ops so values are never materialized by the tool.
    """

    op: str
    path: str
    value: Any = None
    suggestion: str = ""

    def to_dict(self, redact: bool = True) -> dict:
        # ``value`` is only ever populated for non-secret fields, but redact
        # defensively when asked.
        return {
            "op": self.op,
            "path": self.path,
            "value": self.value,
            "suggestion": self.suggestion,
        }


@dataclass
class FieldChange:
    kind: ChangeKind
    severity: Severity
    path: str  # dotted JSON path, e.g. "credentials.api_key"
    message: str
    is_secret: bool = False
    old_value: Any = None  # always None for secret fields
    new_value: Any = None  # always None for secret fields
    migration: Optional[MigrationOp] = None

    def to_dict(self) -> dict:
        d = {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "path": self.path,
            "message": self.message,
            "is_secret": self.is_secret,
            "old_value": "***" if self.is_secret and self.old_value is not None else self.old_value,
            "new_value": "***" if self.is_secret and self.new_value is not None else self.new_value,
        }
        if self.migration is not None:
            d["migration"] = self.migration.to_dict()
        return d


@dataclass
class ConnectionReport:
    workspace_id: str
    connection_id: str
    connection_name: str
    docker_repository: str
    docker_image_tag: str
    status: ConnectionStatus
    changes: list[FieldChange] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def blocking(self) -> bool:
        """True when a destructive sync/migration must not proceed."""
        return self.status in (ConnectionStatus.BREAKING, ConnectionStatus.BLOCKED)

    def migration_plan(self) -> list[MigrationOp]:
        return [c.migration for c in self.changes if c.migration is not None]

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "connection_id": self.connection_id,
            "connection_name": self.connection_name,
            "connector": f"{self.docker_repository}:{self.docker_image_tag}",
            "status": self.status.value,
            "error": self.error,
            "changes": [c.to_dict() for c in self.changes],
        }


@dataclass
class DriftReport:
    connections: list[ConnectionReport] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(c.blocking for c in self.connections)

    def by_status(self, status: ConnectionStatus) -> list[ConnectionReport]:
        return [c for c in self.connections if c.status is status]

    def to_dict(self) -> dict:
        return {
            "blocking": self.blocking,
            "summary": {
                status.value: len(self.by_status(status)) for status in ConnectionStatus
            },
            "connections": [c.to_dict() for c in self.connections],
        }


def worst_status(changes: list[FieldChange]) -> ConnectionStatus:
    if not changes:
        return ConnectionStatus.OK
    worst = max(_SEVERITY_RANK[c.severity] for c in changes)
    if worst == _SEVERITY_RANK[Severity.BREAKING]:
        return ConnectionStatus.BREAKING
    if worst == _SEVERITY_RANK[Severity.WARNING]:
        return ConnectionStatus.WARNING
    return ConnectionStatus.COMPATIBLE
