"""Diff a stored connection configuration against a connector spec.

The spec is the connector's ``connectionSpecification`` (JSON Schema
draft-07 subset as used by Airbyte connectors). The diff is field-oriented:
it reports added/removed/unknown fields, type changes, default changes and
enum changes, each with a severity and an executable migration suggestion.

Secret values (``airbyte_secret: true`` or credential-like names) are never
copied into changes — only their paths.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from airbyte_config_drift.models import (
    ChangeKind,
    FieldChange,
    MigrationOp,
    Severity,
)
from airbyte_config_drift.secrets import is_secret_field

# JSON Schema type -> python types accepted for it.
_TYPE_CHECKERS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, Mapping),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}

# Widening coercions that are safe (stored value of the left type remains
# valid for the right type).
_COMPATIBLE_WIDENINGS = {("integer", "number")}


def _normalize_types(type_decl: Any) -> list[str]:
    if type_decl is None:
        return []
    if isinstance(type_decl, str):
        return [type_decl]
    if isinstance(type_decl, list):
        return [t for t in type_decl if isinstance(t, str)]
    return []


def _value_matches(value: Any, type_names: list[str]) -> bool:
    if not type_names:
        return True  # no type declared -> anything goes
    return any(_TYPE_CHECKERS.get(t, lambda v: True)(value) for t in type_names)


def _types_compatible(old: list[str], new: list[str]) -> bool:
    """True if every value valid for ``old`` remains valid for ``new``."""
    if not old or not new:
        return True
    for o in old:
        if o in new:
            continue
        if any((o, n) in _COMPATIBLE_WIDENINGS for n in new):
            continue
        return False
    return True


def _join(parent: str, key: str) -> str:
    return f"{parent}.{key}" if parent else key


def _pick_one_of(schema: Mapping[str, Any], config_value: Any) -> Optional[Mapping[str, Any]]:
    """Resolve a oneOf against the stored config value.

    Uses the Airbyte discriminator convention: each option declares a
    property whose ``const`` identifies it. Returns None when no option can
    be selected confidently (callers then skip deep comparison rather than
    guessing — guessing could silently drop config).
    """
    options = schema.get("oneOf")
    if not isinstance(options, list) or not isinstance(config_value, Mapping):
        return None
    for option in options:
        if not isinstance(option, Mapping):
            continue
        props = option.get("properties", {})
        if not isinstance(props, Mapping):
            continue
        for key, subschema in props.items():
            if isinstance(subschema, Mapping) and "const" in subschema:
                if config_value.get(key) == subschema["const"]:
                    return option
    return None


def diff_config(
    spec: Mapping[str, Any],
    config: Mapping[str, Any],
) -> list[FieldChange]:
    """Compute drift between a stored config and the current spec."""
    changes: list[FieldChange] = []
    _diff_object(spec, config, path="", changes=changes)
    return changes


def _diff_object(
    schema: Mapping[str, Any],
    config: Mapping[str, Any],
    path: str,
    changes: list[FieldChange],
) -> None:
    if not isinstance(schema, Mapping) or not isinstance(config, Mapping):
        return

    selected = _pick_one_of(schema, config)
    if selected is not None:
        schema = selected

    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        properties = {}
    required = schema.get("required", [])
    if not isinstance(required, list):
        required = []

    # 1. Fields present in the spec. Required names missing from
    #    ``properties`` (legal in JSON Schema) are checked with an empty
    #    schema node so they are still flagged when absent from the config.
    field_names = list(properties) + [n for n in required if n not in properties]
    for name in field_names:
        field_schema = properties.get(name, {})
        if not isinstance(field_schema, Mapping):
            field_schema = {}
        field_path = _join(path, name)
        secret = is_secret_field(field_schema, name)
        in_config = name in config
        is_required = name in required

        if not in_config:
            if is_required:
                if "default" in field_schema and not secret:
                    default = field_schema["default"]
                    changes.append(
                        FieldChange(
                            kind=ChangeKind.ADDED_REQUIRED,
                            severity=Severity.BREAKING,
                            path=field_path,
                            message=(
                                f"new required field '{field_path}' is missing from the "
                                f"stored config; the spec provides a default"
                            ),
                            is_secret=secret,
                            new_value=default,
                            migration=MigrationOp(
                                op="set",
                                path=field_path,
                                value=default,
                                suggestion=f"set '{field_path}' to the spec default",
                            ),
                        )
                    )
                else:
                    changes.append(
                        FieldChange(
                            kind=ChangeKind.ADDED_REQUIRED,
                            severity=Severity.BREAKING,
                            path=field_path,
                            message=(
                                f"new required field '{field_path}' is missing from the "
                                f"stored config and has no spec default"
                            ),
                            is_secret=secret,
                            migration=MigrationOp(
                                op="manual",
                                path=field_path,
                                suggestion=(
                                    f"provide a value for required field '{field_path}'"
                                    + (" (secret: re-enter it in the UI, do not store it in files)" if secret else "")
                                ),
                            ),
                        )
                    )
            else:
                default = field_schema.get("default") if not secret else None
                has_default = "default" in field_schema and not secret
                changes.append(
                    FieldChange(
                        kind=ChangeKind.ADDED_OPTIONAL,
                        severity=Severity.INFO,
                        path=field_path,
                        message=f"new optional field '{field_path}' added to the spec",
                        is_secret=secret,
                        new_value=default if has_default else None,
                        migration=(
                            MigrationOp(
                                op="set",
                                path=field_path,
                                value=default,
                                suggestion=f"optionally backfill '{field_path}' with the spec default",
                            )
                            if has_default
                            else None
                        ),
                    )
                )
            continue

        value = config[name]

        # 2. Type changes (only meaningful when the stored value no longer
        #    matches the declared type).
        spec_types = _normalize_types(field_schema.get("type"))
        if spec_types and not _value_matches(value, spec_types):
            changes.append(
                FieldChange(
                    kind=ChangeKind.TYPE_CHANGED,
                    severity=Severity.BREAKING,
                    path=field_path,
                    message=(
                        f"stored value for '{field_path}' does not match the spec "
                        f"type {spec_types}"
                    ),
                    is_secret=secret,
                    migration=MigrationOp(
                        op="manual",
                        path=field_path,
                        suggestion=f"convert '{field_path}' to type {spec_types}",
                    ),
                )
            )

        # 3. Enum changes.
        enum_values = field_schema.get("enum")
        if isinstance(enum_values, list) and enum_values and not secret:
            if value not in enum_values:
                changes.append(
                    FieldChange(
                        kind=ChangeKind.ENUM_CHANGED,
                        severity=Severity.BREAKING,
                        path=field_path,
                        message=(
                            f"stored value for '{field_path}' is no longer an allowed "
                            f"enum value"
                        ),
                        old_value=value,
                        new_value=list(enum_values),
                        migration=MigrationOp(
                            op="manual",
                            path=field_path,
                            suggestion=f"pick one of the allowed values for '{field_path}': {enum_values}",
                        ),
                    )
                )

        # 4. Default changes are only observable with a previous spec
        #    snapshot (an explicitly stored value always wins over the
        #    default); see diff_specs().

        # 5. Recurse into nested objects.
        if isinstance(value, Mapping) and (
            "properties" in field_schema or "oneOf" in field_schema
        ):
            _diff_object(field_schema, value, field_path, changes)

    # 6. Unknown/stale config keys (removed from the spec or never known).
    for name in config:
        if name not in properties:
            field_path = _join(path, name)
            secret = is_secret_field(None, name)
            changes.append(
                FieldChange(
                    kind=ChangeKind.UNKNOWN_FIELD,
                    severity=Severity.WARNING,
                    path=field_path,
                    message=(
                        f"stored config key '{field_path}' is not in the current spec "
                        f"(removed or renamed); it is ignored by the connector"
                    ),
                    is_secret=secret,
                    migration=MigrationOp(
                        op="remove",
                        path=field_path,
                        suggestion=(
                            f"remove stale key '{field_path}' from the stored config "
                            f"(review first: it may have been renamed in the spec)"
                        ),
                    ),
                )
            )


def diff_specs(old_spec: Mapping[str, Any], new_spec: Mapping[str, Any], path: str = "") -> list[FieldChange]:
    """Spec-to-spec diff: type/default/enum changes between spec versions.

    Used to classify drift precisely (e.g. distinguish "field removed from
    spec" from "key was never known") when a previous spec snapshot is
    available. Secret values never appear: specs contain defaults, not
    user secrets, but secret fields are still value-stripped defensively.
    """
    changes: list[FieldChange] = []
    old_props = old_spec.get("properties", {}) if isinstance(old_spec, Mapping) else {}
    new_props = new_spec.get("properties", {}) if isinstance(new_spec, Mapping) else {}
    if not isinstance(old_props, Mapping) or not isinstance(new_props, Mapping):
        return changes

    for name in sorted(set(old_props) | set(new_props)):
        old_node = old_props.get(name)
        new_node = new_props.get(name)
        old_node = old_node if isinstance(old_node, Mapping) else None
        new_node = new_node if isinstance(new_node, Mapping) else None
        field_path = _join(path, name)
        secret = is_secret_field(new_node or old_node, name)

        if old_node is not None and new_node is None:
            changes.append(
                FieldChange(
                    kind=ChangeKind.REMOVED,
                    severity=Severity.WARNING,
                    path=field_path,
                    message=f"field '{field_path}' was removed from the spec",
                    is_secret=secret,
                    migration=MigrationOp(
                        op="remove",
                        path=field_path,
                        suggestion=f"remove '{field_path}' from stored configs after verifying it is not renamed",
                    ),
                )
            )
            continue
        if old_node is None and new_node is not None:
            continue  # additions are handled by diff_config against stored configs

        assert old_node is not None and new_node is not None

        old_types = _normalize_types(old_node.get("type"))
        new_types = _normalize_types(new_node.get("type"))
        if old_types and new_types and old_types != new_types:
            compatible = _types_compatible(old_types, new_types)
            changes.append(
                FieldChange(
                    kind=ChangeKind.TYPE_CHANGED,
                    severity=Severity.INFO if compatible else Severity.BREAKING,
                    path=field_path,
                    message=(
                        f"type of '{field_path}' changed from {old_types} to {new_types}"
                        + (" (backward compatible widening)" if compatible else "")
                    ),
                    is_secret=secret,
                )
            )

        old_default = old_node.get("default", _MISSING)
        new_default = new_node.get("default", _MISSING)
        if old_default is not _MISSING and new_default is not _MISSING and old_default != new_default:
            changes.append(
                FieldChange(
                    kind=ChangeKind.DEFAULT_CHANGED,
                    severity=Severity.WARNING,
                    path=field_path,
                    message=(
                        f"default of '{field_path}' changed; configs relying on the "
                        f"default will behave differently"
                    ),
                    is_secret=secret,
                    old_value=None if secret else old_default,
                    new_value=None if secret else new_default,
                )
            )

        old_enum = old_node.get("enum")
        new_enum = new_node.get("enum")
        if isinstance(old_enum, list) and isinstance(new_enum, list) and old_enum != new_enum:
            removed_values = [v for v in old_enum if v not in new_enum]
            severity = Severity.WARNING if removed_values else Severity.INFO
            changes.append(
                FieldChange(
                    kind=ChangeKind.ENUM_CHANGED,
                    severity=severity,
                    path=field_path,
                    message=(
                        f"enum of '{field_path}' changed"
                        + (f"; removed values: {removed_values}" if removed_values else " (widened)")
                    ),
                    is_secret=secret,
                    old_value=None if secret else old_enum,
                    new_value=None if secret else new_enum,
                )
            )

        # Recurse into nested object properties.
        if "properties" in old_node or "properties" in new_node:
            changes.extend(diff_specs(old_node, new_node, field_path))

    return changes


class _Missing:
    pass


_MISSING = _Missing()
