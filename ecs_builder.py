#!/usr/bin/python3

"""ecs_builder — Convert a normalized AIS record into an ECS 8.17 document.

This module maintains ONE small piece of stateful enrichment: a bounded
in-memory cache of last-known static data keyed by MMSI.  AIS position
messages do not carry ship type, name, dimensions, etc. — that lives in
infrequently-transmitted static messages.  Without the cache, position
docs (the bulk of traffic and the things that drive map icons) are
type-blank and every vessel renders the same.  The cache stamps the
fields onto subsequent position docs so map icons and dashboards can
distinguish cargo from fishing from passenger at a glance.

The cache is in-process: rebuilt on restart from the next pass of static
broadcasts (which happen every 6 minutes for Class A, longer for B).
A short warm-up period is the cost of a stateless restart; we accept it.

Everything else in this module is a pure function; safe to call from the
indexer thread.
"""

from __future__ import annotations

import datetime
import math
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional

from geopy.distance import geodesic  # pyright: ignore[reportMissingImports]

import ais_decoders as dec

ECS_VERSION = "8.17.0"

NM_TO_KM = 1.852
NM_TO_MI = 1.15078

# Cap distress message size to avoid mapping blow-up; safety text is bounded
# by AIS frame size in practice (~120 chars) but we trim defensively.
_TEXT_TRIM = 1024

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday",
              "Friday", "Saturday", "Sunday")


# ---------------------------------------------------------------------------
# Static-data LRU cache (MMSI -> last-known static fields)
# ---------------------------------------------------------------------------
# 50k entries is enough for any realistic single-AOR deployment (busiest
# AIS-receiving sites see a few thousand unique MMSIs per day).  When the
# cache fills, oldest entry is evicted.

_STATIC_CACHE_MAX = 50_000
_STATIC_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_STATIC_CACHE_LOCK = threading.Lock()
_STATIC_CACHE_PATH: Optional[str] = None
_STATIC_CACHE_DIRTY = False


def init_static_cache(path: Optional[str]):
    """Configure the persistent-cache file path and load any existing
    snapshot.  Pass None to disable persistence (in-memory only).

    Should be called once at startup, after config parsing.  Failures to
    load (missing file, malformed JSON, etc.) are non-fatal — the cache
    just starts empty and warms up from the next static-data broadcasts.
    """
    global _STATIC_CACHE_PATH, _STATIC_CACHE_DIRTY
    _STATIC_CACHE_PATH = path
    if not path:
        return
    try:
        import json as _json
        from pathlib import Path as _Path
        text = _Path(path).read_text()
        data = _json.loads(text)
        if not isinstance(data, dict):
            return
        with _STATIC_CACHE_LOCK:
            _STATIC_CACHE.clear()
            for k, v in data.items():
                if isinstance(v, dict):
                    _STATIC_CACHE[k] = v
        _STATIC_CACHE_DIRTY = False
    except FileNotFoundError:
        pass
    except Exception:
        # Don't crash startup on a corrupt snapshot — better to warm-restart
        # than to fail to come up.  Caller logs.
        pass


def save_static_cache():
    """Atomically write the current cache to disk if a path is configured
    and there are unsaved updates.  Returns the number of entries written,
    or None if persistence is disabled or there is nothing to save."""
    global _STATIC_CACHE_DIRTY
    if not _STATIC_CACHE_PATH or not _STATIC_CACHE_DIRTY:
        return None
    import json as _json
    import os as _os
    from pathlib import Path as _Path
    with _STATIC_CACHE_LOCK:
        snapshot = dict(_STATIC_CACHE)
        _STATIC_CACHE_DIRTY = False
    target = _Path(_STATIC_CACHE_PATH)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(_json.dumps(snapshot, separators=(",", ":")))
        _os.replace(tmp, target)
    except Exception:
        # Re-mark dirty so the next save retries.
        with _STATIC_CACHE_LOCK:
            _STATIC_CACHE_DIRTY = True
        raise
    return len(snapshot)


def static_cache_size():
    with _STATIC_CACHE_LOCK:
        return len(_STATIC_CACHE)


def _static_cache_update(mmsi, fields):
    """Insert/refresh the cached static fields for *mmsi*."""
    global _STATIC_CACHE_DIRTY
    if not mmsi or not fields:
        return
    with _STATIC_CACHE_LOCK:
        _STATIC_CACHE.pop(mmsi, None)
        _STATIC_CACHE[mmsi] = fields
        while len(_STATIC_CACHE) > _STATIC_CACHE_MAX:
            _STATIC_CACHE.popitem(last=False)
        _STATIC_CACHE_DIRTY = True


def _static_cache_get(mmsi):
    if not mmsi:
        return None
    with _STATIC_CACHE_LOCK:
        if mmsi in _STATIC_CACHE:
            # Move-to-end to keep this entry warm under LRU eviction.
            _STATIC_CACHE.move_to_end(mmsi)
            return dict(_STATIC_CACHE[mmsi])
    return None


# Fields harvested from static messages and stamped onto positions.  Avoid
# stamping fields that come naturally from the position message itself
# (sog, cog, position) so the static cache never overrides live data.
_STATIC_STAMP_FIELDS = (
    "ship_name", "call_sign", "imo", "destination",
    "ship_type", "ship_type_raw", "ship_type_category",
    "draught_m", "dimensions",
)


# Subset of static fields whose value SHOULD NOT change for a real vessel.
# A change in any of these between successive static broadcasts from the
# same MMSI is an identity-churn signal — a known AIS deception TTP
# (vessel swaps name / IMO mid-voyage to evade tracking).  Voyage-state
# fields (destination, draught, eta) are deliberately excluded because
# they change legitimately every trip.
_IDENTITY_FIELDS = ("ship_name", "call_sign", "imo", "ship_type_raw")


def _detect_identity_change(prev, new):
    """Compare cached static fields *prev* against incoming static *new*.

    Returns (changed_bool, previous_identity_dict).  previous_identity is
    None when nothing changed.  Only changes in stable identity fields
    count — voyage-state churn is ignored.
    """
    if not prev:
        return False, None
    changed = {}
    for f in _IDENTITY_FIELDS:
        p = prev.get(f)
        n = new.get(f)
        # Skip the comparison when the new message simply didn't carry
        # the field (e.g. Class B 24A reports name only — absence of
        # call_sign in 24A doesn't mean the call_sign changed).
        if n is None:
            continue
        if p is not None and p != n:
            changed[f] = p
    return (bool(changed), changed if changed else None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bearing(lat1, lon1, lat2, lon2):
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2_r)
    y = (math.cos(lat1_r) * math.sin(lat2_r)
         - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon))
    return math.degrees(math.atan2(x, y)) % 360.0


def _prune(d):
    """Remove None values and empty dicts/lists, recursively."""
    if isinstance(d, dict):
        cleaned = {}
        for k, v in d.items():
            v2 = _prune(v)
            if v2 is None or v2 == {} or v2 == []:
                continue
            cleaned[k] = v2
        return cleaned
    if isinstance(d, list):
        return [_prune(x) for x in d if x is not None]
    return d


def _eta_to_utc(eta, now_utc):
    """Best-effort ETA datetime; AIS msg 5 has no year.

    Returns (datetime or None, year_assumed_bool).
    """
    if not eta:
        return None, False
    try:
        m = int(eta.get("Month", 0))
        d = int(eta.get("Day", 0))
        h = int(eta.get("Hour", 24))
        mi = int(eta.get("Minute", 60))
    except (TypeError, ValueError):
        return None, False
    if m == 0 or d == 0 or h == 24 or mi == 60:
        return None, False
    year = now_utc.year
    try:
        guess = datetime.datetime(year, m, d, h, mi, tzinfo=datetime.timezone.utc)
    except ValueError:
        return None, False
    # If guessed ETA is more than 90 days in the past, assume next year
    if guess < now_utc - datetime.timedelta(days=90):
        try:
            guess = guess.replace(year=year + 1)
        except ValueError:
            return None, False
    return guess, True


# ---------------------------------------------------------------------------
# Roi (region-of-interest) match against the operator's bounding boxes
# ---------------------------------------------------------------------------

def _match_roi(lat, lon, rois):
    """Return (matched_bool, name_or_None).  *rois* is a list of dicts:
       {name, nw: [lat,lon], se: [lat,lon]}."""
    if lat is None or lon is None or not rois:
        return False, None
    for roi in rois:
        try:
            nw_lat, nw_lon = roi["nw"]
            se_lat, se_lon = roi["se"]
        except (KeyError, ValueError):
            continue
        # AIS bbox is two corners; allow either ordering
        lat_lo, lat_hi = min(nw_lat, se_lat), max(nw_lat, se_lat)
        lon_lo, lon_hi = min(nw_lon, se_lon), max(nw_lon, se_lon)
        if lat_lo <= lat <= lat_hi and lon_lo <= lon <= lon_hi:
            return True, roi.get("name")
    return False, None


# ---------------------------------------------------------------------------
# Per-message-family payload extraction
# ---------------------------------------------------------------------------

def _extract_position_body(body):
    """Common fields shared by msg 1/2/3 (PositionReport),
    18 (StandardClassB), 19 (ExtendedClassB), 27 (LongRange)."""
    common: Dict[str, Any] = {}
    if "Sog" in body:
        sog = body["Sog"]
        common["sog_kt"] = float(sog) if sog is not None else None
    if "Cog" in body:
        cog = body["Cog"]
        common["cog_deg"] = float(cog) if cog is not None else None
    if "TrueHeading" in body:
        th = body["TrueHeading"]
        common["true_heading_deg"] = th if th != dec.TRUE_HEADING_NOT_AVAILABLE else None
    if "NavigationalStatus" in body:
        ns = body["NavigationalStatus"]
        common["nav_status_raw"] = ns
        common["nav_status"] = dec.decode_nav_status(ns)
    if "RateOfTurn" in body:
        common["rot_dpm"] = dec.decode_rate_of_turn(body["RateOfTurn"])
    return common


def _extract_static(body, now_utc):
    """Extract msg 5 (ShipStaticData) fields.

    Only set string fields when we actually parsed a value — writing None
    would clobber values already pulled from MetaData (e.g. ship_name)
    before per-family extraction runs."""
    out: Dict[str, Any] = {}
    cs = dec.clean_ais_string(body.get("CallSign"))
    if cs:
        out["call_sign"] = cs
    sn = dec.clean_ais_string(body.get("Name"))
    if sn:
        out["ship_name"] = sn
    dest = dec.clean_ais_string(body.get("Destination"))
    if dest:
        out["destination"] = dest
    if "ImoNumber" in body and body["ImoNumber"]:
        out["imo"] = int(body["ImoNumber"])
        out["imo_info"] = {"checksum_valid": dec.imo_checksum_valid(out["imo"])}
    if "Type" in body:
        out["ship_type_raw"] = body["Type"]
        label, cat = dec.decode_ship_type(body["Type"])
        out["ship_type"] = label
        out["ship_type_category"] = cat
    if "MaximumStaticDraught" in body:
        d = body["MaximumStaticDraught"]
        if d is not None and d > 0:
            out["draught_m"] = float(d)
    dim = body.get("Dimension") or {}
    out["dimensions"] = _dimensions_from(dim)
    if "FixType" in body:
        out.setdefault("position", {})["epfd"] = dec.decode_epfd(body["FixType"])
    eta_dt, year_assumed = _eta_to_utc(body.get("Eta"), now_utc)
    if eta_dt:
        out["eta_utc"] = eta_dt.isoformat()
        out["eta_year_assumed"] = year_assumed
    return out


def _extract_static_data_report(body):
    """Extract msg 24 — Class B static, split into ReportA / ReportB parts."""
    out: Dict[str, Any] = {}
    sr: Dict[str, Any] = {"part_number": 1 if body.get("PartNumber") else 0}
    a = body.get("ReportA") or {}
    b = body.get("ReportB") or {}
    if a.get("Valid"):
        out["ship_name"] = dec.clean_ais_string(a.get("Name"))
    if b.get("Valid"):
        out["call_sign"] = dec.clean_ais_string(b.get("CallSign"))
        if "ShipType" in b:
            out["ship_type_raw"] = b["ShipType"]
            label, cat = dec.decode_ship_type(b["ShipType"])
            out["ship_type"] = label
            out["ship_type_category"] = cat
        dim = b.get("Dimension") or {}
        out["dimensions"] = _dimensions_from(dim)
        if "FixType" in b:
            out.setdefault("position", {})["epfd"] = dec.decode_epfd(b["FixType"])
        sr["vendor_id_name"] = dec.clean_ais_string(b.get("VendorIDName"))
        if b.get("VenderIDModel"):
            sr["vendor_unit_model_code"] = b["VenderIDModel"]
        if b.get("VenderIDSerial"):
            sr["vendor_serial_number"] = b["VenderIDSerial"]
        if "MothershipMmsi" in b:
            sr["mothership_mmsi"] = f"{int(b['MothershipMmsi']):09d}"
    out["static_report"] = sr
    return out


def _extract_aton(body):
    """Extract msg 21 — Aid to Navigation Report."""
    out: Dict[str, Any] = {"aton": {}}
    aton = out["aton"]
    if "Type" in body:
        aton["type_raw"] = body["Type"]
        aton["type"] = dec.decode_aton_type(body["Type"])
    aton["name"] = dec.clean_ais_string(body.get("Name"))
    aton["name_extension"] = dec.clean_ais_string(body.get("NameExtension"))
    if "OffPosition" in body:
        aton["off_position"] = bool(body["OffPosition"])
    if "VirtualAtoN" in body:
        aton["virtual"] = bool(body["VirtualAtoN"])
    dim = body.get("Dimension") or {}
    out["dimensions"] = _dimensions_from(dim)
    # AtoN uses "Fixtype" (lowercase 't') — quirk in upstream library
    fix = body.get("Fixtype", body.get("FixType"))
    if fix is not None:
        out.setdefault("position", {})["epfd"] = dec.decode_epfd(fix)
    return out


def _extract_sar(body):
    """Extract msg 9 — Standard SAR Aircraft Report."""
    common = _extract_position_body(body)
    sar = {}
    if "Altitude" in body:
        sar["altitude_m"] = body["Altitude"]
    return {**common, "sar": sar}


def _extract_safety(body):
    """Extract msg 12 / 14 — safety text (distress detection in caller)."""
    out: Dict[str, Any] = {}
    m = body.get("DestMmsi") or body.get("DestinationMmsi")
    if m:
        out.setdefault("distress", {})["addressed_to_mmsi"] = f"{int(m):09d}"
    return out


def _extract_base_station(body):
    out: Dict[str, Any] = {}
    bs = {}
    try:
        y = int(body.get("Year", 0))
        mo = int(body.get("Month", 0))
        d = int(body.get("Day", 0))
        h = int(body.get("Hour", 24))
        mi = int(body.get("Minute", 60))
        se = int(body.get("Second", 60))
        if y > 0 and 1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60 and se < 60:
            bs["time_utc"] = datetime.datetime(
                y, mo, d, h, mi, se,
                tzinfo=datetime.timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    if "Fixtype" in body or "FixType" in body:
        fix = body.get("Fixtype", body.get("FixType"))
        out.setdefault("position", {})["epfd"] = dec.decode_epfd(fix)
    if bs:
        out["base_station"] = bs
    return out


def _extract_binary(body):
    out = {"binary": {}}
    if "ApplicationID" in body:
        aid = body["ApplicationID"]
        if isinstance(aid, dict):
            if "DesignatedAreaCode" in aid:
                out["binary"]["dac"] = aid["DesignatedAreaCode"]
            if "FunctionIdentifier" in aid:
                out["binary"]["fi"] = aid["FunctionIdentifier"]
    if "DestinationID" in body:
        out["binary"]["addressed_to_mmsi"] = f"{int(body['DestinationID']):09d}"
    return out


def _dimensions_from(dim):
    if not dim:
        return None
    out = {}
    a = dim.get("A")
    b = dim.get("B")
    c = dim.get("C")
    d = dim.get("D")
    if a is not None:
        out["to_bow_m"] = a
    if b is not None:
        out["to_stern_m"] = b
    if c is not None:
        out["to_port_m"] = c
    if d is not None:
        out["to_starboard_m"] = d
    if a is not None and b is not None and (a + b) > 0:
        out["length_m"] = float(a + b)
    if c is not None and d is not None and (c + d) > 0:
        out["beam_m"] = float(c + d)
    return out or None


# ---------------------------------------------------------------------------
# Anomaly detection (single-message, no state)
# ---------------------------------------------------------------------------

def _detect_anomalies(body, mmsi_info, message_type, position_valid_flag,
                      envelope_mmsi, body_mmsi):
    """Return ais.anomaly.* sub-object."""
    a = {}
    if not mmsi_info["valid"]:
        a["mmsi_invalid_range"] = True
    if envelope_mmsi is not None and body_mmsi is not None and \
            int(envelope_mmsi) != int(body_mmsi):
        a["mmsi_envelope_mismatch"] = True
    # All-same-digit MMSIs and similar placeholders are misconfigured
    # transponders, not real vessel identities — surface them at ingest.
    if mmsi_info["valid"] and dec.mmsi_pattern_suspicious(envelope_mmsi or body_mmsi):
        a["mmsi_suspicious_pattern"] = True

    # mmsi-category vs message-type sanity
    if mmsi_info["valid"]:
        cat = mmsi_info["category"]
        is_position = message_type in (
            "PositionReport", "StandardClassBPositionReport",
            "ExtendedClassBPositionReport", "LongRangeAisBroadcastMessage")
        is_sar_msg = message_type == "StandardSearchAndRescueAircraftReport"
        is_aton_msg = message_type == "AidsToNavigationReport"
        if is_position and cat in ("aton", "sar_aircraft", "coast_station",
                                   "group", "invalid"):
            a["mmsi_category_message_mismatch"] = True
        if is_aton_msg and cat != "aton":
            a["mmsi_category_message_mismatch"] = True
        if is_sar_msg and cat != "sar_aircraft":
            a["mmsi_category_message_mismatch"] = True

    if not body:
        return a

    # Null-island: literal (0,0) is suspect (uninitialized GPS / test fixture).
    # Note: ITU sentinel "field not reported" values (lat=91, lon=181,
    # SoG=1023, CoG=3600, TrueHeading=511, RoT=-128) are NOT anomalies — they
    # are normal for Class B transponders, anchored vessels, and any platform
    # without the relevant sensor.  The corresponding scalar fields
    # (ais.true_heading_deg, ais.rot_dpm, ...) are already null when these
    # sentinels are received; null-field queries are the right way to ask
    # "what's missing" without polluting the anomaly namespace.
    lat = body.get("Latitude")
    lon = body.get("Longitude")
    if position_valid_flag and dec.position_is_null_island(lat, lon):
        a["null_island"] = True

    # Implausibly-high SoG (Class A SoG > 60 kt below the high sentinel)
    sog = body.get("Sog")
    if sog is not None and sog > 60.0 and sog < dec.SOG_HIGH_SENTINEL:
        a["sog_implausible_high"] = True

    cog = body.get("Cog")
    th = body.get("TrueHeading")

    # Nav-status vs SOG mismatch (only for Class A position reports)
    ns = body.get("NavigationalStatus")
    if ns is not None and sog is not None and sog < dec.SOG_HIGH_SENTINEL:
        if dec.nav_status_implies_stationary(ns) and sog > 3.0:
            a["navstatus_speed_mismatch"] = True

    # Heading-vs-COG disagreement (only when both valid and moving)
    if th is not None and th != dec.TRUE_HEADING_NOT_AVAILABLE and \
            cog is not None and cog != dec.COG_NOT_AVAILABLE and \
            sog is not None and sog > 5.0:
        diff = abs(th - cog)
        if diff > 180:
            diff = 360 - diff
        if diff > 45:
            a["heading_cog_disagree"] = True

    return a


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_document(record: Dict[str, Any],
                   observer: Dict[str, Any],
                   rois: Optional[list] = None,
                   local_tz: Optional[datetime.tzinfo] = None) -> Dict[str, Any]:
    """Convert a normalized AIS record into an ECS document dict.

    *record* shape (from sources/base.py):
        received_at:      datetime (UTC-aware)
        source_metadata:  dict with kind/vendor/feed_id/dataset/etc.
        raw:              original aisstream envelope
    *observer* dict:
        name, hostname, geo: {lat, lon, alt_m}, vendor, product, type
    *rois* list of {name, nw:[lat,lon], se:[lat,lon]} for region matching.
    """
    raw = record["raw"]
    received_at = record["received_at"]
    src_meta = record["source_metadata"]

    message_type = raw.get("MessageType", "UnknownMessage")
    metadata = raw.get("MetaData", {}) or {}
    msg_outer = raw.get("Message", {}) or {}
    # Body is the single inner dict keyed by message-type
    body = msg_outer.get(message_type, {}) or {}

    envelope_mmsi = metadata.get("MMSI")
    body_mmsi = body.get("UserID")
    mmsi = envelope_mmsi or body_mmsi
    mmsi_info = dec.classify_mmsi(mmsi)
    mmsi_str = f"{int(mmsi):09d}" if mmsi_info["valid"] else None

    # Time of the AIS event itself (Go-format string from aisstream)
    msg_time = dec.parse_go_time(metadata.get("time_utc"))
    timestamp = msg_time or received_at
    lag_s = (received_at - msg_time).total_seconds() if msg_time else None

    # Position resolution: prefer message body; fall back to MetaData
    lat = body.get("Latitude")
    lon = body.get("Longitude")
    if lat is None or lon is None or not dec.position_valid(lat, lon):
        meta_lat = metadata.get("latitude")
        meta_lon = metadata.get("longitude")
        if dec.position_valid(meta_lat, meta_lon) and (meta_lat, meta_lon) != (0.0, 0.0):
            lat, lon = meta_lat, meta_lon
    pos_valid = dec.position_valid(lat, lon) and not dec.position_is_null_island(lat, lon)

    # --- Begin building the ais.* sub-object ---
    ais: Dict[str, Any] = {
        "message_type": message_type,
        "message_class": dec.message_class(message_type),
        "received_at": received_at.isoformat(),
    }
    if "MessageID" in body:
        ais["message_id"] = body["MessageID"]
    if "Valid" in body:
        ais["valid"] = bool(body["Valid"])
    if "RepeatIndicator" in body:
        ais["repeat_indicator"] = body["RepeatIndicator"]
    if lag_s is not None:
        ais["lag_s"] = round(lag_s, 3)

    # MMSI block (always emitted when we have any MMSI at all)
    if mmsi_str:
        country, iso = dec.mid_lookup(mmsi_info["mid"]) if mmsi_info["mid"] else (None, None)
        ais["mmsi"] = {
            "value": mmsi_str,
            "value_int": int(mmsi_str),
            "category": mmsi_info["category"],
        }
        if mmsi_info["mid"] is not None:
            ais["mmsi"]["mid"] = mmsi_info["mid"]
        if country:
            ais["mmsi"]["flag_country"] = country
        if iso:
            ais["mmsi"]["flag_iso_code"] = iso

    # ShipName from MetaData (most messages — backfilled from static when ship reports it)
    sn = dec.clean_ais_string(metadata.get("ShipName"))
    if sn:
        ais["ship_name"] = sn

    # Per-family extraction
    msg_class = ais["message_class"]
    if msg_class == "position":
        ais.update(_extract_position_body(body))
    elif message_type == "ShipStaticData":
        ais.update(_extract_static(body, received_at))
    elif message_type == "StaticDataReport":
        ais.update(_extract_static_data_report(body))
    elif message_type == "AidsToNavigationReport":
        ais.update(_extract_aton(body))
    elif message_type == "StandardSearchAndRescueAircraftReport":
        ais.update(_extract_sar(body))
    elif msg_class == "safety":
        ais.update(_extract_safety(body))

    # Static-data caching:
    #   - When THIS message is static, harvest the type/dimension fields into
    #     the per-MMSI cache so subsequent position docs from the same vessel
    #     can be type-aware.  Before overwriting the cache, compare the new
    #     static fields to the cached ones — a change in identity (name /
    #     callsign / IMO / type) is the classic AIS deception TTP and is
    #     surfaced as ais.anomaly.identity_changed plus a previous_identity
    #     sub-object so the operator sees what changed.
    #   - When THIS message is a position (or anything missing type info),
    #     stamp from the cache if we have it.
    if mmsi_str:
        if msg_class == "static":
            cacheable = {k: ais[k] for k in _STATIC_STAMP_FIELDS if k in ais}
            if cacheable:
                prev = _static_cache_get(mmsi_str)
                changed, prev_identity = _detect_identity_change(prev, cacheable)
                if changed:
                    a = ais.setdefault("anomaly", {})
                    a["identity_changed"] = True
                    a["previous_identity"] = prev_identity
                _static_cache_update(mmsi_str, cacheable)
        else:
            cached = _static_cache_get(mmsi_str)
            if cached:
                for k, v in cached.items():
                    ais.setdefault(k, v)

    # Tracking URL pivot.  After the static-cache stamp so position docs can
    # use a cached IMO (MarineTraffic only resolves by IMO now).
    if mmsi_str and mmsi_info["category"] in ("ship", "auxiliary_craft", "sar_aircraft"):
        url, _site = dec.vessel_tracking_link(mmsi_str, ais.get("imo"))
        if url:
            ais["tracking_url"] = url

    # Default ship_type_category so map icon dispatch always has a value
    # to match against (renders the neutral "unspecified" icon vs nothing).
    # Only applies to records that would land on the map.
    if msg_class in ("position", "static") and "ship_type_category" not in ais:
        ais["ship_type_category"] = "unspecified"
    elif message_type == "BaseStationReport":
        ais.update(_extract_base_station(body))
    elif msg_class == "binary":
        ais.update(_extract_binary(body))

    # Position quality bits
    if pos_valid:
        ais.setdefault("position", {})["valid"] = True
        if "PositionAccuracy" in body:
            ais["position"]["accuracy_high"] = bool(body["PositionAccuracy"])
        if "Raim" in body:
            ais["position"]["raim"] = bool(body["Raim"])
        if "Timestamp" in body:
            ais["position"]["timestamp_s"] = body["Timestamp"]
            ais["position"]["timestamp_quality"] = dec.decode_timestamp_quality(body["Timestamp"])

    # Range from observer (only when both observer location and message
    # position are known)
    obs_geo = (observer or {}).get("geo") or {}
    obs_lat = obs_geo.get("lat")
    obs_lon = obs_geo.get("lon")
    if pos_valid and obs_lat is not None and obs_lon is not None:
        dist_km = geodesic((obs_lat, obs_lon), (lat, lon)).km
        dist_nm = dist_km / NM_TO_KM
        brg = _bearing(obs_lat, obs_lon, lat, lon)
        ais["range"] = {
            "distance_km": round(dist_km, 3),
            "distance_nm": round(dist_nm, 3),
            "distance_mi": round(dist_nm * NM_TO_MI, 3),
            "bearing_deg": round(brg, 2),
            "bearing_cardinal": dec.bearing_to_cardinal(brg),
        }

    # ROI match
    matched, roi_name = _match_roi(lat, lon, rois)
    if matched:
        ais["roi"] = {"matched": True, "name": roi_name}

    # Time fields (UTC always; local only if tz given)
    ais["time"] = {
        "hour_utc": timestamp.astimezone(datetime.timezone.utc).hour,
        "day_of_week_utc": _DAY_NAMES[timestamp.astimezone(datetime.timezone.utc).weekday()],
    }
    if local_tz:
        local = timestamp.astimezone(local_tz)
        ais["time"]["hour_local"] = local.hour
        ais["time"]["day_of_week_local"] = _DAY_NAMES[local.weekday()]

    # Source feed identification
    ais["source_feed"] = {
        "kind": src_meta.get("kind"),
        "vendor": src_meta.get("vendor"),
        "feed_id": src_meta.get("feed_id"),
    }

    # Distress detection
    distress = dec.detect_distress(message_type, mmsi_info, body)
    if distress["text"]:
        distress["text"] = distress["text"][:_TEXT_TRIM]
    if distress["is_distress"] or distress["text"] or distress["keywords_matched"]:
        ais.setdefault("distress", {})
        for k in ("is_distress", "source", "severity", "keywords_matched", "text"):
            v = distress.get(k)
            if v not in (None, [], ""):
                ais["distress"][k] = v

    # Anomalies
    anomalies = _detect_anomalies(
        body, mmsi_info, message_type, pos_valid, envelope_mmsi, body_mmsi)
    if anomalies:
        ais["anomaly"] = anomalies

    # ECS top level
    dataset = src_meta.get("dataset") or f"ais.{src_meta.get('kind', 'unknown')}"
    doc: Dict[str, Any] = {
        "@timestamp": timestamp.astimezone(datetime.timezone.utc).isoformat(),
        "ecs": {"version": ECS_VERSION},
        "event": {
            "kind": "alert" if distress["is_distress"] else "event",
            "category": ["network"] + (["threat"] if distress["is_distress"] else []),
            "type": ["info"],
            "module": src_meta.get("module", "ais"),
            "dataset": dataset,
            "action": f"ais-{msg_class}-message",
            "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        "observer": {
            "type": observer.get("type", "feed"),
            "vendor": observer.get("vendor"),
            "product": observer.get("product"),
            "name": observer.get("name"),
            "hostname": observer.get("hostname"),
        },
        "ais": ais,
    }
    if distress["severity"] is not None:
        doc["event"]["severity"] = distress["severity"]
    if obs_lat is not None and obs_lon is not None:
        doc["observer"]["geo"] = {"location": {"lat": obs_lat, "lon": obs_lon}}
        if obs_geo.get("alt_m") is not None:
            doc["observer"]["geo"]["altitude"] = obs_geo["alt_m"]

    # source.*
    src: Dict[str, Any] = {}
    if mmsi_str:
        src["address"] = mmsi_str
    if pos_valid:
        src.setdefault("geo", {})["location"] = {"lat": lat, "lon": lon}
        if message_type in ("AidsToNavigationReport",):
            src["geo"]["name"] = "aton position"
        elif message_type == "StandardSearchAndRescueAircraftReport":
            src["geo"]["name"] = "sar aircraft position"
        elif message_type == "BaseStationReport":
            src["geo"]["name"] = "base station position"
        else:
            src["geo"]["name"] = "vessel position"
        if ais.get("mmsi", {}).get("flag_country"):
            src["geo"]["country_name"] = ais["mmsi"]["flag_country"]
        if ais.get("mmsi", {}).get("flag_iso_code"):
            src["geo"]["country_iso_code"] = ais["mmsi"]["flag_iso_code"]
    if sn:
        src.setdefault("user", {})["name"] = sn
    if src:
        doc["source"] = src

    # related.*
    related_ids = []
    related_hosts = []
    if mmsi_str:
        related_ids.append(mmsi_str)
        related_hosts.append(mmsi_str)
    if "imo" in ais and ais["imo"]:
        related_ids.append(f"IMO{ais['imo']}")
    cs = ais.get("call_sign")
    if cs:
        related_ids.append(cs.upper())
    if related_ids or related_hosts:
        doc["related"] = {}
        if related_ids:
            doc["related"]["id"] = related_ids
        if related_hosts:
            doc["related"]["hosts"] = related_hosts

    # message — short, human-readable summary
    name_part = sn or (mmsi_str or "?")
    if distress["is_distress"]:
        doc["message"] = f"DISTRESS: {name_part} {message_type}"
    elif msg_class == "position" and pos_valid:
        doc["message"] = f"{name_part} position"
    elif msg_class == "static":
        doc["message"] = f"{name_part} static data"
    elif msg_class == "safety":
        doc["message"] = f"{name_part} safety {message_type}"
    else:
        doc["message"] = f"{name_part} {message_type}"

    return _prune(doc)
