"""Detector orchestration: multi-workspace, spec failure fail-safe,
no fetch-per-sync, migration plan and dry-run/apply."""

import json

from airbyte_config_drift.detector import DriftDetector
from airbyte_config_drift.migration import apply_migration
from airbyte_config_drift.models import ChangeKind, ConnectionStatus, MigrationOp
from airbyte_config_drift.spec_cache import SpecCache
from airbyte_config_drift.spec_provider import SpecFetchError, StaticSpecProvider
from airbyte_config_drift.store import JsonConfigStore, StoredConnection

SPEC_V1 = {
    "type": "object",
    "properties": {"host": {"type": "string"}},
    "required": ["host"],
}

SPEC_V2 = {
    "type": "object",
    "properties": {
        "host": {"type": "string"},
        "port": {"type": "integer", "default": 5432},
    },
    "required": ["host", "port"],
}


def make_conn(conn_id, workspace="ws1", tag="2.0.0", config=None):
    return StoredConnection(
        workspace_id=workspace,
        connection_id=conn_id,
        name=f"conn-{conn_id}",
        docker_repository="airbyte/source-x",
        docker_image_tag=tag,
        configuration=config if config is not None else {"host": "db"},
    )


class ListStore:
    def __init__(self, connections):
        self._connections = connections

    def connections(self):
        return iter(self._connections)


class TestMultiWorkspace:
    def test_all_workspaces_are_checked(self, tmp_path):
        doc = {
            "workspaces": [
                {
                    "id": "ws1",
                    "connections": [
                        {
                            "id": "c1",
                            "name": "a",
                            "docker_repository": "airbyte/source-x",
                            "docker_image_tag": "2.0.0",
                            "configuration": {"host": "db"},
                        }
                    ],
                },
                {
                    "id": "ws2",
                    "connections": [
                        {
                            "id": "c2",
                            "name": "b",
                            "docker_repository": "airbyte/source-x",
                            "docker_image_tag": "2.0.0",
                            "configuration": {"host": "db", "port": 1},
                        }
                    ],
                },
            ]
        }
        path = tmp_path / "configs.json"
        path.write_text(json.dumps(doc))
        provider = StaticSpecProvider({("airbyte/source-x", "2.0.0"): SPEC_V2})
        report = DriftDetector(spec_provider=provider).check(JsonConfigStore(path))

        assert {c.workspace_id for c in report.connections} == {"ws1", "ws2"}
        by_id = {c.connection_id: c for c in report.connections}
        assert by_id["c1"].status is ConnectionStatus.BREAKING  # missing required port
        assert by_id["c2"].status is ConnectionStatus.OK

    def test_spec_fetched_once_per_version_not_per_connection(self):
        provider = StaticSpecProvider({("airbyte/source-x", "2.0.0"): SPEC_V2})
        detector = DriftDetector(spec_provider=provider)
        store = ListStore([make_conn(f"c{i}") for i in range(5)])
        detector.check(store)
        # 5 connections, 1 spec fetch: no full spec pull per sync/connection.
        assert provider.calls == [("airbyte/source-x", "2.0.0")]


class TestSpecFetchFailure:
    def test_failure_marks_connection_blocked_and_preserves_config(self):
        class FailingProvider:
            def get_spec(self, repo, tag):
                raise SpecFetchError("registry unreachable")

        detector = DriftDetector(spec_provider=FailingProvider())
        conn = make_conn("c1", config={"host": "db", "legacy": "x"})
        report = detector.check_connection(conn)

        assert report.status is ConnectionStatus.BLOCKED
        assert report.blocking
        assert "registry unreachable" in report.error
        # Config untouched: the detector never mutates the stored config.
        assert conn.configuration == {"host": "db", "legacy": "x"}

    def test_blocked_report_is_blocking_at_report_level(self):
        class FailingProvider:
            def get_spec(self, repo, tag):
                raise SpecFetchError("boom")

        detector = DriftDetector(spec_provider=FailingProvider())
        report = detector.check(ListStore([make_conn("c1")]))
        assert report.blocking
        assert report.to_dict()["summary"]["blocked"] == 1

    def test_oversized_spec_blocks_instead_of_crashing(self):
        big_spec = {"type": "object", "properties": {"blob": {"const": "x" * 10_000}}}
        provider = StaticSpecProvider({("airbyte/source-x", "2.0.0"): big_spec})
        cache = SpecCache(fetcher=provider.get_spec, max_spec_bytes=1_000)
        detector = DriftDetector(spec_provider=provider, cache=cache)
        report = detector.check_connection(make_conn("c1"))
        assert report.status is ConnectionStatus.BLOCKED


class TestStatuses:
    def test_breaking_when_required_field_missing(self):
        provider = StaticSpecProvider({("airbyte/source-x", "2.0.0"): SPEC_V2})
        report = DriftDetector(spec_provider=provider).check_connection(make_conn("c1"))
        assert report.status is ConnectionStatus.BREAKING
        assert report.changes[0].kind is ChangeKind.ADDED_REQUIRED

    def test_compatible_when_only_info_drift(self):
        spec = dict(SPEC_V1)
        spec["properties"] = dict(SPEC_V1["properties"], nickname={"type": "string"})
        provider = StaticSpecProvider({("airbyte/source-x", "2.0.0"): spec})
        report = DriftDetector(spec_provider=provider).check_connection(make_conn("c1"))
        assert report.status is ConnectionStatus.COMPATIBLE
        assert not report.blocking

    def test_ok_when_no_drift(self):
        provider = StaticSpecProvider({("airbyte/source-x", "1.0.0"): SPEC_V1})
        report = DriftDetector(spec_provider=provider).check_connection(make_conn("c1", tag="1.0.0"))
        assert report.status is ConnectionStatus.OK


class TestMigration:
    def test_plan_backfills_default_and_removes_stale_key(self):
        provider = StaticSpecProvider({("airbyte/source-x", "2.0.0"): SPEC_V2})
        conn = make_conn("c1", config={"host": "db", "legacy": "x"})
        report = DriftDetector(spec_provider=provider).check_connection(conn)
        plan = report.migration_plan()

        migrated, skipped = apply_migration(conn.configuration, plan)
        assert migrated == {"host": "db", "port": 5432}
        assert skipped == []

    def test_manual_ops_are_never_executed(self):
        plan = [MigrationOp(op="manual", path="api_key", suggestion="re-enter secret")]
        migrated, skipped = apply_migration({"host": "db"}, plan)
        assert migrated == {"host": "db"}
        assert skipped == ["api_key"]

    def test_apply_does_not_mutate_input(self):
        original = {"host": "db"}
        apply_migration(original, [MigrationOp(op="set", path="port", value=1)])
        assert original == {"host": "db"}

    def test_nested_set_and_remove(self):
        plan = [
            MigrationOp(op="set", path="tunnel.enabled", value=True),
            MigrationOp(op="remove", path="stale.key"),
        ]
        migrated, skipped = apply_migration({"stale": {"key": 1, "keep": 2}}, plan)
        assert migrated == {"stale": {"keep": 2}, "tunnel": {"enabled": True}}
        assert skipped == []
