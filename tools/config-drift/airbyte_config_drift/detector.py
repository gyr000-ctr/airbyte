"""Drift detection orchestration.

For every stored connection (across all workspaces) the detector obtains
the connector's current spec through a bounded cache — so a spec is fetched
at most once per connector version per TTL window, never on every sync —
and diffs the stored configuration against it.

Failure policy: any spec-fetch failure marks the connection BLOCKED. A
blocked connection must not be synced destructively or migrated; its stored
configuration is left untouched. This is the fail-safe path: we never
silently drop configuration we could not validate.
"""

from __future__ import annotations

import logging
from typing import Optional

from airbyte_config_drift.diff import diff_config
from airbyte_config_drift.models import (
    ConnectionReport,
    ConnectionStatus,
    DriftReport,
    worst_status,
)
from airbyte_config_drift.secrets import mask_config
from airbyte_config_drift.spec_cache import SpecCache
from airbyte_config_drift.spec_provider import SpecProvider
from airbyte_config_drift.store import ConfigStore, StoredConnection

logger = logging.getLogger("airbyte_config_drift")


class DriftDetector:
    def __init__(
        self,
        spec_provider: SpecProvider,
        cache: Optional[SpecCache] = None,
    ) -> None:
        self._cache = cache if cache is not None else SpecCache(fetcher=spec_provider.get_spec)

    def check_connection(self, conn: StoredConnection) -> ConnectionReport:
        base = dict(
            workspace_id=conn.workspace_id,
            connection_id=conn.connection_id,
            connection_name=conn.name,
            docker_repository=conn.docker_repository,
            docker_image_tag=conn.docker_image_tag,
        )
        try:
            spec = self._cache.get_or_fetch(conn.docker_repository, conn.docker_image_tag)
        except Exception as exc:  # fail safe: block, never drop config
            logger.warning(
                "spec fetch failed for %s:%s (connection %s); marking BLOCKED: %s",
                conn.docker_repository,
                conn.docker_image_tag,
                conn.connection_id,
                exc,
            )
            return ConnectionReport(
                **base,
                status=ConnectionStatus.BLOCKED,
                error=f"spec unavailable: {exc}",
            )

        changes = diff_config(spec, conn.configuration)
        status = worst_status(changes)
        if logger.isEnabledFor(logging.DEBUG):
            # Log only the masked config — never raw values.
            logger.debug(
                "drift check %s (%s:%s): status=%s config=%s",
                conn.connection_id,
                conn.docker_repository,
                conn.docker_image_tag,
                status.value,
                mask_config(conn.configuration, spec),
            )
        return ConnectionReport(**base, status=status, changes=changes)

    def check(self, store: ConfigStore) -> DriftReport:
        report = DriftReport()
        for conn in store.connections():
            report.connections.append(self.check_connection(conn))
        return report
