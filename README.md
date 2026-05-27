# ais-elastic — AIS to Elasticsearch / OpenSearch Bridge

## Overview

Subscribes to AIS ship-message feeds (currently [aisstream.io](https://aisstream.io)'s
WebSocket push API), converts each message into an ECS 8.17.0 compliant document
with a structured `ais.*` custom namespace, and bulk-indexes them into either
**Elasticsearch** (ILM lifecycle) or **OpenSearch** (ISM lifecycle).

### Features

- **Auto-detect backend** — probes `GET /` on startup to identify Elasticsearch or
  OpenSearch automatically.  No `--backend-type` flag required for typical deployments.
  Pass `--backend-type elasticsearch` or `--backend-type opensearch` to skip the probe.
  Lifecycle policy automatically switches between ILM (ES) and ISM (OS).
- **ECS 8.17.0 compliant** — `observer.*`, `source.*`, `event.*`, `related.*`,
  `ecs.version` populated; vessel position lands in `source.geo.location`.
- **Source abstraction** — `BaseAISSource` thread interface; additional feeds
  (local NMEA, MarineTraffic, etc.) plug in without touching the indexer or
  the ECS builder.
- **Single data stream, polymorphic payload** — every message type indexed
  into the same write alias; `ais.message_type` + `ais.message_class` carry
  the discriminator, per-family fields live under `ais.payload.*`-style
  sub-objects (`ais.aton.*`, `ais.dimensions.*`, `ais.static_report.*`, etc.).
- **MMSI category & flag-state decoded at ingest** — bundled ITU MID table
  resolves MMSI → flag country / ISO code without any external download.
  Distinguishes ship / AtoN / SAR-aircraft / SART / MOB / EPIRB / auxiliary /
  group / handheld / coast-station categories.
- **Distress detection at ingest** — SART/MOB/EPIRB MMSI prefixes, nav-status
  AIS-SART (14), and safety-message text scanning produce a queryable
  `ais.distress.*` sub-object with severity scoring.
- **Single-message anomaly flags** — sentinel detection (lat=91 / lon=181 /
  SOG=102.3 / heading=511), null-island, MMSI category-vs-message-type
  mismatch, suspicious-MMSI-pattern detection (all-same-digit, sequential,
  alternating placeholders), nav-status vs SOG inconsistency, heading-vs-COG
  disagreement.
- **Range / bearing from observer** — geodesic distance and bearing
  precomputed per position-bearing message; supports future DF / COMINT
  / radar fusion via simple keyword/range queries.
- **Region-of-interest tagging** — every message tested against the
  configured bounding boxes at ingest, exposed as `ais.roi.{matched,name}`.
- **Time-of-day decoration** — `ais.time.hour_utc`, `day_of_week_utc`, and
  matching `*_local` fields when `local_timezone` is configured.
- **Index template + lifecycle policy auto-install** on startup; ILM for ES,
  ISM for OpenSearch.  `--skip-index-setup` to disable.
- **`--print-template` / `--print-config`** for offline inspection of the
  shipped template and the effective merged config (secrets redacted).
- **Alerting** — rules-engine that watches each indexed document and posts
  to Slack (incoming webhook) and/or an external alert API (bearer-token
  HTTP POST to a downstream endpoint).  Ships with a `distress` rule
  enabled by default and five anomaly rules disabled-but-documented.
  `--verify-only` to ping notifier credentials without touching aisstream
  or Elasticsearch.  See [Alerting](#alerting) below.

### Index strategy: write alias + rollover (not data streams)

OpenSearch ISM works against indices and aliases — data streams in
OpenSearch are second-class and rotate awkwardly under ISM.  To keep one
code path across both backends, ais-elastic uses **traditional index +
write alias + rollover** on both Elasticsearch and OpenSearch.

## ECS Field Mapping

| ECS Field | Source |
|---|---|
| `@timestamp` | `MetaData.time_utc` from aisstream (the AIS-reported moment) |
| `event.created` | bridge wall clock at document construction |
| `event.module` | `ais` |
| `event.dataset` | `ais.aisstream` (per source) |
| `event.kind` | `event` (or `alert` when distress) |
| `event.severity` | syslog scale (2 critical / 3 high / 4 medium / 5 low) |
| `observer.{name,hostname,vendor,product,type,geo}` | bridge identity + operator-chosen reference-point coords (see "Reference point coordinate" below) |
| `source.geo.location` | vessel / AtoN / SAR-aircraft / base-station position when valid |
| `source.geo.country_name` / `country_iso_code` | from MMSI MID lookup |
| `source.address` | MMSI |
| `source.user.name` | vessel name |
| `related.id` | MMSI, `IMO<num>`, call sign |
| `related.hosts` | MMSI |

## Custom `ais.*` Namespace (top-level)

| Field | Type | Notes |
|---|---|---|
| `ais.message_type` | keyword | `PositionReport`, `ShipStaticData`, etc. |
| `ais.message_id` | byte | ITU-R M.1371 numeric (1/3/5/18/21/24/...) |
| `ais.message_class` | keyword | `position` / `static` / `safety` / `aton` / `sar` / `base_station` / `binary` / `link_layer` / `other` |
| `ais.valid` | boolean | aisstream's per-message validity flag |
| `ais.received_at` | date | when bridge saw the message |
| `ais.lag_s` | float | `received_at - @timestamp` (feed-latency observability) |
| `ais.ship_name` | keyword | trimmed of `@` padding and trailing space |
| `ais.call_sign` | keyword |  |
| `ais.imo` | long |  |
| `ais.imo_info.checksum_valid` | boolean | IMO 7-digit check digit |
| `ais.destination` | keyword |  |
| `ais.eta_utc` | date | best-effort; year inferred |
| `ais.eta_year_assumed` | boolean |  |
| `ais.draught_m` | float |  |
| `ais.ship_type_raw` | short | original 0-99 |
| `ais.ship_type` | keyword | decoded label |
| `ais.ship_type_category` | keyword | rollup: `cargo` / `tanker` / `passenger` / `fishing` / `pleasure` / `military` / `sar` / ... |
| `ais.nav_status_raw` | byte | 0-15 |
| `ais.nav_status` | keyword | decoded label |
| `ais.sog_kt` | float | knots |
| `ais.cog_deg` | float | degrees true |
| `ais.true_heading_deg` | short | (511 sentinel dropped) |
| `ais.rot_dpm` | float | decoded deg/min |
| `ais.tracking_url` | keyword | marinetraffic.com pivot |

### Sub-objects

| Sub-tree | Purpose |
|---|---|
| `ais.mmsi.{value,value_int,category,mid,flag_country,flag_iso_code}` | MMSI breakdown + ITU MID decoded flag-state |
| `ais.position.{valid,accuracy_high,raim,timestamp_s,timestamp_quality,epfd}` | position-quality bits |
| `ais.dimensions.{to_bow_m,to_stern_m,to_port_m,to_starboard_m,length_m,beam_m}` | AIS reference-point offsets + derived overall size |
| `ais.range.{distance_km,distance_nm,distance_mi,bearing_deg,bearing_cardinal}` | geodesic from observer (when both have positions) |
| `ais.time.{hour_utc,day_of_week_utc,hour_local,day_of_week_local}` | time-of-day decoration |
| `ais.source_feed.{kind,vendor,feed_id}` | feed identification |
| `ais.roi.{matched,name}` | ROI bounding-box match |
| `ais.distress.{is_distress,source,severity,keywords_matched,text,addressed_to_mmsi}` | distress / safety flagging |
| `ais.anomaly.{mmsi_*,position_*,sog_*,navstatus_speed_mismatch,heading_cog_disagree,...}` | single-message anomaly flags |
| `ais.vessel.{registry_name,registry_type,imo,length_m,watchlist,fishing,naval,sanctioned}` | enrichment hook for future user-curated vessel DB |
| `ais.aton.{type,type_raw,name,name_extension,off_position,virtual}` | Aid-to-Navigation reports (msg 21) |
| `ais.sar.altitude_m` | SAR-aircraft altitude (msg 9) |
| `ais.base_station.time_utc` | base-station UTC (msg 4) |
| `ais.binary.{dac,fi,addressed_to_mmsi}` | binary-message slot identifiers (payload not decoded) |
| `ais.static_report.{part_number,vendor_id_name,vendor_unit_model_code,vendor_serial_number,mothership_mmsi}` | Class B static (msg 24) |

## Installation

```bash
pip install -r requirements.txt
```

Both `elasticsearch` and `opensearch-py` are listed; only one is imported
at runtime based on `--backend-type`.  Trim the file if your deployment
uses just one backend.

## Quick Start

For first-time installs. For the system-wide systemd setup with optional hardening, see [Deployment](#deployment) below.

```bash
# 1. Get an API key from https://aisstream.io

# 2. Copy the example YAML and fill in your aisstream API key, ES URL,
#    bounding box, and observer coordinate.
cp ais-elastic.example.yaml ais-elastic.yaml
chmod 0600 ais-elastic.yaml         # gitignored; protect the secrets
# edit ais-elastic.yaml

# 3. Sanity-check the merged config (secrets redacted in output)
python3 ais_elastic.py --print-config

# 4. (Optional) test the aisstream feed against your bounding box for 60s.
#    capture_samples.py is a developer-only tool and still reads the env var.
AISSTREAM_API_KEY=<your-key> python3 capture_samples.py \
    --bbox 33.90,-81.00,32.00,-77.50 --duration 60

# 5. Run the bridge — config comes from ais-elastic.yaml
python3 ais_elastic.py

# 6. Install Kibana / OpenSearch Dashboards saved objects
python3 create_dashboards.py \
    --backend-type elasticsearch \
    --server-url http://elastic:elastic@es.host:9200 \
    --dashboards-url http://kibana.host:5601
```

### Configuration

#### YAML config file (recommended)

Copy `ais-elastic.example.yaml` to `ais-elastic.yaml` (gitignored) and fill in
environment-specific values.  Lock the live config file to mode 0640 since it
contains secrets.

**Discovery order** (first match wins):

1. `--config <path>` if explicitly passed — must exist; absence is an error
2. `<script_dir>/ais-elastic.yaml` (next to `ais_elastic.py`)
3. `/etc/ais-elastic/ais-elastic.yaml`
4. `/etc/default/ais-elastic.yaml`

If no file is found the bridge falls through to CLI flags and defaults.

**Required fields:** `elasticsearch.url`, `sources[0].api_key`,
`sources[0].bounding_boxes`.

#### Command-line flags (legacy / override)

CLI flags always override YAML values.  CLI-only operation is fully supported
for scripted or ad-hoc runs, but the YAML file is recommended for persistent
deployments (it supports the full config surface: named bounding boxes, observer
coordinates, flush tuning, alert rules, etc.).

#### Mixed mode

Run with a YAML file for base configuration and override specific keys via CLI:

```bash
python3 ais_elastic.py --server-url https://other-host:9200 --no-verify-tls
```

#### Diagnostics

```bash
# Print effective merged config (secrets redacted) as YAML and exit:
python3 ais_elastic.py --print-config

# Print the index template for manual inspection and exit:
python3 ais_elastic.py --print-template

# Verify notifier credentials (Slack + external alert API) and exit 0 if all pass:
python3 ais_elastic.py --verify-only
```

`--print-config` shows scalar configuration only.  The `sources:` list
(bounding boxes, feed settings) requires viewing the YAML file directly.

#### Migration from previous schema

| Old key | New key | Notes |
|---|---|---|
| `observer.geo.lat/lon/alt_m` | `observer.lat/lon/alt_m` | Legacy nested form still accepted with a deprecation log |
| `elasticsearch.url_env` | removed | Set `elasticsearch.url` directly in the YAML |
| `sources[].api_key_env` | removed | Set `sources[0].api_key` directly in the YAML |
| `alerts.slack_webhook` | `alerts.slack.webhook_url` | Nested under `slack:` sub-key |
| `alerts.analytics_*` | `alerts.analytics.*` | Nested under `analytics:` sub-key |

## Command-line Options

```
usage: ais_elastic.py [-h] [--config CONFIG]
                      [--backend-type {elasticsearch,opensearch}]
                      [--server-url URL] [--username U] [--password P]
                      [--api-key K] [--no-verify-tls]
                      [--index-alias NAME] [--initial-index NAME]
                      [--template-name NAME] [--policy-name NAME]
                      [--skip-index-setup] [--print-template] [--print-config]
                      [--source-kind {aisstream}]
                      [--aisstream-insecure]
                      [--bbox NW_LAT,NW_LON,SE_LAT,SE_LON]
                      [--filter-mmsi MMSI] [--filter-message-type TYPE]
                      [--observer-name NAME]
                      [--observer-coord LAT,LON[,ALT_M]]
                      [--flush-interval SEC] [--flush-batch-size N]
                      [--queue-size N] [--local-timezone TZ]
                      [--static-cache-path PATH]
                      [--alert-rules PATH] [--slack-webhook URL]
                      [--slack-display-name NAME]
                      [--analytics-base-url URL] [--analytics-domain D]
                      [--analytics-token T] [--analytics-insecure]
                      [--verify-only]
```

`--bbox`, `--filter-mmsi`, and `--filter-message-type` are all repeatable.

## Backend selection

Specify `--backend-type elasticsearch` (default) or `--backend-type opensearch`.
The bridge:

- Picks the matching client library (`elasticsearch` vs `opensearchpy`)
- Loads either `ais_elastic_lifecycle_policy.json` (ILM) or
  `ais_elastic_ism_policy.json` (ISM)
- Uses `kbn-xsrf` AND `osd-xsrf` headers in dashboard import so either UI
  accepts the request.

## Index strategy

`ais-elastic` uses **traditional indices + write alias + rollover**:

- The composable template `ais-elastic` matches `ais-*` indices.
- Initial backing index `ais-000001` is created with `is_write_index: true`
  on the alias `ais`.
- Lifecycle policy rolls over at 20 GB / 7 d, deletes at 60 d.

Bulk writes target the write alias.  Rollover increments the suffix
(`ais-000002`, `ais-000003`, ...) automatically.

## Adding a vessel database (`ais.vessel.*`)

A `--vessel-db <CSV-path>` hook is reserved but not yet implemented.  When
shipped, the CSV will be keyed by MMSI with columns:

```
mmsi,name,callsign,imo,type,flag_state,watchlist,fishing,naval,sanctioned,notes
```

Until then, only `ais.mmsi.flag_country` / `flag_iso_code` (from the
bundled ITU MID table) and `ais.mmsi.category` (from MMSI prefix) are
populated as enrichment.  These cover the most common analyst pivots.

## Reference point coordinate

The `observer.geo.{lat,lon,alt_m}` config block (and `--observer-coord` CLI
flag) names an **operator-chosen reference coordinate**, not a physical
receiver.  When the source is a cloud aggregator like aisstream.io, the
ingester has no real antenna on the host — aisstream does the listening.
The reference point is whatever anchor the operator wants `ais.range.*`
distances and bearings computed from:

- AOR centroid / sector center
- Future DF antenna location (for COMINT/DF fusion queries)
- A logical "looking-from" point at the edge of the AOR

The dashboard map labels this point as **"Reference Point"** (green marker).
It does **not** indicate where the bridge process is physically running.

If you later add a local SDR-based AIS receiver as a second source
(`sources/nmea.py`-style), that source can populate its own
`observer.geo.location` for true antenna position, and the map can
render both points.

## Distress workflow

Filter the dashboards with:

```
ais.distress.is_distress: true
```

Severity scale (`event.severity`):

| Value | Meaning | Triggers |
|---|---|---|
| 2 | critical | SART / MOB / EPIRB MMSI; nav-status AIS-SART (14); MAYDAY / SOS / SINKING / ABANDON in safety text |
| 3 | high | PAN-PAN in safety text |
| 4 | medium | SÉCURITÉ in safety text |
| 5 | low | other safety message text |

Real maydays primarily flow on VHF Ch 16 voice / DSC Ch 70 — they do NOT
transit aisstream.io.  This bridge's distress detection is the AIS-side
cross-check ("we got a voice mayday at this bearing; do we see a SART
activation in the same area?").

## Alerting

Distress and anomaly events are also pushed to operator-facing delivery
channels by the alert engine in `alerts.py`.  Every indexed document is
evaluated against the rules in `alert_rules.json`; matches dispatch on
daemon threads so the indexer never blocks on notifier I/O.

Delivery channels:

- **Slack** — POSTs an icon-tagged message to an incoming webhook,
  filtered by minimum severity.  Multiple alerts in a single indexer
  cycle are batched into one Slack post.
- **External alert API** — POSTs each alert individually to a downstream
  HTTP endpoint (e.g. `POST /elasticengine/v1/alerts`) with a bearer-token
  `Authorization` header and an ECS-shaped JSON body.  503
  (UPSTREAM_UNAVAILABLE) is retried with exponential backoff;
  400/401/200-dropped abort without retry.  Per-rule `alert_label` /
  `alert_category` map to ECS `rule.name` / `rule.category`.  The endpoint
  contract is generic — any service that accepts ECS-shaped bearer-auth
  POSTs at `<base_url>/alerts` and `<base_url>/alerts/verify` works.

### Rules file

Copy `alert_rules.json.example` to `alert_rules.json` (gitignored) and
edit.  Rule shape:

```json
{
  "name": "distress",
  "enabled": true,
  "alert_label": "AIS distress / mayday signal",
  "alert_category": "distress",
  "description": "...",
  "conditions": ["distress"],
  "severity": "critical",
  "realert_minutes": 15
}
```

A rule fires when **all** of its `conditions` tokens match.  Cooldowns
are keyed by `(mmsi, rule_name)` so a single vessel cannot flood
notifications; the floor is 60 seconds regardless of what
`realert_minutes` says.

Available condition tokens:

| Token | Matches when |
|---|---|
| `distress` | `ais.distress.is_distress` is true |
| `distress_critical` / `_high` / `_medium` / `_low` | `ais.distress.severity` is 2 / 3 / 4 / 5 |
| `anomaly:<flag>` | `ais.anomaly.<flag>` is truthy (see anomaly flag list below) |

Recognized anomaly flags: `mmsi_invalid_range`, `mmsi_envelope_mismatch`,
`mmsi_suspicious_pattern`, `mmsi_category_message_mismatch`,
`null_island`, `sog_implausible_high`, `navstatus_speed_mismatch`,
`heading_cog_disagree`, `identity_changed`.  Unknown flags log a warning
at rule-load time so misconfiguration shows up immediately rather than
silently never firing.

### Defaults

The shipped example enables only the `distress` rule (severity=critical,
re-alert 15 min).  Five anomaly rules are present but disabled —
`identity_change`, `mmsi_envelope_mismatch`, `implausible_speed`,
`navstatus_speed_mismatch`, `null_island` — because the anomaly signals
on a busy bounding box can be noisy.  Enable individually after you've
watched the signal in Discover and decided it's worth the notification.

### Credentials precedence

For each notifier, credentials resolve in this order: CLI flag → rules
file block → disabled.  Both blocks have an `"enabled"` flag so you can
keep credentials populated but stop sending without deleting them.

```json
"slack":     { "enabled": true, "webhook_url": "...", "display_name": "AIS Monitor",  "min_severity": "warning" },
"analytics": { "enabled": true, "base_url": "...", "domain": "ais", "token": "...", "insecure": false, "min_severity": "info" }
```

### Verifying credentials

Before enabling the systemd unit, ping each notifier's verify endpoint:

```bash
python3 ais_elastic.py --verify-only
```

This constructs the alert engine, calls `verify()` on each notifier
(the external alert API receives a probe at `<base_url>/alerts/verify`;
Slack notifiers have no verify endpoint and are skipped), and exits
0 / 1 based on whether all credentialed channels passed.  Useful for
CI-style deploy gates that should refuse to start the service if the
external alert API token has been rotated and not yet updated in the
rules file.

## Fusion with DF / COMINT / radar

`ais.range.bearing_deg` and `ais.range.distance_km` are precomputed from
the configured observer coordinate.  A future DF/COMINT correlator can
issue queries like

```
ais.range.bearing_deg >= {bearing - beamwidth/2}
AND ais.range.bearing_deg <= {bearing + beamwidth/2}
AND @timestamp >= now-30s
AND ais.range.distance_km <= MAX_DETECTION_KM
```

to enumerate candidate emitters within the DF cone of arrival.

`related.id` and `related.hosts` give a single multi-valued field across
which the fusion engine can pivot on MMSI / IMO / call sign without
knowing the schema.

## Captured-sample utility

`capture_samples.py` is a one-off subscription that dumps a handful of
real envelopes per message type to `sample.json`.  Used during schema
development; safe to delete in production.  `sample.json` is gitignored
because real captures may carry identifiable vessel info.

## Index template / policy files

- `ais_elastic_index_template.json` — composable template (PUT to
  `_index_template/ais-elastic`)
- `ais_elastic_lifecycle_policy.json` — ILM policy (Elasticsearch only)
- `ais_elastic_ism_policy.json` — ISM policy (OpenSearch only)

These are auto-installed by the bridge on startup.  `create_dashboards.py`
can install them manually and also push the Kibana / OSD saved objects.

## Deployment

The shipped unit runs as **root** out-of-the-box.  To harden, see the
"Run as a dedicated user" section below.

```bash
sudo cp ais-elastic.service /etc/systemd/system/

# Place your config at any of the discovery paths.  Recommended:
#   /etc/ais-elastic/ais-elastic.yaml           (system-wide, mode 0640)
#   /opt/sdr/ais/ais-elastic/ais-elastic.yaml   (next to the script)
#   /etc/default/ais-elastic.yaml
sudo mkdir -p /etc/ais-elastic
sudo cp ais-elastic.yaml /etc/ais-elastic/ais-elastic.yaml
sudo chmod 0640 /etc/ais-elastic/ais-elastic.yaml

sudo systemctl daemon-reload
sudo systemctl enable --now ais-elastic
sudo journalctl -u ais-elastic -f
```

### Optional: alerting

Before enabling the unit, copy and fill in the rules file:

```bash
cp alert_rules.json.example alert_rules.json
# edit slack.webhook_url and/or analytics.{base_url,domain,token}, set
# "enabled": true on the channel(s) you want, then verify:
python3 ais_elastic.py --verify-only
# exit 0 = credentials accepted; safe to start the service.
```

`alert_rules.json` is gitignored.  The shipped example enables only the
distress rule; flip individual anomaly rules on as you tune them.

### Optional: run as a dedicated user (hardening)

```bash
sudo useradd -r -s /usr/sbin/nologin ais-elastic
sudo chown -R ais-elastic:ais-elastic /opt/sdr/ais/ais-elastic/
```

Then uncomment the `User=`, `Group=`, and `# Hardening` block (NoNewPrivileges,
ProtectSystem, ProtectHome, PrivateTmp, PrivateDevices, ReadWritePaths) at the
bottom of `ais-elastic.service` and `sudo systemctl daemon-reload && sudo
systemctl restart ais-elastic`.

## Files

| File | Purpose |
|---|---|
| `ais_elastic.py` | Main bridge — config merge, threads, indexer lifecycle |
| `sources/base.py` | `BaseAISSource` thread ABC + record contract |
| `sources/aisstream.py` | aisstream.io WebSocket source w/ reconnect/backoff |
| `ecs_builder.py` | Stateless `record → ECS document` transformation |
| `ais_decoders.py` | Sentinel constants + MID/MMSI/nav-status/ship-type/AtoN decoders + distress detection |
| `search_client.py` | ES / OpenSearch abstraction (mirrors sparrow-droneid pattern) |
| `ais_elastic_index_template.json` | Composable mapping for `ais-*` |
| `ais_elastic_lifecycle_policy.json` | ILM policy (ES) |
| `ais_elastic_ism_policy.json` | ISM policy (OpenSearch) |
| `create_dashboards.py` | Push template + policy + saved-object NDJSON |
| `kibana_saved_objects.ndjson` | Kibana saved objects — Lens viz, Discover searches, the Maps "AIS Vessel Positions" with type-aware icons, and the bound dashboard |
| `opensearch_saved_objects.ndjson` | OpenSearch Dashboards saved objects — same Lens / Discover / dashboard set; Maps panel is omitted because the OpenSearch Dashboards Maps plugin uses a different saved-object schema that diverges across releases. Add the map panel manually in the OSD Maps app pointing to `source.geo.location`. |
| `_build_kibana_ndjson.py` | Regenerator for both NDJSON files (keep in repo so dashboards can be rebuilt) |
| `capture_samples.py` | Quick sample-capture utility (dev-only) |
| `alerts.py` | Alert rules engine + Slack webhook / external alert API notifiers |
| `alert_rules.json.example` | Sample alert rules; live `alert_rules.json` is gitignored |
| `ais-elastic.service.example` | Sample systemd unit; live `ais-elastic.service` is gitignored |
| `run_ais_elastic.sh` / `startup_ais_elastic.sh` | Manual + boot-wrapper run scripts |
| `ais-elastic.example.yaml` | Sample config (real file gitignored) |
| `LICENSE` | GNU General Public License v3 |

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE) for the full text. The project follows the same license as the reference implementation [sparrow-wifi](https://github.com/ghostop14/sparrow-wifi).

### Using ais-elastic as a component in your own pipeline

Running ais-elastic as a standalone service that writes to Elasticsearch / OpenSearch — where your own application then reads from the index — is **aggregation** under GPLv3, not a derivative work. Your application code is **not** obligated to be GPL-licensed in that arrangement. You can ship a commercial product whose architecture includes this bridge as one of its data-ingest services, communicate with it via the search index it populates, and keep your own code under any license you choose.

GPL terms **do** apply when you modify the source, embed code from this project into your own program (copying functions, importing as a Python module that you then redistribute, etc.), or distribute a fork. In those cases the combined or modified work must also be GPLv3.

If you're unsure whether your intended use is aggregation or derivation, the [GNU FAQ on aggregation vs. combination](https://www.gnu.org/licenses/gpl-faq.en.html#MereAggregation) is the authoritative reference.
