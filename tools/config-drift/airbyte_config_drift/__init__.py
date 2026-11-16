"""Airbyte connector configuration drift detection.

Compares stored connection configurations against the connector's current
spec (``connectionSpecification``) and reports drift: added/removed fields,
type changes, default changes and enum changes, together with executable
migration suggestions.

Design guarantees:
- Secret values are never logged or embedded in reports.
- Spec fetching is cached (bounded size, bounded entries, TTL) so a spec is
  never pulled on every sync.
- Spec fetch failures fail safe: the connection is marked BLOCKED instead of
  silently dropping or migrating its configuration.
- Detection is advisory by default; sync behavior is unchanged unless
  ``enforce`` is requested explicitly.
"""

from airbyte_config_drift.models import (
    ChangeKind,
    ConnectionReport,
    ConnectionStatus,
    DriftReport,
    FieldChange,
    MigrationOp,
    Severity,
)
from airbyte_config_drift.detector import DriftDetector
from airbyte_config_drift.spec_cache import SpecCache
from airbyte_config_drift.spec_provider import (
    DockerSpecProvider,
    SpecFetchError,
    SpecProvider,
    StaticSpecProvider,
)
from airbyte_config_drift.store import JsonConfigStore

__all__ = [
    "ChangeKind",
    "ConnectionReport",
    "ConnectionStatus",
    "DockerSpecProvider",
    "DriftDetector",
    "DriftReport",
    "FieldChange",
    "JsonConfigStore",
    "MigrationOp",
    "Severity",
    "SpecCache",
    "SpecFetchError",
    "SpecProvider",
    "StaticSpecProvider",
]

__version__ = "0.1.0"
