"""Command-line interface.

Default behavior is a dry-run: it prints the drift report and the migration
plan and changes nothing (sync behavior is untouched). ``--apply`` writes
migrated configs to a *new* file. ``--enforce`` exits non-zero when any
connection is BREAKING or BLOCKED, so a sync pipeline can gate destructive
syncs on drift without changing the default path.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from airbyte_config_drift.detector import DriftDetector
from airbyte_config_drift.migration import apply_migration
from airbyte_config_drift.models import ConnectionStatus
from airbyte_config_drift.spec_provider import DockerSpecProvider
from airbyte_config_drift.store import JsonConfigStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airbyte-config-drift",
        description="Detect connector config drift against current connector specs.",
    )
    parser.add_argument("--configs", required=True, help="JSON config store (see JsonConfigStore)")
    parser.add_argument(
        "--apply",
        metavar="OUT_JSON",
        help="apply safe migration ops and write migrated configs to OUT_JSON (never in place)",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="exit 1 when any connection is BREAKING or BLOCKED (gate destructive syncs)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None, spec_provider=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    store = JsonConfigStore(args.configs)
    detector = DriftDetector(spec_provider=spec_provider or DockerSpecProvider())
    report = detector.check(store)

    print(json.dumps(report.to_dict(), indent=2))

    if args.apply:
        _apply(store, report, Path(args.apply))

    if args.enforce and report.blocking:
        return 1
    return 0


def _apply(store: JsonConfigStore, report, out_path: Path) -> None:
    source = json.loads(store._path.read_text(encoding="utf-8"))  # noqa: SLF001
    plans = {c.connection_id: c.migration_plan() for c in report.connections}
    # Fail safe: never rewrite a config whose spec could not be fetched.
    # BREAKING connections are *not* skipped — migrating them is the point.
    blocked = {
        c.connection_id
        for c in report.connections
        if c.status is ConnectionStatus.BLOCKED
    }
    for workspace in source.get("workspaces", []):
        for conn in workspace.get("connections", []):
            conn_id = str(conn.get("id", ""))
            if conn_id in blocked:
                # Fail safe: never rewrite a config we could not validate.
                continue
            plan = plans.get(conn_id, [])
            if plan:
                migrated, skipped = apply_migration(conn.get("configuration", {}), plan)
                conn["configuration"] = migrated
                if skipped:
                    conn.setdefault("drift_manual_steps", skipped)
    out_path.write_text(json.dumps(source, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
