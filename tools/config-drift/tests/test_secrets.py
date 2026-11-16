"""Secret handling: values must never leak into reports, migrations or logs."""

import json
import logging

from airbyte_config_drift.detector import DriftDetector
from airbyte_config_drift.diff import diff_config
from airbyte_config_drift.models import ChangeKind
from airbyte_config_drift.secrets import MASK, is_secret_field, mask_config
from airbyte_config_drift.spec_provider import StaticSpecProvider
from airbyte_config_drift.store import StoredConnection

SECRET_VALUE = "s3cr3t-token-12345"

SPEC = {
    "type": "object",
    "properties": {
        "host": {"type": "string"},
        "api_key": {"type": "string", "airbyte_secret": True},
    },
    "required": ["host", "api_key"],
}


def test_airbyte_secret_marker_marks_field():
    assert is_secret_field({"airbyte_secret": True}, "whatever")
    assert not is_secret_field({"airbyte_secret": False}, "whatever")


def test_credential_like_names_are_secret_without_schema():
    for name in ("password", "api_token", "client_secret", "private_key"):
        assert is_secret_field(None, name)
    assert not is_secret_field(None, "host")


def test_mask_config_masks_marked_and_guessed_fields():
    masked = mask_config(
        {"host": "db", "api_key": SECRET_VALUE, "nested": {"token": "abc"}},
        SPEC,
    )
    assert masked["host"] == "db"
    assert masked["api_key"] == MASK
    # "token" is not in the spec's properties, but the name heuristic masks it.
    assert masked["nested"]["token"] == MASK
    assert SECRET_VALUE not in json.dumps(masked)


def test_missing_required_secret_produces_manual_migration_without_value():
    changes = diff_config(SPEC, {"host": "db"})
    assert len(changes) == 1
    change = changes[0]
    assert change.kind is ChangeKind.ADDED_REQUIRED
    assert change.is_secret
    assert change.migration.op == "manual"  # never auto-set a secret
    assert SECRET_VALUE not in json.dumps(change.to_dict())


def test_secret_value_never_appears_in_serialized_report():
    provider = StaticSpecProvider({("airbyte/source-x", "1.0.0"): SPEC})
    detector = DriftDetector(spec_provider=provider)
    conn = StoredConnection(
        workspace_id="ws1",
        connection_id="c1",
        name="conn",
        docker_repository="airbyte/source-x",
        docker_image_tag="1.0.0",
        configuration={"host": "db", "api_key": SECRET_VALUE, "stale_secret": SECRET_VALUE},
    )
    report = detector.check_connection(conn)
    serialized = json.dumps(report.to_dict())
    assert SECRET_VALUE not in serialized


def test_debug_logging_never_logs_secret_values(caplog):
    provider = StaticSpecProvider({("airbyte/source-x", "1.0.0"): SPEC})
    detector = DriftDetector(spec_provider=provider)
    conn = StoredConnection(
        workspace_id="ws1",
        connection_id="c1",
        name="conn",
        docker_repository="airbyte/source-x",
        docker_image_tag="1.0.0",
        configuration={"host": "db", "api_key": SECRET_VALUE},
    )
    with caplog.at_level(logging.DEBUG, logger="airbyte_config_drift"):
        detector.check_connection(conn)
    assert SECRET_VALUE not in caplog.text
