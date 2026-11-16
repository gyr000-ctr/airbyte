"""CLI behavior: dry-run by default, --enforce gating, --apply never in
place and never rewriting blocked connections."""

import json

from airbyte_config_drift.cli import main
from airbyte_config_drift.spec_provider import SpecFetchError, StaticSpecProvider

SPEC = {
    "type": "object",
    "properties": {
        "host": {"type": "string"},
        "port": {"type": "integer", "default": 5432},
    },
    "required": ["host", "port"],
}


def write_store(tmp_path, connections):
    doc = {
        "workspaces": [
            {
                "id": "ws1",
                "connections": [
                    {
                        "id": cid,
                        "name": cid,
                        "docker_repository": "airbyte/source-x",
                        "docker_image_tag": tag,
                        "configuration": config,
                    }
                    for cid, tag, config in connections
                ],
            }
        ]
    }
    path = tmp_path / "configs.json"
    path.write_text(json.dumps(doc))
    return path


def test_dry_run_is_default_and_changes_nothing(tmp_path, capsys):
    store = write_store(tmp_path, [("c1", "2.0.0", {"host": "db"})])
    provider = StaticSpecProvider({("airbyte/source-x", "2.0.0"): SPEC})

    rc = main(["--configs", str(store)], spec_provider=provider)

    assert rc == 0  # advisory by default: sync behavior unchanged
    output = json.loads(capsys.readouterr().out)
    assert output["connections"][0]["status"] == "breaking"
    # File untouched.
    assert json.loads(store.read_text())["workspaces"][0]["connections"][0]["configuration"] == {
        "host": "db"
    }


def test_enforce_exits_nonzero_on_breaking(tmp_path):
    store = write_store(tmp_path, [("c1", "2.0.0", {"host": "db"})])
    provider = StaticSpecProvider({("airbyte/source-x", "2.0.0"): SPEC})
    rc = main(["--configs", str(store), "--enforce"], spec_provider=provider)
    assert rc == 1


def test_enforce_exits_nonzero_on_spec_failure(tmp_path):
    class FailingProvider:
        def get_spec(self, repo, tag):
            raise SpecFetchError("registry down")

    store = write_store(tmp_path, [("c1", "2.0.0", {"host": "db"})])
    rc = main(["--configs", str(store), "--enforce"], spec_provider=FailingProvider())
    assert rc == 1  # fail safe: block destructive sync, don't silently proceed


def test_enforce_passes_when_compatible(tmp_path):
    store = write_store(tmp_path, [("c1", "2.0.0", {"host": "db", "port": 5432})])
    provider = StaticSpecProvider({("airbyte/source-x", "2.0.0"): SPEC})
    rc = main(["--configs", str(store), "--enforce"], spec_provider=provider)
    assert rc == 0


def test_apply_writes_new_file_and_skips_blocked(tmp_path):
    store = write_store(
        tmp_path,
        [
            ("c1", "2.0.0", {"host": "db"}),
            ("c2", "2.0.0", {"host": "db", "legacy": "x"}),
            ("c3", "9.9.9", {"host": "db", "legacy": "keep-me"}),
        ],
    )

    class PartialProvider:
        def get_spec(self, repo, tag):
            if tag == "9.9.9":
                raise SpecFetchError("registry unreachable")
            return SPEC

    out = tmp_path / "migrated.json"
    rc = main(
        ["--configs", str(store), "--apply", str(out)],
        spec_provider=PartialProvider(),
    )
    assert rc == 0
    migrated = json.loads(out.read_text())["workspaces"][0]["connections"]
    by_id = {c["id"]: c for c in migrated}
    # Safe ops applied: default backfilled, stale key removed.
    assert by_id["c1"]["configuration"] == {"host": "db", "port": 5432}
    assert by_id["c2"]["configuration"] == {"host": "db", "port": 5432}
    # Blocked connection (spec fetch failed): config preserved verbatim.
    assert by_id["c3"]["configuration"] == {"host": "db", "legacy": "keep-me"}
    # Original file untouched (never in place).
    assert json.loads(store.read_text())["workspaces"][0]["connections"][0]["configuration"] == {
        "host": "db"
    }
