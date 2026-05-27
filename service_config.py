"""service_config.py — YAML config loader for ais_elastic.

Provides load_config() and print_config() for folding a YAML file into an
argparse.Namespace while respecting CLI > YAML > argparse-defaults precedence.

Standalone module — no deps on other project files.
Requires: PyYAML (pyyaml).
"""

import copy
import logging
import os
import re
import sys
from typing import Any, Dict, Optional, Set

import yaml

log = logging.getLogger("ais_elastic")

# ---------------------------------------------------------------------------
# Sentinel for "was this CLI flag explicitly set?"
# ---------------------------------------------------------------------------

class _Sentinel:
    """Unique token placed as every default so we can detect explicit CLI args."""


_SENTINEL = _Sentinel()


def _explicitly_set_cli_args(argparser, argv) -> Set[str]:
    """Return the set of argparse dest names that appeared explicitly in argv.

    Uses the sentinel-clone idiom: replace every action's default with a
    unique sentinel, parse argv, collect dests whose value is NOT the sentinel.
    """
    clone = copy.deepcopy(argparser)
    for action in clone._actions:
        if action.dest != "help":
            action.default = _SENTINEL
    parsed = clone.parse_args(argv)
    return {dest for dest, val in vars(parsed).items() if not isinstance(val, _Sentinel)}


# ---------------------------------------------------------------------------
# YAML I/O helpers
# ---------------------------------------------------------------------------

def _load_yaml_file(path: str) -> Optional[Dict]:
    """Load and return a YAML file as a dict, or None if the file is absent.

    Empty file → returns {}.
    Malformed YAML or wrong top-level type → logs error and sys.exit(2).
    """
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        log.error("Malformed YAML in %s: %s", path, exc)
        sys.exit(2)
    except OSError as exc:
        log.error("Cannot read config file %s: %s", path, exc)
        sys.exit(2)

    if raw is None:
        log.info("Config file %s is empty — using CLI/defaults only.", path)
        return {}

    if not isinstance(raw, dict):
        log.error(
            "Config file %s: expected a YAML mapping at top level, got %s",
            path, type(raw).__name__,
        )
        sys.exit(2)

    return raw


def _get_yaml_path(cfg: Dict, dotted_key: str) -> Any:
    """Traverse a nested dict using a dotted key path, e.g. 'alerts.slack.webhook_url'.

    Returns None if any key in the path is absent or the value is actually None.
    """
    parts = dotted_key.split(".")
    node = cfg
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# ---------------------------------------------------------------------------
# CLI → YAML mapping table
# ---------------------------------------------------------------------------
# Each entry: (argparse_dest, yaml_dotted_path, python_type)
# Types: str, int, float, bool
# Special: observer_coord is synthesized from observer block (handled separately).
# TLS inversion: no_verify_tls ↔ elasticsearch.verify_tls (handled separately).
# CLI-only (NOT in this table): config, print_config, print_template, verify_only,
#   source_kind, aisstream_insecure, bbox, filter_mmsi, filter_message_type,
#   initial_index, template_name, policy_name, static_cache_path.

_MAPPING = [
    # argparse dest         yaml path                               type
    ("backend_type",        "elasticsearch.backend_type",           str),
    ("server_url",          "elasticsearch.url",                    str),
    ("username",            "elasticsearch.username",               str),
    ("password",            "elasticsearch.password",               str),
    ("api_key",             "elasticsearch.api_key",                str),
    ("index_alias",         "elasticsearch.index",                  str),
    ("skip_index_setup",    "elasticsearch.skip_index_setup",       bool),
    ("flush_interval",      "flush.interval_s",                     float),
    ("flush_batch_size",    "flush.batch_size",                     int),
    ("queue_size",          "flush.max_queue",                      int),
    ("local_timezone",      "local_timezone",                       str),
    ("static_cache_path",   "static_cache_path",                    str),
    ("alert_rules",         "alerts.rules_file",                    str),
    ("slack_webhook",       "alerts.slack.webhook_url",             str),
    ("slack_display_name",  "alerts.slack.display_name",            str),
    ("analytics_base_url",  "alerts.analytics.base_url",            str),
    ("analytics_domain",    "alerts.analytics.domain",              str),
    ("analytics_token",     "alerts.analytics.token",               str),
    ("analytics_insecure",  "alerts.analytics.insecure",            bool),
    ("observer_name",       "observer.name",                        str),
]


def _apply_yaml_to_args(args, yaml_cfg: Dict, explicit_cli: Set[str]) -> None:
    """Write YAML values into args for any dest NOT explicitly set on the CLI.

    Precedence: CLI-explicit > YAML > argparse defaults.
    Mutates args in-place.
    """
    for dest, yaml_path, typ in _MAPPING:
        if dest in explicit_cli:
            continue  # CLI wins — do not touch

        val = _get_yaml_path(yaml_cfg, yaml_path)
        if val is None:
            continue  # Not in YAML — keep argparse default

        # Coerce to declared type (YAML may give int where str expected, etc.)
        try:
            if typ is bool:
                coerced = bool(val)
            elif typ is int:
                coerced = int(val)
            elif typ is float:
                coerced = float(val)
            else:
                coerced = str(val)
        except (TypeError, ValueError) as exc:
            log.error(
                "Config %s: cannot convert %r to %s: %s",
                yaml_path, val, typ.__name__, exc,
            )
            sys.exit(2)

        setattr(args, dest, coerced)

    # observer.name: empty string in YAML means "not set" — leave args.observer_name as-is
    if "observer_name" not in explicit_cli:
        observer_block = yaml_cfg.get("observer") or {}
        if isinstance(observer_block, dict):
            name_val = observer_block.get("name")
            if name_val is not None and name_val != "":
                setattr(args, "observer_name", str(name_val))
            # name="" in YAML → leave args.observer_name untouched (hostname fallback)

    # observer coord — dict shape.  Accepts flat observer.{lat,lon,alt_m} OR
    # legacy nested observer.geo.{lat,lon,alt_m}.  Legacy form logs a deprecation
    # INFO so operators know to migrate.
    if "observer_coord" not in explicit_cli:
        obs = yaml_cfg.get("observer") or {}
        flat_lat = obs.get("lat")
        flat_lon = obs.get("lon")
        flat_alt = obs.get("alt_m")
        geo = obs.get("geo") or {}
        nested_lat = geo.get("lat")
        nested_lon = geo.get("lon")
        nested_alt = geo.get("alt_m")
        legacy_used = (nested_lat is not None or nested_lon is not None
                       or nested_alt is not None)
        if legacy_used and not (flat_lat is not None or flat_lon is not None):
            log.info(
                "Legacy observer.geo.{lat,lon,alt_m} layout detected; "
                "migrate to flat observer.lat/lon/alt_m."
            )
            lat, lon, alt = nested_lat, nested_lon, nested_alt
        else:
            lat, lon, alt = flat_lat, flat_lon, flat_alt
        if lat is not None and lon is not None:
            coord = {"lat": float(lat), "lon": float(lon),
                     "alt_m": float(alt) if alt is not None else 0.0}
            setattr(args, "observer_coord", coord)
        elif lat is not None or lon is not None or alt is not None:
            log.error(
                "observer coord partially specified; need both lat and lon "
                "(alt_m optional, defaults to 0)."
            )
            sys.exit(2)

    # TLS inversion: YAML elasticsearch.verify_tls (bool) → args.no_verify_tls (bool)
    if "no_verify_tls" not in explicit_cli:
        v = _get_yaml_path(yaml_cfg, "elasticsearch.verify_tls")
        if v is not None:
            setattr(args, "no_verify_tls", not bool(v))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(args, project_name: str, script_dir: str, argparser, argv=None) -> Dict:
    """Resolve YAML config and fold it into args in-place (mutates the Namespace).

    Search order (first match wins):
      1. args.config if set (--config <path>)
      2. <script_dir>/<project_name>.yaml
      3. /etc/<project_name>/<project_name>.yaml
      4. /etc/default/<project_name>.yaml

    If no file is found, returns {} and leaves args untouched (CLI-only path
    continues to work exactly as before).

    Precedence applied to args: CLI-explicit > YAML > argparse defaults.

    Returns the parsed YAML dict (empty dict if no file found or file is empty).
    """
    # Determine where to look
    config_path = getattr(args, "config", None)

    if config_path:
        # Explicit --config: must exist (absence is an error)
        if not os.path.exists(config_path):
            log.error("Config file not found: %s", config_path)
            sys.exit(2)
        search_paths = [config_path]
        mandatory = True
    else:
        search_paths = [
            os.path.join(script_dir, f"{project_name}.yaml"),
            f"/etc/{project_name}/{project_name}.yaml",
            f"/etc/default/{project_name}.yaml",
        ]
        mandatory = False

    yaml_cfg: Optional[Dict] = None
    used_path: Optional[str] = None

    for candidate in search_paths:
        result = _load_yaml_file(candidate)
        if result is not None:
            yaml_cfg = result
            used_path = candidate
            break

    if yaml_cfg is None:
        if mandatory:
            # _load_yaml_file already called sys.exit(2) for bad files;
            # if we reach here the file existed but returned None (impossible
            # given the mandatory check above), but guard anyway.
            log.error("Config file not found: %s", config_path)
            sys.exit(2)
        # No YAML found — CLI-only operation, return empty dict
        return {}

    if used_path:
        log.info("Loaded config from %s", used_path)

    if not yaml_cfg:
        # Empty file
        return {}

    # Determine which CLI args were explicitly set
    explicit_cli = _explicitly_set_cli_args(argparser, argv if argv is not None else sys.argv[1:])

    _apply_yaml_to_args(args, yaml_cfg, explicit_cli)

    return yaml_cfg


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Leaf key names that are always redacted, regardless of nesting
_SENSITIVE_KEYS = frozenset({"password", "token", "secret", "webhook_url", "api_key"})


def _redact_value(key: str, value: Any) -> Any:
    """Return a redacted representation of a sensitive value."""
    if not value:
        return value  # Empty string / None / falsy → nothing to redact
    if key == "url" and isinstance(value, str):
        # Redact user:pass@ in URLs (e.g. https://user:pass@host:9200)
        return re.sub(r"://[^:@/]+:[^:@/]+@", r"://***:***@", value)
    if key == "webhook_url" and isinstance(value, str):
        return "https://hooks.slack.com/services/***"
    # Generic sensitive key
    return "***"


def _redact(cfg: Any, parent_key: str = "") -> Any:
    """Recursively walk cfg and redact sensitive leaf values.

    Works on both plain dicts and nested structures.
    The elasticsearch.url field gets special URL-credential scrubbing.
    """
    if isinstance(cfg, dict):
        out = {}
        for k, v in cfg.items():
            if isinstance(v, dict):
                out[k] = _redact(v, parent_key=k)
            elif k in _SENSITIVE_KEYS or (k == "url" and parent_key == "elasticsearch"):
                out[k] = _redact_value(k, v)
            else:
                out[k] = v
        return out
    return cfg


def print_config(args, redacted: bool = True) -> None:
    """Print effective configuration to stdout as YAML; secrets redacted by default.

    The `args` Namespace is already the merged result (CLI > YAML > defaults)
    by the time this is called, so the canonical view is built directly from it.

    Note: `sources` (list of AIS feed configs) is NOT shown here — it lives
    in the parsed YAML dict and is outside the scalar config domain.  Inspect
    the YAML file directly to view sources configuration.
    """
    effective: Dict[str, Any] = {
        "elasticsearch": {
            "backend_type":     getattr(args, "backend_type", None),
            "url":              getattr(args, "server_url", None),
            "username":         getattr(args, "username", None),
            "password":         getattr(args, "password", None),
            "api_key":          getattr(args, "api_key", None),
            "index":            getattr(args, "index_alias", None),
            "verify_tls":       not getattr(args, "no_verify_tls", False),
            "skip_index_setup": getattr(args, "skip_index_setup", False),
        },
        "observer": {
            "name":  getattr(args, "observer_name", None),
            "coord": getattr(args, "observer_coord", None),  # dict or None
        },
        "flush": {
            "interval_s": getattr(args, "flush_interval", None),
            "batch_size": getattr(args, "flush_batch_size", None),
            "max_queue":  getattr(args, "queue_size", None),
        },
        "local_timezone":    getattr(args, "local_timezone", None),
        "static_cache_path": getattr(args, "static_cache_path", None),
        "alerts": {
            "rules_file": getattr(args, "alert_rules", None),
            "slack": {
                "webhook_url":  getattr(args, "slack_webhook", None),
                "display_name": getattr(args, "slack_display_name", None),
            },
            "analytics": {
                "base_url": getattr(args, "analytics_base_url", None),
                "token":    getattr(args, "analytics_token", None),
                "domain":   getattr(args, "analytics_domain", None),
                "insecure": getattr(args, "analytics_insecure", None),
            },
        },
    }

    # Remove None leaves for cleaner output
    def _strip_none(d):
        if isinstance(d, dict):
            return {k: _strip_none(v) for k, v in d.items() if v is not None}
        return d

    effective = _strip_none(effective)

    if redacted:
        effective = _redact(effective)

    print(yaml.dump(effective, default_flow_style=False, sort_keys=False), end="")
