#!/usr/bin/python3

"""ais_elastic — AIS to Elasticsearch / OpenSearch bridge.

Subscribes to an AIS feed (currently aisstream.io's WebSocket push API),
converts each message into an ECS 8.17 document with a structured ais.*
custom namespace, and bulk-indexes them.  Works against either
Elasticsearch (ILM lifecycle) or OpenSearch (ISM lifecycle).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import alerts
import ecs_builder
import search_client as sc
import service_config
from sources.aisstream import AISStreamIOSource

HOSTNAME = platform.node()
DEFAULT_TEMPLATE_NAME = "ais-elastic"
DEFAULT_POLICY_NAME = "ais-elastic"
DEFAULT_ALIAS = "ais"
DEFAULT_INITIAL_INDEX = "ais-000001"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("ais_elastic")

# Quiet the noisy retry-trace warnings from the transport libs during ES/OS
# outages.  Our own wait_for_cluster / indexer backoff loops already report
# connectivity state at WARNING; we don't need the raw HTTP retries too.
for _noisy in ("elastic_transport.transport", "elastic_transport.node_pool",
               "opensearch", "urllib3.connectionpool"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _parse_bbox_cli(arg: str) -> Dict:
    parts = [float(x) for x in arg.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "--bbox must be NW_LAT,NW_LON,SE_LAT,SE_LON")
    nw_lat, nw_lon, se_lat, se_lon = parts
    # aisstream.io rejects boxes where NW isn't actually north-and-west of SE.
    # Catch swapped corners at parse time rather than at first message.
    if nw_lat <= se_lat:
        raise argparse.ArgumentTypeError(
            "--bbox NW latitude must be greater than SE latitude")
    if nw_lon >= se_lon:
        raise argparse.ArgumentTypeError(
            "--bbox NW longitude must be less than SE longitude")
    return {"name": f"bbox-{nw_lat:.2f},{nw_lon:.2f}",
            "nw": [nw_lat, nw_lon],
            "se": [se_lat, se_lon]}


def _parse_coord(arg: str):
    parts = [float(x) for x in arg.split(",")]
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError("--coord must be LAT,LON[,ALT_M]")
    out = {"lat": parts[0], "lon": parts[1]}
    if len(parts) == 3:
        out["alt_m"] = parts[2]
    return out


def build_parser():
    p = argparse.ArgumentParser(
        description="AIS to Elasticsearch / OpenSearch bridge.")
    p.add_argument("--config", help="YAML config file")
    p.add_argument("--backend-type", choices=["auto", "elasticsearch", "opensearch"],
                   default="auto",
                   help="Search backend (default: auto-detect from cluster /). "
                        "Use 'elasticsearch' or 'opensearch' to skip the probe.")
    p.add_argument("--server-url", help="Backend URL (e.g. https://host:9200)")
    p.add_argument("--username", default="")
    p.add_argument("--password", default="")
    p.add_argument("--api-key", default="",
                   help="API key (Elasticsearch only)")
    p.add_argument("--no-verify-tls", action="store_true")
    p.add_argument("--index-alias", default=DEFAULT_ALIAS,
                   help=f"Write alias name (default: {DEFAULT_ALIAS})")
    p.add_argument("--initial-index", default=DEFAULT_INITIAL_INDEX,
                   help=f"Initial backing index (default: {DEFAULT_INITIAL_INDEX})")
    p.add_argument("--template-name", default=DEFAULT_TEMPLATE_NAME)
    p.add_argument("--policy-name", default=DEFAULT_POLICY_NAME)
    p.add_argument("--skip-index-setup", action="store_true")
    p.add_argument("--print-template", action="store_true",
                   help="Print index template and exit")
    p.add_argument("--print-config", action="store_true",
                   help="Print effective config (secrets redacted) and exit")
    p.add_argument("--source-kind", default="aisstream",
                   choices=["aisstream"])
    p.add_argument("--aisstream-insecure", action="store_true", default=None,
                   help="Skip TLS verification on the aisstream.io WebSocket "
                        "connection.  Workaround for aisstream's occasional "
                        "expired-certificate windows; should not be the "
                        "long-term default.")
    p.add_argument("--bbox", action="append", type=_parse_bbox_cli,
                   help="Bounding box NW_LAT,NW_LON,SE_LAT,SE_LON (repeatable)")
    p.add_argument("--filter-mmsi", action="append", default=[],
                   help="Filter to specific MMSI (repeatable)")
    p.add_argument("--filter-message-type", action="append", default=[],
                   help="Filter to message type (repeatable)")
    p.add_argument("--observer-name", default=None,
                   help="Observer/reference-point name (default: YAML "
                        "observer.name, else hostname)")
    p.add_argument("--observer-coord", type=_parse_coord,
                   help="Observer coordinates LAT,LON[,ALT_M] for ais.range.*")
    # Defaults are None so YAML values take precedence when neither side
    # explicitly passes a flag.  Falls back to literal defaults at merge time.
    p.add_argument("--flush-interval", type=float, default=None,
                   help="Bulk flush interval seconds (default 5)")
    p.add_argument("--flush-batch-size", type=int, default=None)
    p.add_argument("--queue-size", type=int, default=None)
    p.add_argument("--local-timezone", default="",
                   help="IANA tz name for ais.time.hour_local (e.g. America/New_York)")
    p.add_argument("--static-cache-path", default=None,
                   help="Path to persist the per-MMSI static-data cache so "
                        "ship-type stamping survives restarts (default: "
                        "<workdir>/static_cache.json; pass empty string to "
                        "disable persistence).")
    # Alerting --------------------------------------------------------------
    p.add_argument("--alert-rules", default=None,
                   help="Path to alert_rules.json (default: <workdir>/"
                        "alert_rules.json; alerting is disabled if absent).")
    p.add_argument("--slack-webhook", default="",
                   help="Slack incoming-webhook URL for alert delivery "
                        "(overrides slack.webhook_url in alert_rules.json).")
    p.add_argument("--slack-display-name", default="",
                   help="Slack username for alert messages "
                        "(default: AIS Monitor).")
    p.add_argument("--analytics-base-url", default="",
                   help="External alert API base URL "
                        "(overrides analytics.base_url in alert_rules.json).")
    p.add_argument("--analytics-domain", default="",
                   help="External alert API domain (overrides analytics.domain).")
    p.add_argument("--analytics-token", default="",
                   help="External alert API bearer token (overrides analytics.token).")
    p.add_argument("--analytics-insecure", action="store_true", default=None,
                   help="Skip TLS verification on external alert API calls.")
    p.add_argument("--verify-only", action="store_true",
                   help="Construct the alert engine, verify each notifier's "
                        "credentials, and exit. Does not connect to Elasticsearch "
                        "or aisstream.io. Exit 0 = all checks passed, 1 = at "
                        "least one check failed.")
    return p


def _compose_eff(args, yaml_cfg: Dict) -> Dict:
    """Build the downstream `eff` dict from the merged args + raw yaml_cfg.

    `args` has already had YAML values folded in by service_config.load_config(),
    so scalar config is read straight from args.  List-shaped `sources` (which
    service_config does not touch) is read from yaml_cfg directly.
    """
    sources_cfg = yaml_cfg.get("sources", []) or []
    aisstream_src = next(
        (s for s in sources_cfg if s.get("kind") == "aisstream"),
        {})

    # Bounding boxes: CLI overrides YAML
    rois: List[Dict] = []
    if args.bbox:
        rois = list(args.bbox)
    elif aisstream_src.get("bounding_boxes"):
        rois = list(aisstream_src["bounding_boxes"])

    # API key lives in the source block (YAML only; env-var fallback removed)
    api_key = aisstream_src.get("api_key") or ""

    # aisstream insecure flag: CLI overrides YAML source block
    aisstream_insecure = (
        args.aisstream_insecure
        if args.aisstream_insecure is not None
        else bool(aisstream_src.get("insecure", False))
    )

    # Observer coord: already merged into args by service_config
    observer_coord = getattr(args, "observer_coord", None) or {}

    # Flush defaults when not set via CLI or YAML
    flush_interval = args.flush_interval if args.flush_interval is not None else 5.0
    flush_batch_size = args.flush_batch_size if args.flush_batch_size is not None else 500
    queue_size = args.queue_size if args.queue_size is not None else 50000

    # static_cache_path default
    static_cache_path = (
        args.static_cache_path
        if args.static_cache_path is not None
        else "static_cache.json"
    )

    # alerts: scalar values already merged into args by service_config
    alert_rules = (
        args.alert_rules
        if args.alert_rules is not None
        else "alert_rules.json"
    )

    return {
        "backend_type":   args.backend_type or "auto",
        "server_url":     args.server_url or "",
        "username":       args.username or "",
        "password":       args.password or "",
        "api_key":        args.api_key or "",
        "verify_tls":     not getattr(args, "no_verify_tls", False),
        "index_alias":    args.index_alias,
        "initial_index":  args.initial_index,
        "template_name":  args.template_name,
        "policy_name":    args.policy_name,
        "skip_index_setup": args.skip_index_setup,
        "aisstream": {
            "api_key":            api_key,
            "bounding_boxes":     rois,
            "feed_id":            aisstream_src.get("name") or "aisstream.io",
            "mmsi_filter":        (args.filter_mmsi
                                   or aisstream_src.get("filter_mmsi", [])),
            "message_type_filter": (args.filter_message_type
                                    or aisstream_src.get("filter_message_types", [])),
            "insecure":           aisstream_insecure,
        },
        "observer": {
            "name":     getattr(args, "observer_name", None) or HOSTNAME,
            "hostname": HOSTNAME,
            "vendor":   "aisstream.io",
            "product":  "wss://stream.aisstream.io/v0/stream",
            "type":     "feed",
            "geo":      observer_coord,
        },
        "flush_interval":    flush_interval,
        "flush_batch_size":  flush_batch_size,
        "queue_size":        queue_size,
        "local_timezone":    args.local_timezone or "",
        "static_cache_path": static_cache_path,
        "alerts": {
            "rules_file":         alert_rules,
            "slack_webhook":      args.slack_webhook or "",
            "slack_display_name": args.slack_display_name or "AIS Monitor",
            "analytics_base_url": args.analytics_base_url or "",
            "analytics_domain":   args.analytics_domain or "",
            "analytics_token":    args.analytics_token or "",
            "analytics_insecure": (args.analytics_insecure
                                   if args.analytics_insecure is not None
                                   else None),
        },
    }


# ---------------------------------------------------------------------------
# Indexer thread
# ---------------------------------------------------------------------------

class IndexerThread(threading.Thread):
    """Drains the source queue, builds ECS docs, bulk-indexes them."""

    def __init__(self, *, client: sc.SearchClient, alias: str,
                 in_queue: "queue.Queue", stop_event: threading.Event,
                 observer: Dict, rois: List[Dict], local_tz,
                 flush_interval_s: float, batch_size: int,
                 alert_engine: Optional["alerts.AlertEngine"] = None):
        super().__init__(name="indexer", daemon=True)
        self._client = client
        self._alias = alias
        self._q = in_queue
        self._stop_event = stop_event
        self._observer = observer
        self._rois = rois
        self._local_tz = local_tz
        self._flush_interval = flush_interval_s
        self._batch_size = batch_size
        self._alert_engine = alert_engine
        self.docs_indexed = 0
        self.docs_failed = 0
        self.batches_flushed = 0
        self.alerts_fired = 0

    def _drain(self) -> List[Dict]:
        batch: List[Dict] = []
        deadline = time.monotonic() + self._flush_interval
        while not self._stop_event.is_set() and len(batch) < self._batch_size:
            timeout = max(0.05, deadline - time.monotonic())
            try:
                rec = self._q.get(timeout=timeout)
            except queue.Empty:
                break
            try:
                doc = ecs_builder.build_document(
                    rec, self._observer, self._rois, self._local_tz)
                batch.append(doc)
            except Exception as exc:
                self.docs_failed += 1
                log.warning("ecs build failed: %s", exc)
                continue
            if self._alert_engine is not None:
                # Alert evaluation is cheap (dict lookups + cooldown check) and
                # notifier dispatch is fire-and-forget on daemon threads, so
                # running it inline on the indexer thread doesn't slow indexing.
                try:
                    fired = self._alert_engine.evaluate(doc, time.monotonic())
                    if fired:
                        self.alerts_fired += len(fired)
                except Exception:
                    log.exception("alert evaluation failed for one document")
            if time.monotonic() >= deadline:
                break
        return batch

    def run(self):
        # bulk_backoff escalates on transport failures so the indexer doesn't
        # hammer a downed cluster.  The source thread keeps producing into the
        # bounded queue throughout; drop-oldest absorbs the outage (user has
        # accepted data loss during backend downtime).
        bulk_backoff = 0.0
        while not self._stop_event.is_set():
            if bulk_backoff > 0 and self._stop_event.wait(bulk_backoff):
                return
            batch = self._drain()
            if not batch:
                continue
            actions = list(sc.build_bulk_actions(batch, self._alias))
            try:
                success, errors = self._client.bulk(actions)
                bulk_backoff = 0.0
                self.docs_indexed += success
                self.docs_failed += len(errors)
                self.batches_flushed += 1
                msg = (f"flushed {success} doc(s) "
                       f"(total indexed {self.docs_indexed}, "
                       f"failed {self.docs_failed})")
                if errors:
                    log.warning("%s; %d index errors (sample: %s)",
                                msg, len(errors),
                                json.dumps(errors[0], default=str)[:300])
                else:
                    log.info(msg)
            except Exception as exc:
                self.docs_failed += len(batch)
                bulk_backoff = min(max(bulk_backoff * 2, 1.0), 60.0)
                log.warning("bulk indexing failed (%d docs lost); "
                            "backing off %.1fs: %s",
                            len(batch), bulk_backoff, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Dict:
    return json.loads(path.read_text())


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    script_dir = str(Path(__file__).resolve().parent)
    yaml_cfg = service_config.load_config(
        args, project_name="ais-elastic",
        script_dir=script_dir, argparser=parser, argv=argv)

    here = Path(script_dir)
    template_body = _load_json(here / "ais_elastic_index_template.json")

    if args.backend_type == "opensearch":
        policy_body = _load_json(here / "ais_elastic_ism_policy.json")
    else:
        policy_body = _load_json(here / "ais_elastic_lifecycle_policy.json")

    if args.print_template:
        # Emit a Kibana Dev Tools-friendly PUT for the template, with ILM
        # settings baked in so a manual install via Dev Tools matches what
        # the bridge would install on Elasticsearch.
        # Use _compose_eff to get the effective backend/alias values.
        eff_tmp = _compose_eff(args, yaml_cfg)
        # For --print-template, treat 'auto' as 'elasticsearch' (prints ILM settings).
        # Use --backend-type opensearch explicitly to print the IS-policy-compatible form.
        if eff_tmp["backend_type"] in ("elasticsearch", "auto"):
            settings = template_body.setdefault("template", {}).setdefault("settings", {})
            settings["index.lifecycle.name"] = eff_tmp["policy_name"]
            settings["index.lifecycle.rollover_alias"] = eff_tmp["index_alias"]
        print(f"PUT _index_template/{eff_tmp['template_name']}")
        print(json.dumps(template_body, indent=2))
        return 0

    if args.print_config:
        service_config.print_config(args, redacted=True)
        return 0

    # Env-var fast-fail guards: these were removed to avoid silent surprises
    # when a user has a stale habit of setting these vars.
    if not args.server_url and os.environ.get("ES_URL"):
        log.error(
            "ES_URL env-var fallback was removed; "
            "set elasticsearch.url in ais-elastic.yaml.")
        return 2
    if os.environ.get("AISSTREAM_API_KEY"):
        srcs = yaml_cfg.get("sources") or []
        if not (srcs and srcs[0].get("api_key")):
            log.error(
                "AISSTREAM_API_KEY env-var fallback was removed; "
                "set sources[0].api_key in ais-elastic.yaml.")
            return 2

    eff = _compose_eff(args, yaml_cfg)

    if args.verify_only:
        # Construct the alert engine, ping each notifier's credentials, exit.
        # Does not touch ES or aisstream — for deploy-time sanity checks that
        # the rules file + analytics token are valid before enabling the unit.
        alerts_cfg = eff["alerts"]
        rules_path = alerts_cfg["rules_file"]
        if rules_path and not os.path.isabs(rules_path):
            rules_path = str(here / rules_path)
        try:
            engine = alerts.AlertEngine(
                rules_file=rules_path,
                slack_webhook_url=alerts_cfg["slack_webhook"],
                slack_display_name=alerts_cfg["slack_display_name"],
                analytics_base_url=alerts_cfg["analytics_base_url"],
                analytics_domain=alerts_cfg["analytics_domain"],
                analytics_token=alerts_cfg["analytics_token"],
                analytics_insecure=alerts_cfg["analytics_insecure"],
                observer=eff["observer"],
            )
        except Exception:
            log.exception("[verify] Failed to construct AlertEngine")
            return 1
        notifiers = engine._notifiers
        if not notifiers:
            log.warning("[verify] No notifiers configured — nothing to verify.")
            return 0
        log.info("[verify] %d notifier(s) loaded: %s",
                 len(notifiers), ", ".join(type(n).__name__ for n in notifiers))
        failed = 0
        for n in notifiers:
            vfn = getattr(n, "verify", None)
            if not callable(vfn):
                log.info("[verify] %s: skipped (no verify method)",
                         type(n).__name__)
                continue
            if vfn():
                log.info("[verify] %s: OK", type(n).__name__)
            else:
                log.error("[verify] %s: FAILED", type(n).__name__)
                failed += 1
        if failed:
            log.error("[verify] %d check(s) failed", failed)
            return 1
        log.info("[verify] All checks passed.")
        return 0

    # Validation
    if not eff["server_url"]:
        log.error("server URL is required (--server-url or YAML elasticsearch.url)")
        return 2
    if not eff["aisstream"]["bounding_boxes"]:
        log.error("at least one bounding box is required (--bbox or YAML)")
        return 2
    if not eff["aisstream"]["api_key"]:
        log.error("aisstream API key is required; "
                  "set sources[0].api_key in ais-elastic.yaml")
        return 2

    local_tz = None
    if eff["local_timezone"]:
        try:
            import zoneinfo
            local_tz = zoneinfo.ZoneInfo(eff["local_timezone"])
        except Exception as exc:
            log.warning("could not load timezone %s: %s",
                        eff["local_timezone"], exc)

    # Persistent static-data cache.  Empty string disables; otherwise load
    # the snapshot (if any) so vessel type-stamping survives restarts.
    cache_path = eff["static_cache_path"] or None
    ecs_builder.init_static_cache(cache_path)
    if cache_path:
        log.info("static-data cache: %s (entries loaded: %d)",
                 cache_path, ecs_builder.static_cache_size())

    stop_event = threading.Event()

    def shutdown(signum, _frame):
        log.info("signal %d received; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    resolved_backend = sc.resolve_backend(
        eff["backend_type"], eff["server_url"],
        username=eff["username"], password=eff["password"],
        api_key=eff["api_key"], verify_tls=eff["verify_tls"],
    )
    if resolved_backend == "opensearch" and eff["api_key"]:
        log.info("api_key set but backend is opensearch; "
                 "OpenSearch does not support ES API-key auth — "
                 "use username/password instead.")
    client = sc.make_client(
        backend=resolved_backend, url=eff["server_url"],
        username=eff["username"], password=eff["password"],
        api_key=eff["api_key"], verify_tls=eff["verify_tls"],
    )

    # Survive backend outages at startup (boot before ES is up, overnight
    # maintenance, etc.).  Block here until the cluster responds, then proceed.
    if not sc.wait_for_cluster(client, stop_event):
        log.info("shutdown requested before %s cluster came up",
                 resolved_backend)
        client.close()
        return 0

    try:
        info = client.cluster_info()
        log.info("connected to %s cluster: name=%s version=%s",
                 resolved_backend, info.get("cluster_name"),
                 (info.get("version") or {}).get("number"))
    except Exception as exc:
        log.warning("could not read cluster info: %s", exc)

    if not eff["skip_index_setup"]:
        sc.ensure_index_setup(
            client,
            template_name=eff["template_name"], template_body=template_body,
            policy_name=eff["policy_name"], policy_body=policy_body,
            alias=eff["index_alias"], initial_index=eff["initial_index"],
        )

    # Convert YAML-shape bboxes -> aisstream subscription shape
    sub_bboxes = []
    for r in eff["aisstream"]["bounding_boxes"]:
        sub_bboxes.append([r["nw"], r["se"]])

    q: "queue.Queue" = queue.Queue(maxsize=eff["queue_size"])

    # Resolve a relative rules-file path against the script directory so the
    # default works regardless of $PWD (matches how YAML / template files are
    # discovered above).
    alerts_cfg = eff["alerts"]
    rules_path = alerts_cfg["rules_file"]
    if rules_path and not os.path.isabs(rules_path):
        rules_path = str(here / rules_path)
    alert_engine = alerts.AlertEngine(
        rules_file=rules_path,
        slack_webhook_url=alerts_cfg["slack_webhook"],
        slack_display_name=alerts_cfg["slack_display_name"],
        analytics_base_url=alerts_cfg["analytics_base_url"],
        analytics_domain=alerts_cfg["analytics_domain"],
        analytics_token=alerts_cfg["analytics_token"],
        analytics_insecure=alerts_cfg["analytics_insecure"],
        observer=eff["observer"],
    )

    source = AISStreamIOSource(
        name="aisstream", out_queue=q, stop_event=stop_event,
        api_key=eff["aisstream"]["api_key"],
        bounding_boxes=sub_bboxes,
        mmsi_filter=eff["aisstream"]["mmsi_filter"],
        message_type_filter=eff["aisstream"]["message_type_filter"],
        feed_id=eff["aisstream"]["feed_id"],
        insecure=eff["aisstream"]["insecure"],
    )
    if eff["aisstream"]["insecure"]:
        log.warning("aisstream WebSocket TLS verification DISABLED "
                    "(--aisstream-insecure / YAML sources[].insecure=true). "
                    "Workaround for an expired-cert window; "
                    "re-enable verification once aisstream renews.")
    indexer = IndexerThread(
        client=client, alias=eff["index_alias"], in_queue=q,
        stop_event=stop_event, observer=eff["observer"],
        rois=eff["aisstream"]["bounding_boxes"], local_tz=local_tz,
        flush_interval_s=eff["flush_interval"],
        batch_size=eff["flush_batch_size"],
        alert_engine=alert_engine,
    )

    source.start()
    indexer.start()

    # Status heartbeat — use Event.wait so signals wake the main thread
    # immediately rather than waiting up to the full 60s.  Surface a
    # WARNING when the queue is dropping messages (aisstream.io docs say
    # they may close us if we're a slow consumer; drops are the leading
    # indicator).  The heartbeat also handles the periodic static-cache
    # snapshot.
    last_dropped = 0
    try:
        while not stop_event.wait(60):
            dropped_delta = source.messages_dropped - last_dropped
            last_dropped = source.messages_dropped
            log_fn = log.warning if dropped_delta > 0 else log.info
            log_fn("status: rx=%d dropped=%d(+%d) indexed=%d failed=%d "
                   "qsize=%d alerts=%d",
                   source.messages_received, source.messages_dropped,
                   dropped_delta, indexer.docs_indexed, indexer.docs_failed,
                   q.qsize(), indexer.alerts_fired)
            try:
                wrote = ecs_builder.save_static_cache()
                if wrote is not None:
                    log.info("static-data cache snapshot: %d entries", wrote)
            except Exception as exc:
                log.warning("static-data cache save failed: %s", exc)
    except KeyboardInterrupt:
        stop_event.set()

    source.join(timeout=10)
    indexer.join(timeout=30)
    try:
        wrote = ecs_builder.save_static_cache()
        if wrote is not None:
            log.info("static-data cache flushed on shutdown: %d entries", wrote)
    except Exception as exc:
        log.warning("final static-data cache save failed: %s", exc)
    client.close()
    log.info("clean shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
