"""Secret handling.

A field is secret when the spec marks it with ``airbyte_secret: true``. As a
defensive fallback (e.g. for unknown config keys that no longer exist in the
spec, where no schema node is available), common credential-like names are
also treated as secret.

Nothing in this module ever returns a secret value; it only classifies
paths and masks structures for logging.
"""

from __future__ import annotations

from typing import Any, Mapping

SECRET_MARKER = "airbyte_secret"

# Substrings that, when present in a field name, mark it as secret even
# without a schema node (defense in depth for unknown/removed fields).
_SECRET_NAME_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "client_secret",
    "access_key",
    "credentials",
    "auth",
)

MASK = "***"


def name_looks_secret(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _SECRET_NAME_HINTS)


def is_secret_field(schema_node: Mapping[str, Any] | None, field_name: str) -> bool:
    if schema_node is not None and schema_node.get(SECRET_MARKER) is True:
        return True
    return name_looks_secret(field_name)


def mask_config(config: Any, schema_node: Mapping[str, Any] | None = None, _is_secret: bool = False) -> Any:
    """Return a copy of ``config`` with secret values replaced by ``MASK``.

    Safe to log or embed in reports. Never raises on unexpected shapes.
    """
    if _is_secret:
        return MASK
    if isinstance(config, Mapping):
        properties = (schema_node or {}).get("properties", {}) if isinstance(schema_node, Mapping) else {}
        masked = {}
        for key, value in config.items():
            child_schema = properties.get(key) if isinstance(properties, Mapping) else None
            child_secret = is_secret_field(child_schema if isinstance(child_schema, Mapping) else None, str(key))
            masked[key] = mask_config(value, child_schema, child_secret)
        return masked
    if isinstance(config, list):
        items_schema = (schema_node or {}).get("items") if isinstance(schema_node, Mapping) else None
        return [mask_config(v, items_schema if isinstance(items_schema, Mapping) else None) for v in config]
    return config
