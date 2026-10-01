#!/usr/bin/env python3
"""ais-elastic alerting: distress / anomaly rule engine with Slack + external alert API delivery.

JSON rules file, severity-filtered notifiers running on daemon threads,
bearer-token POSTs to an external alert API endpoint.

The engine consumes already-built ECS documents from ecs_builder.build_document
— it reads ais.distress.* and ais.anomaly.* directly rather than re-deriving
them.  That keeps the detection logic in one place (ecs_builder) and lets the
engine focus on policy: which signals matter, at what severity, with what
cooldown.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

import ais_decoders as dec

logger = logging.getLogger("ais_elastic.alerts")

_SEVERITY_ORDER: Dict[str, int] = {"info": 0, "warning": 1, "critical": 2}

# Operator-facing labels for distress detection sources (set by
# ecs_builder.detect_distress).  Used in both the one-line summary and
# the Slack block so the console reads in plain English instead of
# implementation-internal source codes.
_DISTRESS_SOURCE_LABELS: Dict[str, str] = {
    "sart_mmsi":       "SART distress beacon (MMSI 970-prefix)",
    "mob_mmsi":        "Man-Overboard beacon (MMSI 972-prefix)",
    "epirb_mmsi":      "EPIRB distress beacon (MMSI 974-prefix)",
    "navstatus_sart":  "AIS-SART active (navigational-status 14)",
    "safety_text":     "Safety-broadcast message with distress keyword",
}

# Operator-facing labels for anomaly flags (set by ecs_builder._detect_anomalies).
_ANOMALY_LABELS: Dict[str, str] = {
    "mmsi_invalid_range":             "MMSI outside any valid ITU range",
    "mmsi_envelope_mismatch":         "MMSI envelope / body mismatch",
    "mmsi_suspicious_pattern":        "Placeholder MMSI pattern (all-same / sequential)",
    "mmsi_category_message_mismatch": "MMSI category does not match this message type",
    "null_island":                    "Position equals (0, 0)",
    "sog_implausible_high":           "Speed > 60 kt — implausible for a surface vessel",
    "navstatus_speed_mismatch":       "Reports moored/anchored but moving over 3 kt",
    "heading_cog_disagree":           "Heading vs course-over-ground disagree by > 45°",
    "identity_changed":               "Ship name / call sign / IMO changed between static broadcasts",
}

# Mapped to ECS severity numerics for the Analytics API (lower = more urgent).
_ANALYTICS_SEVERITY_MAP: Dict[str, int] = {
    "critical": 10,
    "warning":  40,
    "info":     70,
}


# ---------------------------------------------------------------------------
# Alert dataclass
# ---------------------------------------------------------------------------

@dataclass
class Alert:
    """A single triggered AIS alert.  Slot of the rule that fired plus
    enough vessel/event context for the notifier payloads.
    """
    rule_name: str
    event: str
    mmsi: str
    severity: str = "warning"
    message: str = ""
    details: Dict = field(default_factory=dict)
    # Vessel position at trigger time when known (distress beacons broadcast
    # without position are valid — None passes through).
    source_lat: Optional[float] = None
    source_lon: Optional[float] = None
    # Per-rule overrides for ECS rule.name / rule.category in the analytics payload.
    alert_label: Optional[str] = None
    alert_category: Optional[str] = None
    # External vessel-page deep-link (ais_decoders.vessel_tracking_link) and
    # the site it points at, for the link label.  Shared by the Slack and
    # analytics notifiers so the two link forms can't drift.
    tracking_url: Optional[str] = None
    tracking_site: Optional[str] = None


# ---------------------------------------------------------------------------
# Notifier base
# ---------------------------------------------------------------------------

class Notifier:
    """Filter alerts by minimum severity and dispatch on a daemon thread."""

    def __init__(self, display_name: str, min_severity: str = "info") -> None:
        self._display_name = display_name
        self._min_severity_order = _SEVERITY_ORDER.get(min_severity, 0)

    def matches(self, alert: Alert) -> bool:
        return _SEVERITY_ORDER.get(alert.severity, 0) >= self._min_severity_order

    def send(self, alerts: List[Alert]) -> None:
        filtered = [a for a in alerts if self.matches(a)]
        if not filtered:
            return
        payload = self._build_payload(filtered)

        def _worker():
            self._do_post(payload)

        t = threading.Thread(target=_worker, daemon=True, name="ais-alert-notifier")
        t.start()

    def _build_payload(self, alerts: List[Alert]) -> dict:
        del alerts
        raise NotImplementedError

    def _do_post(self, payload: dict) -> None:
        del payload
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

class SlackNotifier(Notifier):
    """Post AIS alerts to a Slack incoming webhook."""

    def __init__(self,
                 webhook_url: str,
                 display_name: str = "AIS Monitor",
                 min_severity: str = "warning") -> None:
        super().__init__(display_name, min_severity)
        self._webhook_url = webhook_url

    def _build_payload(self, alerts: List[Alert]) -> dict:
        has_alarm = any(a.severity in ("warning", "critical") for a in alerts)
        if has_alarm:
            header = f":rotating_light: AIS Alert — {self._display_name}"
        else:
            header = f":ship: AIS Status — {self._display_name}"

        blocks = [f"*{header}*"]
        for a in alerts:
            blocks.append(self._format_alert_block(a))

        # `unfurl_links: False` suppresses Slack's automatic preview cards for
        # the vessel-tracker links in each alert block — those would otherwise
        # clutter the channel with full vessel-detail panels.
        return {
            "username": self._display_name,
            "text": "\n\n".join(blocks),
            "unfurl_links": False,
            "unfurl_media": False,
        }

    @staticmethod
    def _format_alert_block(a: Alert) -> str:
        """Render one Alert as a multi-line Slack block for the console operator.

        Order is what a watch-floor reader scans for: severity icon + plain
        headline, vessel identity, position + range, then trigger detail and
        any message text.  The single-line `Alert.message` is intentionally
        not used here — it's the log/analytics fallback.
        """
        if a.severity == "critical":
            icon = ":red_circle:"
        elif a.severity == "warning":
            icon = ":warning:"
        else:
            icon = ":large_blue_circle:"

        d = a.details or {}
        headline = a.alert_label or a.rule_name
        lines = [f"{icon} *{headline}*"]

        identity_bits: List[str] = []
        ship_name = d.get("ship_name")
        if ship_name:
            identity_bits.append(ship_name)
        identity_bits.append(f"MMSI {a.mmsi}")
        flag = d.get("flag_country")
        if flag:
            identity_bits.append(f"{flag} flag")
        lines.append("Vessel: " + " / ".join(identity_bits))

        ship_type = d.get("ship_type")
        if ship_type:
            lines.append(f"Type: {ship_type}")

        if a.source_lat is not None and a.source_lon is not None:
            lines.append(f"Position: {a.source_lat:.4f}, {a.source_lon:.4f}")
        rng = d.get("range_mi")
        brg = d.get("bearing_deg")
        if rng is not None and brg is not None:
            card = d.get("bearing_cardinal")
            brg_str = f"{brg:.0f}°{(' ' + card) if card else ''}"
            lines.append(f"Range: {rng:.1f} mi at {brg_str} from observer")

        # Vessel-tracker deep-link. Tap-friendly on mobile; Slack renders
        # <url|text> as a clickable link. Same deep-link is sent to the
        # analytics alert API.
        if a.tracking_url:
            lines.append(f"<{a.tracking_url}|:ship: Track on {a.tracking_site}>")

        # Trigger detail per condition.  Distress is the most operator-facing
        # so it gets explicit source + keywords + text snippet; anomaly rules
        # render their human label and any structured value.
        conditions = d.get("conditions") or {}
        text_snippet = None
        trigger_lines: List[str] = []
        for cond, meta in conditions.items():
            if cond == "distress":
                src_key = meta.get("source") or ""
                src_label = _DISTRESS_SOURCE_LABELS.get(
                    src_key, src_key or "distress")
                trigger_lines.append(f"Trigger: {src_label}")
                kws = meta.get("keywords_matched") or []
                if kws:
                    trigger_lines.append(f"Keywords: {', '.join(kws)}")
                text_snippet = meta.get("text")
            elif cond.startswith("anomaly:"):
                flag = cond.split(":", 1)[1]
                trigger_lines.append(
                    f"Trigger: {_ANOMALY_LABELS.get(flag, flag)}")
        lines.extend(trigger_lines)

        if text_snippet:
            snippet = text_snippet[:240].replace("`", "'").replace("\n", " ")
            lines.append(f"Text: \"{snippet}\"")

        return "\n".join(lines)

    def _do_post(self, payload: dict) -> None:
        try:
            resp = requests.post(self._webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception:
            logger.exception("Slack webhook post failed")


# ---------------------------------------------------------------------------
# Analytics API
# ---------------------------------------------------------------------------

class AnalyticsAlertNotifier(Notifier):
    """POST each alert individually to /elasticengine/v1/alerts with bearer auth.

    Retry policy follows the UPSTREAM_UNAVAILABLE contract: 503 → exponential
    backoff; 400/401/200-dropped → abort without retry.
    """

    def __init__(self,
                 base_url: str,
                 domain: str,
                 token: str,
                 display_name: str = "AIS Monitor",
                 min_severity: str = "info",
                 verify_tls: bool = True,
                 max_retries: int = 5,
                 backoff_base: float = 1.0,
                 backoff_factor: float = 2.0,
                 observer: Optional[dict] = None) -> None:
        super().__init__(display_name, min_severity)
        self._base_url = base_url.rstrip("/")
        self._domain = domain
        self._token = token
        self._verify_tls = verify_tls
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_factor = backoff_factor
        self._observer = observer or {}
        if not verify_tls:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass

    def send(self, alerts: List[Alert]) -> None:
        filtered = [a for a in alerts if self.matches(a)]
        if not filtered:
            return
        for alert in filtered:
            payload = self._build_alert_payload(alert)
            t = threading.Thread(
                target=self._post_with_retry, args=(payload, alert),
                daemon=True, name="ais-alert-analytics",
            )
            t.start()

    def _build_alert_payload(self, alert: Alert) -> dict:
        observer: dict = {
            "name": self._observer.get("name") or self._display_name,
            "type": self._observer.get("type") or "ais-feed",
        }
        geo = (self._observer.get("geo") or {})
        if geo.get("lat") is not None and geo.get("lon") is not None:
            observer["geo"] = {"location": {
                "lat": float(geo["lat"]), "lon": float(geo["lon"])}}

        rule_name = alert.alert_label or f"{alert.rule_name}:{alert.event}"
        rule_category = alert.alert_category or alert.rule_name

        body: dict = {
            "domain": self._domain,
            "alert": {
                "message": alert.message,
                "observer": observer,
                "rule": {"name": rule_name, "category": rule_category},
                "event": {
                    "severity": _ANALYTICS_SEVERITY_MAP.get(alert.severity, 70),
                    "category": "network",
                    "action": alert.event,
                },
                "labels": {"mmsi": alert.mmsi},
                "ais_details": alert.details,
            },
        }
        if alert.source_lat is not None and alert.source_lon is not None:
            body["alert"]["source"] = {"geo": {"location": {
                "lat": float(alert.source_lat), "lon": float(alert.source_lon)}}}

        # Optional external-info deep-link. elk-ui renders this as a one-click
        # "open external" button in its alert panels; the server sanitizes the
        # URL (https/http only) before storing. Same link the Slack message uses.
        if alert.tracking_url:
            body["alert"]["external_link"] = {
                "url": alert.tracking_url,
                "label": f"Track on {alert.tracking_site}",
            }
        return body

    def _post_with_retry(self, payload: dict, alert: Alert) -> None:
        url = f"{self._base_url}/elasticengine/v1/alerts"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        for attempt in range(self._max_retries + 1):
            try:
                resp = requests.post(url, json=payload, headers=headers,
                                     timeout=15, verify=self._verify_tls)
            except requests.RequestException as exc:
                logger.warning("AnalyticsAlertNotifier: network error (attempt %d/%d): %s",
                               attempt + 1, self._max_retries + 1, exc)
                if attempt < self._max_retries:
                    time.sleep(self._backoff_base * (self._backoff_factor ** attempt))
                continue

            if resp.status_code == 201:
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                logger.info(
                    "AnalyticsAlertNotifier: accepted alert_id=%s rule=%s mmsi=%s",
                    data.get("alert_id", "?"), alert.rule_name, alert.mmsi,
                )
                return

            if resp.status_code == 200:
                try:
                    if resp.json().get("status") == "dropped":
                        logger.warning(
                            "AnalyticsAlertNotifier: domain '%s' is disabled — alert dropped",
                            self._domain)
                        return
                except ValueError:
                    pass

            if resp.status_code == 401:
                logger.error(
                    "AnalyticsAlertNotifier: 401 authentication failed — check token/domain (domain=%s)",
                    self._domain)
                return

            if resp.status_code == 400:
                err = {}
                try:
                    err = resp.json().get("error", {}) or {}
                except ValueError:
                    pass
                logger.error("AnalyticsAlertNotifier: 400 bad payload [%s]: %s",
                             err.get("code", "?"), err.get("message", resp.text[:200]))
                return

            if resp.status_code == 503:
                if attempt < self._max_retries:
                    delay = self._backoff_base * (self._backoff_factor ** attempt)
                    logger.warning(
                        "AnalyticsAlertNotifier: 503 UPSTREAM_UNAVAILABLE (attempt %d/%d) — retrying in %.1fs",
                        attempt + 1, self._max_retries + 1, delay)
                    time.sleep(delay)
                    continue
                logger.error("AnalyticsAlertNotifier: 503 retry budget exhausted")
                return

            logger.error("AnalyticsAlertNotifier: unexpected status %d: %s",
                         resp.status_code, resp.text[:200])
            return

    def verify(self) -> bool:
        url = f"{self._base_url}/elasticengine/v1/alerts/verify"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(url, json={"domain": self._domain}, headers=headers,
                                 timeout=10, verify=self._verify_tls)
        except requests.RequestException as exc:
            logger.error("AnalyticsAlertNotifier: verify network error: %s", exc)
            return False

        if resp.status_code == 200:
            try:
                ok = resp.json().get("status") == "ok"
            except ValueError:
                ok = False
            if ok:
                logger.info("AnalyticsAlertNotifier: verify OK — domain=%s endpoint=%s",
                            self._domain, url)
                return True
            logger.error("AnalyticsAlertNotifier: verify 200 but unexpected body: %s",
                         resp.text[:200])
            return False

        if resp.status_code == 401:
            logger.error("AnalyticsAlertNotifier: verify 401 — check domain and token")
            return False

        logger.error("AnalyticsAlertNotifier: verify unexpected status %d: %s",
                     resp.status_code, resp.text[:200])
        return False


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

# Anomaly flag names recognised by the "anomaly:<flag>" condition token.
# Keep this aligned with ecs_builder._detect_anomalies — emit a warning if a
# rules file references an unknown flag so misconfiguration shows up at load
# rather than silently never firing.
_KNOWN_ANOMALY_FLAGS = frozenset({
    "mmsi_invalid_range",
    "mmsi_envelope_mismatch",
    "mmsi_suspicious_pattern",
    "mmsi_category_message_mismatch",
    "null_island",
    "sog_implausible_high",
    "navstatus_speed_mismatch",
    "heading_cog_disagree",
    "identity_changed",
})

_DISTRESS_SEVERITY_TOKENS = {
    # AIS distress severities come from ecs_builder.detect_distress (syslog scale).
    "distress_critical": 2,
    "distress_high":     3,
    "distress_medium":   4,
    "distress_low":      5,
}


class AlertEngine:
    """Evaluate built ECS documents against configured rules and dispatch alerts.

    Rule shape (in alert_rules.json under "rules"):
        {
          "name": "distress",
          "enabled": true,
          "alert_label": "...",   # optional, → ECS rule.name in analytics
          "alert_category": "...",# optional, → ECS rule.category
          "description": "...",
          "conditions": ["distress"],         # all tokens must match
          "severity": "critical",             # info|warning|critical
          "realert_minutes": 15
        }

    Available condition tokens:
        distress                  ais.distress.is_distress == true
        distress_critical/high/medium/low   ais.distress.severity matches
        anomaly:<flag>            ais.anomaly.<flag> is truthy
    """

    def __init__(self,
                 rules_file: str,
                 slack_webhook_url: str = "",
                 slack_display_name: str = "AIS Monitor",
                 analytics_base_url: str = "",
                 analytics_domain: str = "",
                 analytics_token: str = "",
                 analytics_insecure: Optional[bool] = None,
                 observer: Optional[Dict] = None,
                 eviction_ttl_seconds: int = 3600) -> None:
        self._rules_doc: dict = {}
        self._rules: List[dict] = []
        # (mmsi, rule_name) → monotonic timestamp of last fire
        self._cooldowns: Dict[tuple, float] = {}
        # mmsi → monotonic timestamp of last evaluation (for cooldown sweep)
        self._mmsi_last_seen: Dict[str, float] = {}
        self._last_eviction_mono: float = 0.0
        self._eviction_ttl: int = eviction_ttl_seconds
        self._notifiers: List[Notifier] = []
        self._observer = observer or {}

        self._load_rules(rules_file)

        # Slack — CLI args override rules-file slack block.
        rules_slack = self._rules_doc.get("slack", {}) if isinstance(
            self._rules_doc.get("slack"), dict) else {}
        eff_webhook = slack_webhook_url or rules_slack.get("webhook_url", "")
        slack_display = slack_display_name or rules_slack.get(
            "display_name", "AIS Monitor")
        slack_min_sev = str(rules_slack.get("min_severity", "warning"))
        slack_enabled = rules_slack.get("enabled", True) if rules_slack else True
        if slack_enabled and eff_webhook:
            self._notifiers.append(
                SlackNotifier(eff_webhook, slack_display, slack_min_sev))
            slack_status = "enabled"
        elif not slack_enabled:
            slack_status = "disabled (slack.enabled=false)"
        else:
            slack_status = "disabled (no webhook configured)"

        # Analytics — CLI args override rules-file analytics block.
        rules_analytics = self._rules_doc.get("analytics", {}) if isinstance(
            self._rules_doc.get("analytics"), dict) else {}
        eff_base   = analytics_base_url or rules_analytics.get("base_url", "")
        eff_domain = analytics_domain   or rules_analytics.get("domain", "")
        eff_token  = analytics_token    or rules_analytics.get("token", "")
        if analytics_insecure is None:
            eff_insecure = bool(rules_analytics.get("insecure", False))
        else:
            eff_insecure = bool(analytics_insecure)
        eff_min_sev = str(rules_analytics.get("min_severity", "info"))
        eff_enabled = rules_analytics.get("enabled", True) if rules_analytics else True

        if eff_enabled and eff_base and eff_domain and eff_token:
            self._notifiers.append(
                AnalyticsAlertNotifier(
                    base_url=eff_base, domain=eff_domain, token=eff_token,
                    display_name=slack_display, min_severity=eff_min_sev,
                    verify_tls=not eff_insecure, observer=self._observer,
                ))
            analytics_status = f"enabled (domain={eff_domain})"
        elif not eff_enabled:
            analytics_status = "disabled (analytics.enabled=false)"
        else:
            analytics_status = "disabled (missing base_url/domain/token)"

        if not self._notifiers:
            logger.warning(
                "AlertEngine: no notifiers configured — alerts will be logged only.")

        enabled_count = sum(1 for r in self._rules if r.get("enabled", False))
        logger.info(
            "AlertEngine: slack=%s analytics=%s rules=%s (enabled=%d/%d)",
            slack_status, analytics_status, rules_file,
            enabled_count, len(self._rules),
        )

    # ------------------------------------------------------------------
    # Rule loading + validation
    # ------------------------------------------------------------------

    def _load_rules(self, rules_file: str) -> None:
        if not os.path.isfile(rules_file):
            logger.warning("Rules file not found: %s — all alerts disabled.", rules_file)
            return
        try:
            with open(rules_file, "r") as fh:
                self._rules_doc = json.load(fh)
        except Exception:
            logger.exception("Failed to load rules file %s", rules_file)
            return
        raw_rules = self._rules_doc.get("rules", [])
        if not isinstance(raw_rules, list):
            logger.warning("Rules file %s has no 'rules' list — alerts disabled.", rules_file)
            return
        self._rules = [r for r in raw_rules if isinstance(r, dict)]

        # Warn on unknown tokens at load time rather than silently never firing.
        for r in self._rules:
            for tok in r.get("conditions", []) or []:
                if tok == "distress":
                    continue
                if tok in _DISTRESS_SEVERITY_TOKENS:
                    continue
                if tok.startswith("anomaly:"):
                    flag = tok.split(":", 1)[1]
                    if flag not in _KNOWN_ANOMALY_FLAGS:
                        logger.warning(
                            "Rule '%s' references unknown anomaly flag '%s' — will never fire.",
                            r.get("name", "<unnamed>"), flag)
                    continue
                logger.warning(
                    "Rule '%s' has unknown condition token '%s' — will never fire.",
                    r.get("name", "<unnamed>"), tok)

    # ------------------------------------------------------------------
    # Condition checks
    # ------------------------------------------------------------------

    def _check_condition(self, token: str, ais_block: dict) -> Optional[dict]:
        """Return metadata dict (truthy) when *token* matches, None otherwise."""
        if token == "distress":
            distress = ais_block.get("distress") or {}
            if not distress.get("is_distress"):
                return None
            return {
                "source": distress.get("source"),
                "severity": distress.get("severity"),
                "keywords_matched": distress.get("keywords_matched") or [],
                "text": distress.get("text"),
            }

        if token in _DISTRESS_SEVERITY_TOKENS:
            distress = ais_block.get("distress") or {}
            if distress.get("severity") == _DISTRESS_SEVERITY_TOKENS[token]:
                return {"severity": distress.get("severity")}
            return None

        if token.startswith("anomaly:"):
            flag = token.split(":", 1)[1]
            anomaly = ais_block.get("anomaly") or {}
            val = anomaly.get(flag)
            if not val:
                return None
            return {"flag": flag, "value": val}

        return None

    # ------------------------------------------------------------------
    # Cooldowns
    # ------------------------------------------------------------------

    def _in_cooldown(self, mmsi: str, rule_name: str,
                     realert_minutes: float, now_mono: float) -> bool:
        last = self._cooldowns.get((mmsi, rule_name))
        if last is None:
            return False
        effective_sec = max(60.0, realert_minutes * 60.0)
        return (now_mono - last) < effective_sec

    def _mark_cooldown(self, mmsi: str, rule_name: str, now_mono: float) -> None:
        self._cooldowns[(mmsi, rule_name)] = now_mono

    def _evict_stale(self, now_mono: float) -> None:
        """Drop cooldown entries for MMSIs not seen within eviction_ttl_seconds.

        Bounds memory across a long-running process when many distinct vessels
        trigger alerts over time.  Rate-limited to once per 300s.
        """
        if (now_mono - self._last_eviction_mono) < 300.0:
            return
        self._last_eviction_mono = now_mono
        stale_mmsis = {
            mmsi for mmsi, last in self._mmsi_last_seen.items()
            if (now_mono - last) >= self._eviction_ttl
        }
        if not stale_mmsis:
            return
        for mmsi in stale_mmsis:
            self._mmsi_last_seen.pop(mmsi, None)
        self._cooldowns = {
            k: v for k, v in self._cooldowns.items() if k[0] not in stale_mmsis
        }
        logger.debug("AlertEngine: evicted %d stale mmsi(s) from cooldown table",
                     len(stale_mmsis))

    # ------------------------------------------------------------------
    # Public evaluation
    # ------------------------------------------------------------------

    def evaluate(self, doc: dict, now_mono: float) -> List[Alert]:
        """Evaluate one built ECS document against all enabled rules.

        Returns the list of alerts that fired (also dispatched to notifiers).
        Empty list when no rule matched or no MMSI is present.
        """
        ais_block = doc.get("ais") or {}
        # Cooldown is keyed by MMSI; messages without an MMSI cannot have a
        # stable cooldown key, so we skip them.  Distress with no MMSI is
        # vanishingly rare (beacons must broadcast an MMSI).
        mmsi = (((doc.get("source") or {}).get("address"))
                or ((ais_block.get("mmsi") or {}).get("value")))
        if not mmsi:
            return []

        self._mmsi_last_seen[mmsi] = now_mono

        alerts: List[Alert] = []
        for rule in self._rules:
            if not rule.get("enabled", False):
                continue
            rule_name = rule.get("name", "<unnamed>")
            conditions = rule.get("conditions", []) or []
            if not conditions:
                continue

            matched: Dict[str, dict] = {}
            failed = False
            for cond in conditions:
                meta = self._check_condition(cond, ais_block)
                if meta is None:
                    failed = True
                    break
                matched[cond] = meta
            if failed:
                continue

            realert_min = float(rule.get("realert_minutes", 60.0))
            if self._in_cooldown(mmsi, rule_name, realert_min, now_mono):
                continue

            alerts.append(self._build_alert(doc, ais_block, rule, matched, mmsi))
            self._mark_cooldown(mmsi, rule_name, now_mono)

        self._evict_stale(now_mono)

        if alerts:
            for n in self._notifiers:
                n.send(alerts)
        return alerts

    # ------------------------------------------------------------------
    # Alert construction
    # ------------------------------------------------------------------

    def _build_alert(self, doc: dict, ais_block: dict, rule: dict,
                     matched: Dict[str, dict], mmsi: str) -> Alert:
        rule_name = rule.get("name", "<unnamed>")
        severity = str(rule.get("severity", "warning")).lower()
        if severity not in _SEVERITY_ORDER:
            severity = "warning"

        ship_name = ais_block.get("ship_name")
        ship_type = ais_block.get("ship_type")
        flag_country = (ais_block.get("mmsi") or {}).get("flag_country")
        rng = ais_block.get("range") or {}

        identity_bits = [mmsi]
        if ship_name:
            identity_bits.append(f"'{ship_name}'")
        if flag_country:
            identity_bits.append(f"[{flag_country}]")
        identity = " ".join(identity_bits)

        # message: a one-line summary suitable for log lines and the analytics
        # API. Slack uses a richer multi-line block built from the structured
        # fields below; this string is the fallback / log-trace form.
        summary_bits = [f"Vessel {identity}"]
        for cond, meta in matched.items():
            if cond == "distress":
                src_key = meta.get("source") or ""
                summary_bits.append(
                    _DISTRESS_SOURCE_LABELS.get(src_key, src_key or "distress"))
                kws = meta.get("keywords_matched") or []
                if kws:
                    summary_bits.append(f"keywords {','.join(kws)}")
            elif cond.startswith("anomaly:"):
                flag = cond.split(":", 1)[1]
                summary_bits.append(_ANOMALY_LABELS.get(flag, flag))
        message = f"{rule.get('alert_label') or rule_name}: " + "; ".join(summary_bits) + "."
        logger.warning("ALERT [%s] %s", rule_name, message)

        src_geo = ((doc.get("source") or {}).get("geo") or {}).get("location") or {}
        src_lat = src_geo.get("lat")
        src_lon = src_geo.get("lon")
        tracking_url, tracking_site = dec.vessel_tracking_link(mmsi, ais_block.get("imo"))

        return Alert(
            rule_name=rule_name,
            event=rule_name,
            mmsi=mmsi,
            severity=severity,
            message=message,
            details={
                "rule_name": rule_name,
                "rule_description": rule.get("description") or rule_name,
                "ship_name": ship_name,
                "ship_type": ship_type,
                "flag_country": flag_country,
                "message_type": ais_block.get("message_type"),
                "range_mi": rng.get("distance_mi"),
                "bearing_deg": rng.get("bearing_deg"),
                "bearing_cardinal": rng.get("bearing_cardinal"),
                "conditions": matched,
            },
            source_lat=float(src_lat) if src_lat is not None else None,
            source_lon=float(src_lon) if src_lon is not None else None,
            alert_label=rule.get("alert_label"),
            alert_category=rule.get("alert_category"),
            tracking_url=tracking_url,
            tracking_site=tracking_site,
        )
