"""Apply migration plans to stored configs.

Only non-destructive-by-default execution: ``set`` ops backfill defaults,
``remove`` ops drop stale keys, ``manual`` ops are never executed. Applying
is always explicit (the CLI requires ``--apply``); the default dry-run only
prints the plan.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from airbyte_config_drift.models import MigrationOp


def _split(path: str) -> list[str]:
    return [p for p in path.split(".") if p]


def apply_migration(config: Mapping[str, Any], plan: list[MigrationOp]) -> tuple[dict, list[str]]:
    """Return ``(new_config, skipped)``: the migrated config and the paths
    of manual ops that were not executed.

    The input is never mutated. Unknown ops are skipped and reported.
    """
    new_config: dict = copy.deepcopy(dict(config))
    skipped: list[str] = []

    for op in plan:
        parts = _split(op.path)
        if not parts:
            skipped.append(op.path)
            continue
        if op.op == "set":
            node = new_config
            for part in parts[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
            node[parts[-1]] = copy.deepcopy(op.value)
        elif op.op == "remove":
            node = new_config
            for part in parts[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = None
                    break
                node = child
            if isinstance(node, dict):
                node.pop(parts[-1], None)
        else:  # "manual" and anything unknown
            skipped.append(op.path)

    return new_config, skipped
