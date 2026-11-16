"""Diff engine tests: added fields, type changes, enums, unknown fields,
backward compatibility, nested objects and oneOf."""

from airbyte_config_drift.diff import diff_config, diff_specs
from airbyte_config_drift.models import ChangeKind, Severity


def make_spec(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def kinds(changes):
    return {c.path: c.kind for c in changes}


class TestAddedFields:
    def test_new_required_field_without_default_is_breaking(self):
        spec = make_spec(
            {"host": {"type": "string"}, "port": {"type": "integer"}},
            required=["host", "port"],
        )
        changes = diff_config(spec, {"host": "db"})
        assert kinds(changes) == {"port": ChangeKind.ADDED_REQUIRED}
        assert changes[0].severity is Severity.BREAKING
        assert changes[0].migration.op == "manual"

    def test_new_required_field_with_default_gets_set_migration(self):
        spec = make_spec(
            {"host": {"type": "string"}, "ssl": {"type": "boolean", "default": True}},
            required=["host", "ssl"],
        )
        changes = diff_config(spec, {"host": "db"})
        assert len(changes) == 1
        change = changes[0]
        assert change.kind is ChangeKind.ADDED_REQUIRED
        assert change.severity is Severity.BREAKING
        assert change.migration.op == "set"
        assert change.migration.value is True

    def test_new_optional_field_is_backward_compatible_info(self):
        spec = make_spec(
            {"host": {"type": "string"}, "tunnel": {"type": "object"}},
            required=["host"],
        )
        changes = diff_config(spec, {"host": "db"})
        assert len(changes) == 1
        assert changes[0].kind is ChangeKind.ADDED_OPTIONAL
        assert changes[0].severity is Severity.INFO

    def test_new_optional_field_with_default_suggests_backfill(self):
        spec = make_spec({"batch": {"type": "integer", "default": 100}})
        changes = diff_config(spec, {})
        assert changes[0].migration.op == "set"
        assert changes[0].migration.value == 100


class TestTypeChanges:
    def test_type_changed_stored_value_no_longer_valid(self):
        spec = make_spec({"port": {"type": "string"}})
        changes = diff_config(spec, {"port": 5432})
        assert kinds(changes) == {"port": ChangeKind.TYPE_CHANGED}
        assert changes[0].severity is Severity.BREAKING

    def test_type_still_valid_is_not_drift(self):
        spec = make_spec({"port": {"type": "integer"}})
        assert diff_config(spec, {"port": 5432}) == []

    def test_bool_is_not_an_integer(self):
        spec = make_spec({"flag": {"type": "integer"}})
        changes = diff_config(spec, {"flag": True})
        assert kinds(changes) == {"flag": ChangeKind.TYPE_CHANGED}

    def test_spec_spec_integer_to_number_is_compatible_widening(self):
        old = make_spec({"ratio": {"type": "integer"}})
        new = make_spec({"ratio": {"type": "number"}})
        changes = diff_specs(old, new)
        assert len(changes) == 1
        assert changes[0].kind is ChangeKind.TYPE_CHANGED
        assert changes[0].severity is Severity.INFO

    def test_spec_spec_number_to_integer_is_breaking_narrowing(self):
        old = make_spec({"ratio": {"type": "number"}})
        new = make_spec({"ratio": {"type": "integer"}})
        changes = diff_specs(old, new)
        assert changes[0].severity is Severity.BREAKING


class TestEnumChanges:
    def test_value_no_longer_allowed_is_breaking(self):
        spec = make_spec({"mode": {"type": "string", "enum": ["a", "b"]}})
        changes = diff_config(spec, {"mode": "c"})
        assert kinds(changes) == {"mode": ChangeKind.ENUM_CHANGED}
        assert changes[0].severity is Severity.BREAKING
        assert changes[0].migration.op == "manual"

    def test_value_still_allowed_is_not_drift(self):
        spec = make_spec({"mode": {"type": "string", "enum": ["a", "b"]}})
        assert diff_config(spec, {"mode": "a"}) == []

    def test_spec_spec_enum_narrowing_is_warning_widening_is_info(self):
        old = make_spec({"m": {"enum": ["a", "b", "c"]}})
        narrowed = make_spec({"m": {"enum": ["a", "b"]}})
        widened = make_spec({"m": {"enum": ["a", "b", "c", "d"]}})
        assert diff_specs(old, narrowed)[0].severity is Severity.WARNING
        assert diff_specs(old, widened)[0].severity is Severity.INFO


class TestDefaultChanges:
    def test_default_change_between_specs_is_warning(self):
        old = make_spec({"retries": {"type": "integer", "default": 3}})
        new = make_spec({"retries": {"type": "integer", "default": 5}})
        changes = diff_specs(old, new)
        assert len(changes) == 1
        assert changes[0].kind is ChangeKind.DEFAULT_CHANGED
        assert changes[0].severity is Severity.WARNING
        assert changes[0].old_value == 3
        assert changes[0].new_value == 5

    def test_unchanged_default_is_not_reported(self):
        old = make_spec({"retries": {"type": "integer", "default": 3}})
        assert diff_specs(old, old) == []


class TestRemovedAndUnknownFields:
    def test_config_key_not_in_spec_is_unknown_field(self):
        spec = make_spec({"host": {"type": "string"}})
        changes = diff_config(spec, {"host": "db", "legacy_flag": True})
        assert kinds(changes) == {"legacy_flag": ChangeKind.UNKNOWN_FIELD}
        assert changes[0].severity is Severity.WARNING
        assert changes[0].migration.op == "remove"

    def test_spec_spec_removed_field(self):
        old = make_spec({"host": {"type": "string"}, "old_opt": {"type": "string"}})
        new = make_spec({"host": {"type": "string"}})
        changes = diff_specs(old, new)
        assert kinds(changes) == {"old_opt": ChangeKind.REMOVED}


class TestNestingAndOneOf:
    def test_nested_object_drift_uses_dotted_paths(self):
        spec = make_spec(
            {
                "tunnel": {
                    "type": "object",
                    "properties": {"host": {"type": "string"}},
                    "required": ["host", "port"],
                }
            }
        )
        changes = diff_config(spec, {"tunnel": {"host": "h"}})
        assert kinds(changes) == {"tunnel.port": ChangeKind.ADDED_REQUIRED}

    def test_one_of_resolved_by_const_discriminator(self):
        spec = make_spec(
            {
                "credentials": {
                    "type": "object",
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "auth_type": {"const": "oauth"},
                                "token": {"type": "string"},
                            },
                            "required": ["auth_type", "token"],
                        },
                        {
                            "type": "object",
                            "properties": {
                                "auth_type": {"const": "basic"},
                                "username": {"type": "string"},
                            },
                            "required": ["auth_type", "username"],
                        },
                    ],
                }
            }
        )
        config = {"credentials": {"auth_type": "basic", "username": "u"}}
        # Without oneOf resolution the oauth branch would flag a missing
        # required "token"; resolving by const avoids the false positive.
        assert diff_config(spec, config) == []

    def test_empty_config_and_spec_produce_no_changes(self):
        assert diff_config(make_spec({}), {}) == []
