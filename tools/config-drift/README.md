# airbyte-config-drift

Connector configuration drift detection for Airbyte connections.

After a connector upgrade, a stored connection configuration can silently
diverge from the connector's new spec: newly added required fields, type
changes, changed defaults or narrowed enums only surface when the next sync
fails. This tool compares each stored connection configuration against the
connector's current `connectionSpecification` and reports the drift
*before* it breaks a sync.

## What it detects

| Drift | Kind | Severity |
|---|---|---|
| New required field missing from the stored config | `added_required` | BREAKING |
| New optional field | `added_optional` | INFO (backward compatible) |
| Stored value no longer matches the spec type | `type_changed` | BREAKING |
| Type widened compatibly (e.g. integer → number) | `type_changed` | INFO |
| Stored value no longer in the enum | `enum_changed` | BREAKING |
| Enum narrowed / widened between spec versions | `enum_changed` | WARNING / INFO |
| Default changed between spec versions | `default_changed` | WARNING |
| Config key not in the spec (removed/renamed) | `unknown_field` / `removed` | WARNING |

Nested objects are compared recursively (dotted paths); `oneOf` blocks are
resolved via the Airbyte `const` discriminator convention. Every change
carries an executable migration suggestion (`set` / `remove` / `manual`).

## Guarantees

- **Default behavior unchanged**: detection is advisory. Nothing is modified
  and the exit code is 0 unless `--enforce` is passed.
- **Fail safe on spec-fetch failure**: the connection is marked `BLOCKED`
  and its configuration is never rewritten or dropped. With `--enforce`
  this blocks destructive syncs (exit 1) instead of silently proceeding.
- **Secrets never logged**: fields marked `airbyte_secret: true` (and
  credential-like names, as defense in depth) are masked in all reports and
  logs; secret migrations are always `manual` — the tool never materializes
  a secret value.
- **Bounded spec cache**: specs are cached per `(repository, tag)` with a
  TTL, an LRU entry cap and a per-spec size cap — a spec is never pulled on
  every sync, and an oversized/broken spec is refused rather than cached.
- **Multi-workspace**: every workspace in the config store is checked.

## Usage

```bash
# Dry-run (default): print the drift report and migration plan, change nothing.
python -m airbyte_config_drift --configs connections.json

# Gate a sync pipeline: exit 1 when any connection is BREAKING or BLOCKED.
python -m airbyte_config_drift --configs connections.json --enforce

# Apply the safe migration ops (default backfills, stale-key removals) to a
# NEW file; manual steps are listed but never executed.
python -m airbyte_config_drift --configs connections.json --apply migrated.json
```

`connections.json` shape:

```json
{"workspaces": [{"id": "ws-1", "connections": [{
  "id": "conn-1", "name": "pg → warehouse",
  "docker_repository": "airbyte/source-postgres",
  "docker_image_tag": "3.6.0",
  "configuration": {"host": "db", "port": 5432}
}]}]}
```

Specs are fetched with `docker run --rm <image> spec` (timeout and output
size capped). For offline analysis or platform integration, inject any
`SpecProvider` / `ConfigStore` implementation — see
`airbyte_config_drift.spec_provider` and `airbyte_config_drift.store`.

## Tests

```bash
cd tools/config-drift
python -m pytest
```
