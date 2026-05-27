# ais-elastic — project notes

Guidance for AI assistants and human contributors working in this repo. Deployment-specific context for a given operator lives in `CLAUDE.local.md` (gitignored).

## What this is
AIS-to-Elasticsearch (or OpenSearch) bridge. Subscribes to the aisstream.io WebSocket feed for one or more bounding boxes, normalizes each AIS message into an ECS 8.x-compliant doc, and bulk-indexes via a write-alias + rollover strategy (intentionally not a data stream — see the README "Index strategy" section for why). Includes per-message stateless content alerts (distress/MAYDAY, MMSI anomalies, identity-change) with Slack + optional external alert API delivery (bearer-token HTTP POST).

## Files at a glance
- `ais_elastic.py` — main entrypoint; argparse via `build_parser()`, `_compose_eff(args, yaml_cfg)` composes the downstream effective config, `IndexerThread` + ECS doc construction.
- `ecs_builder.py` — turns aisstream messages into ECS 8.x docs.
- `search_client.py` — thin Elasticsearch / OpenSearch backend wrapper.
- `sources/aisstream.py` — aisstream.io WebSocket client (the only currently implemented source).
- `alerts.py` — `AlertEngine`, stateless per-message triggers (distress text, MMSI envelope mismatch, identity change), Slack webhook + external alert API notifiers. Self-contained.
- `service_config.py` — standalone YAML config loader. Search order: `--config` → `<script_dir>/ais-elastic.yaml` → `/etc/ais-elastic/ais-elastic.yaml` → `/etc/default/ais-elastic.yaml`. Precedence: CLI-explicit > YAML > argparse defaults.
- `ais-elastic.example.yaml`, `alert_rules.json.example`, `ais-elastic.service.example` — templates. The corresponding non-`.example` files are gitignored and hold live credentials.
- `capture_samples.py` — developer-only sample-capture utility. Reads `AISSTREAM_API_KEY` directly from env — out of the bridge's config domain.

## Configuration model
Three sources, merged in this order (later wins):
1. argparse defaults
2. YAML file (via `service_config.load_config`, which mutates `args` in-place)
3. CLI flags (a sentinel-clone trick in `service_config._explicitly_set_cli_args` distinguishes "explicitly passed" from "argparse default")

A fourth, lower-precedence source exists for alert credentials only: `alert_rules.json` top-level `slack.*` / `analytics.*`. The `AlertEngine` constructor uses `cli_or_yaml_value or rules_file_value or default`. This works because YAML merge happens before `AlertEngine.__init__`, so the constructor receives the CLI-or-YAML winner and the existing `or` chain naturally yields `rules_file` only as last-resort.

## Project-specific quirks worth knowing
- **`sources:` is a YAML list.** Only `kind: aisstream` is implemented today; the list shape is forward-looking. The `_MAPPING` in `service_config.py` does NOT map `sources`. `ais_elastic.py:_compose_eff()` reads `yaml_cfg["sources"][0]` directly. CLI flags `--bbox`, `--filter-mmsi`, `--filter-message-type`, `--aisstream-insecure` override the corresponding `sources[0]` fields.
- **`observer_coord` is a dict** (`{"lat": float, "lon": float, "alt_m": float}`), not a `"lat,lon,alt_m"` string. `service_config.py` has a custom synthesizer for it that overrides the standard `_synthesize_coord`.
- **Legacy `observer.geo.{lat,lon,alt_m}`** layout is accepted with a one-line INFO deprecation log. New schema is flat `observer.{lat,lon,alt_m}`. Keep the legacy-accept path indefinitely — it's a kindness to older configs.
- **TLS inversion:** YAML `elasticsearch.verify_tls: true` ↔ argparse `no_verify_tls: false`. The inversion lives in `_apply_yaml_to_args`.
- **Env-var configuration is not supported for the bridge.** `AISSTREAM_API_KEY` and `ES_URL` no longer have any effect — `main()` fast-fails with exit 2 + a clear error if either is set without a corresponding YAML value. `capture_samples.py` is the lone exception (developer tool, not in the bridge config path).
- **No `--alerts-disabled` flag.** Alerts are always-on; engine no-ops when no notifiers are configured.
- **Write alias + rollover, not a data stream.** The index strategy is intentional and described in the README. Don't "fix" it to use a data stream — AIS docs include update-in-place semantics for static vessel metadata that don't fit the data-stream model.

## Things to be careful about
- **`--print-config` must redact secrets.** Targets: `elasticsearch.url` (the `user:pass@` portion), `alerts.slack.webhook_url`, `alerts.analytics.token`. Defensive: any leaf key in `{password, token, secret, webhook_url, api_key}`. `sources[].api_key` (the aisstream key) is treated by the leaf-key rule. If you add a new credential field, add it to `_SENSITIVE_KEYS` or `_redact()` in `service_config.py`.
- **`sources:` doesn't appear in `--print-config` output** by design — it's an ais-specific list that lives in the parsed YAML dict (`yaml_cfg`), not in `args`. Operators inspect bounding boxes by viewing the YAML directly. Extend `print_config` to render the sources list (with API keys redacted) if this becomes a friction point.
- **Rollover semantics.** The index alias `ais` points at the current write-target backing index. `--skip-index-setup` exists for environments where the alias + initial index are managed externally. Don't auto-roll without operator action.

## Common verification commands
```bash
# Syntax-check
python3 -c "import ast; ast.parse(open('service_config.py').read()); ast.parse(open('ais_elastic.py').read())"

# Print the effective config (secrets redacted) without running the bridge
python3 ais_elastic.py --print-config

# Verify notifier credentials (Slack + external alert API) without touching ES or aisstream
python3 ais_elastic.py --verify-only

# Print the index template as a manual inspection aid
python3 ais_elastic.py --print-template
```
