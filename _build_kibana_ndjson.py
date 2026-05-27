#!/usr/bin/python3
"""Helper that emits kibana_saved_objects.ndjson for ais-elastic.

Not run at install time — produces the shipped file.  Kept in the repo so
the saved-object set can be regenerated when fields change.
"""

import json
from pathlib import Path

_ICON_DIR = Path(__file__).resolve().parent / "dashboard_icons"


def _load_icon_svg(name):
    """Return raw SVG text for *name* (without .svg extension).

    Kibana 8.x customIcons stores raw SVG text in the saved object; the
    Maps renderer rasterizes it on-the-fly via tiny-sdf.  An earlier
    revision base64-encoded the SVG, which Kibana tried to parse as SVG
    and produced unreadable blob renders."""
    return (_ICON_DIR / f"{name}.svg").read_text()


def _custom_icons():
    """Custom inline SVG icons are not used right now.

    Inline customIcons in the map saved object don't render reliably in
    this Kibana version — multiple encoding attempts (base64, raw,
    different SDF parameters) either rendered fuzzy blobs or no icons
    at all.  Kibana's UI runs an SDF preprocessing step on uploaded
    SVGs that we can't replicate from outside the browser without
    pulling in the same JS pipeline.

    The shipped dashboard_icons/*.svg files are kept in the repo for
    future re-attempt or for hand-upload via Stack Management ->
    Maps -> Custom Icons.  Layers currently use built-in Maki icons,
    which gives type-differentiated rendering immediately.
    """
    return []

NOW = "2026-05-15T20:00:00.000Z"

TAG_ID = "ais-tag-0000-0000-000000000001"
IP_ID = "ais-idxp-0000-0000-000000000001"
LENS_VESSELS_ID = "ais-lens-vessels-0000-000000000001"
LENS_DISTRESS_BY_HR_ID = "ais-lens-distress-hr-000000000001"
LENS_POLAR_ID = "ais-lens-polar-0000-00-000000000001"
SEARCH_DISTRESS_ID = "ais-search-distress-00-000000000001"
SEARCH_ANOMALY_ID = "ais-search-anomaly-000-000000000001"
MAP_ID = "ais-map-positions-0000-000000000001"
DASHBOARD_ID = "ais-dash-watch-0000-00-000000000001"

# Operational dashboard saved-object IDs
LENS_KPI_VESSELS_ID = "ais-lens-kpi-vessels-00-000000000001"
LENS_KPI_MSGRATE_ID = "ais-lens-kpi-msgrate-00-000000000001"
LENS_KPI_ANOMALIES_ID = "ais-lens-kpi-anomalies-000000000001"
LENS_KPI_DISTRESS_ID = "ais-lens-kpi-distress-0-000000000001"
LENS_KPI_RANGE_ID = "ais-lens-kpi-range-0000-000000000001"
LENS_KPI_LAG_ID = "ais-lens-kpi-lag-0000-00-000000000001"
LENS_MSGRATE_CLASS_ID = "ais-lens-msgrate-class-000000000001"
LENS_SHIPTYPE_DONUT_ID = "ais-lens-shiptype-donut-00000000001"
LENS_FLAG_BAR_ID = "ais-lens-flag-bar-0000-0-000000000001"
LENS_NAVSTATUS_BAR_ID = "ais-lens-navstatus-bar-000000000001"
LENS_ANOMALY_COUNTS_ID = "ais-lens-anomaly-counts-00000000001"
LENS_DISTRESS_SEV_ID = "ais-lens-distress-sev-0-000000000001"
SEARCH_SUSP_IDENTITY_ID = "ais-search-susp-id-0000-000000000001"
SEARCH_KINEMATIC_ID = "ais-search-kinematic-00-000000000001"
SEARCH_ATON_OFF_ID = "ais-search-aton-off-00-0000000000001"
SEARCH_ACTIVE_VESSELS_ID = "ais-search-active-vessels-00000000001"
DASHBOARD_OPS_ID = "ais-dash-ops-0000-0000-000000000001"

# OR-of-all-anomalies query reused across panels
_ANY_ANOMALY_KQL = (
    "ais.anomaly.mmsi_invalid_range: true OR "
    "ais.anomaly.mmsi_envelope_mismatch: true OR "
    "ais.anomaly.mmsi_category_message_mismatch: true OR "
    "ais.anomaly.mmsi_suspicious_pattern: true OR "
    "ais.anomaly.identity_changed: true OR "
    "ais.anomaly.null_island: true OR "
    "ais.anomaly.sog_implausible_high: true OR "
    "ais.anomaly.navstatus_speed_mismatch: true OR "
    "ais.anomaly.heading_cog_disagree: true"
)

_SUSP_IDENTITY_KQL = (
    "ais.anomaly.mmsi_suspicious_pattern: true OR "
    "ais.anomaly.mmsi_category_message_mismatch: true OR "
    "ais.anomaly.mmsi_invalid_range: true OR "
    "ais.anomaly.mmsi_envelope_mismatch: true OR "
    "ais.anomaly.identity_changed: true"
)

_KINEMATIC_KQL = (
    "ais.anomaly.navstatus_speed_mismatch: true OR "
    "ais.anomaly.heading_cog_disagree: true OR "
    "ais.anomaly.sog_implausible_high: true"
)


def base(d):
    d.setdefault("coreMigrationVersion", "8.8.0")
    d.setdefault("managed", False)
    d.setdefault("created_at", NOW)
    d.setdefault("updated_at", NOW)
    return d


def tag():
    return base({
        "type": "tag",
        "typeMigrationVersion": "8.0.0",
        "id": TAG_ID,
        "attributes": {
            "name": "ais-monitoring",
            "color": "#0077b6",
            "description": "AIS maritime monitoring & distress watch",
        },
        "references": [],
    })


def index_pattern():
    return base({
        "type": "index-pattern",
        "typeMigrationVersion": "8.0.0",
        "id": IP_ID,
        "attributes": {
            "fieldAttrs": "{}",
            "fieldFormatMap": "{}",
            "fields": "[]",
            "name": "ais*",
            "runtimeFieldMap": "{}",
            "sourceFilters": "[]",
            "timeFieldName": "@timestamp",
            "title": "ais*",
            "typeMeta": "{}",
        },
        "references": [],
    })


# --- Lens: Vessels seen (datatable, last X hours) ---------------------------

def lens_vessels_seen():
    layer_id = "ais-layer-vessels-0001"
    state = {
        "datasourceStates": {
            "formBased": {
                "layers": {
                    layer_id: {
                        "columnOrder": [
                            "col_mmsi", "col_name", "col_callsign",
                            "col_flag", "col_category", "col_msgs",
                            "col_max_sog", "col_last_seen",
                        ],
                        "columns": {
                            "col_mmsi": {
                                "label": "MMSI",
                                "dataType": "string",
                                "operationType": "terms",
                                "scale": "ordinal",
                                "sourceField": "ais.mmsi.value",
                                "isBucketed": True,
                                "params": {
                                    "size": 500,
                                    "orderBy": {"type": "column",
                                                "columnId": "col_last_seen"},
                                    "orderDirection": "desc",
                                    "otherBucket": False,
                                    "missingBucket": False,
                                    "parentFormat": {"id": "terms"},
                                },
                                "customLabel": True,
                            },
                            "col_name": {
                                "label": "Ship Name",
                                "dataType": "string",
                                "operationType": "last_value",
                                "scale": "ratio",
                                "sourceField": "ais.ship_name",
                                "isBucketed": False,
                                "params": {"showArrayValues": False,
                                           "sortField": "@timestamp"},
                                "customLabel": True,
                            },
                            "col_callsign": {
                                "label": "Call Sign",
                                "dataType": "string",
                                "operationType": "last_value",
                                "scale": "ratio",
                                "sourceField": "ais.call_sign",
                                "isBucketed": False,
                                "params": {"showArrayValues": False,
                                           "sortField": "@timestamp"},
                                "customLabel": True,
                            },
                            "col_flag": {
                                "label": "Flag",
                                "dataType": "string",
                                "operationType": "last_value",
                                "scale": "ratio",
                                "sourceField": "ais.mmsi.flag_country",
                                "isBucketed": False,
                                "params": {"showArrayValues": False,
                                           "sortField": "@timestamp"},
                                "customLabel": True,
                            },
                            "col_category": {
                                "label": "Type",
                                "dataType": "string",
                                "operationType": "last_value",
                                "scale": "ratio",
                                "sourceField": "ais.ship_type_category",
                                "isBucketed": False,
                                "params": {"showArrayValues": False,
                                           "sortField": "@timestamp"},
                                "customLabel": True,
                            },
                            "col_msgs": {
                                "label": "Messages",
                                "dataType": "number",
                                "operationType": "count",
                                "scale": "ratio",
                                "sourceField": "___records___",
                                "isBucketed": False,
                                "customLabel": True,
                            },
                            "col_max_sog": {
                                "label": "Max SOG (kt)",
                                "dataType": "number",
                                "operationType": "max",
                                "scale": "ratio",
                                "sourceField": "ais.sog_kt",
                                "isBucketed": False,
                                "customLabel": True,
                            },
                            "col_last_seen": {
                                "label": "Last seen",
                                "dataType": "date",
                                "operationType": "max",
                                "scale": "ratio",
                                "sourceField": "@timestamp",
                                "isBucketed": False,
                                "customLabel": True,
                            },
                        },
                        "incompleteColumns": {},
                    }
                }
            }
        },
        "internalReferences": [],
        "adHocDataViews": {},
        "filters": [],
        "query": {"query": "", "language": "kuery"},
        "visualization": {
            "layerId": layer_id,
            "layerType": "data",
            "columns": [
                {"columnId": "col_mmsi"},
                {"columnId": "col_name"},
                {"columnId": "col_callsign"},
                {"columnId": "col_flag"},
                {"columnId": "col_category"},
                {"columnId": "col_msgs"},
                {"columnId": "col_max_sog"},
                {"columnId": "col_last_seen"},
            ],
        },
    }
    return base({
        "type": "lens",
        "typeMigrationVersion": "8.6.0",
        "id": LENS_VESSELS_ID,
        "attributes": {
            "title": "Vessels Seen",
            "description": "Per-MMSI rollup: name, call sign, flag, type, message count, max SOG, last-seen.",
            "visualizationType": "lnsDatatable",
            "state": state,
        },
        "references": [
            {"id": IP_ID,
             "name": f"indexpattern-datasource-layer-{layer_id}",
             "type": "index-pattern"},
            {"id": TAG_ID, "name": "tag-ref-ais", "type": "tag"},
        ],
    })


# --- Lens: Distress events over time ----------------------------------------

def lens_distress_over_time():
    layer_id = "ais-layer-distress-hr-0001"
    state = {
        "datasourceStates": {
            "formBased": {
                "layers": {
                    layer_id: {
                        "columnOrder": ["col_time", "col_count"],
                        "columns": {
                            "col_time": {
                                "label": "@timestamp",
                                "dataType": "date",
                                "operationType": "date_histogram",
                                "scale": "interval",
                                "sourceField": "@timestamp",
                                "isBucketed": True,
                                "params": {"interval": "auto",
                                           "dropPartials": False,
                                           "includeEmptyRows": True},
                            },
                            "col_count": {
                                "label": "Distress events",
                                "dataType": "number",
                                "operationType": "count",
                                "scale": "ratio",
                                "sourceField": "___records___",
                                "isBucketed": False,
                                "customLabel": True,
                            },
                        },
                        "incompleteColumns": {},
                    }
                }
            }
        },
        "internalReferences": [],
        "adHocDataViews": {},
        "filters": [],
        "query": {"language": "kuery",
                  "query": "ais.distress.is_distress: true"},
        "visualization": {
            "preferredSeriesType": "bar_stacked",
            "legend": {"isVisible": True, "position": "right"},
            "valueLabels": "hide",
            "fittingFunction": "None",
            "axisTitlesVisibilitySettings": {"x": True, "yLeft": True, "yRight": True},
            "tickLabelsVisibilitySettings": {"x": True, "yLeft": True, "yRight": True},
            "labelsOrientation": {"x": 0, "yLeft": 0, "yRight": 0},
            "gridlinesVisibilitySettings": {"x": True, "yLeft": True, "yRight": True},
            "layers": [{
                "layerId": layer_id,
                "accessors": ["col_count"],
                "position": "top",
                "seriesType": "bar_stacked",
                "showGridlines": False,
                "layerType": "data",
                "xAccessor": "col_time",
            }],
        },
    }
    return base({
        "type": "lens",
        "typeMigrationVersion": "8.6.0",
        "id": LENS_DISTRESS_BY_HR_ID,
        "attributes": {
            "title": "AIS Distress Events Over Time",
            "description": "Counts of ais.distress.is_distress=true bucketed by time.",
            "visualizationType": "lnsXY",
            "state": state,
        },
        "references": [
            {"id": IP_ID,
             "name": f"indexpattern-datasource-layer-{layer_id}",
             "type": "index-pattern"},
            {"id": TAG_ID, "name": "tag-ref-ais", "type": "tag"},
        ],
    })


# --- Saved search: Distress events ------------------------------------------

def search_distress():
    return base({
        "type": "search",
        "typeMigrationVersion": "10.5.0",
        "id": SEARCH_DISTRESS_ID,
        "attributes": {
            "title": "AIS Distress Events",
            "description": "SART / MOB / EPIRB beacons, NavStatus AIS-SART, or safety messages with distress keywords.",
            "columns": [
                "ais.distress.source", "ais.distress.severity",
                "ais.distress.keywords_matched", "ais.distress.text",
                "ais.mmsi.value", "ais.ship_name",
                "ais.mmsi.flag_country", "source.geo.location",
            ],
            "sort": [["@timestamp", "desc"]],
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps({
                    "query": {"language": "kuery",
                              "query": "ais.distress.is_distress: true"},
                    "filter": [],
                    "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
                }),
            },
        },
        "references": [
            {"id": IP_ID,
             "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
             "type": "index-pattern"},
            {"id": TAG_ID, "name": "tag-ref-ais", "type": "tag"},
        ],
    })


# --- Saved search: Anomalies ------------------------------------------------

def search_anomalies():
    q = (
        "ais.anomaly.navstatus_speed_mismatch: true OR "
        "ais.anomaly.heading_cog_disagree: true OR "
        "ais.anomaly.mmsi_invalid_range: true OR "
        "ais.anomaly.mmsi_envelope_mismatch: true OR "
        "ais.anomaly.mmsi_category_message_mismatch: true OR "
        "ais.anomaly.mmsi_suspicious_pattern: true OR "
        "ais.anomaly.identity_changed: true OR "
        "ais.anomaly.null_island: true OR "
        "ais.anomaly.sog_implausible_high: true"
    )
    return base({
        "type": "search",
        "typeMigrationVersion": "10.5.0",
        "id": SEARCH_ANOMALY_ID,
        "attributes": {
            "title": "AIS Single-Message Anomalies",
            "description": "Spoof / sentinel / kinematic-impossibility flags raised at ingest.",
            "columns": [
                "ais.mmsi.value", "ais.ship_name",
                "ais.mmsi.flag_country", "ais.message_type",
                "ais.anomaly.navstatus_speed_mismatch",
                "ais.anomaly.heading_cog_disagree",
                "ais.anomaly.mmsi_invalid_range",
                "ais.anomaly.mmsi_envelope_mismatch",
                "ais.anomaly.mmsi_category_message_mismatch",
                "ais.anomaly.mmsi_suspicious_pattern",
                "ais.anomaly.null_island",
                "ais.anomaly.sog_implausible_high",
                "source.geo.location",
            ],
            "sort": [["@timestamp", "desc"]],
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps({
                    "query": {"language": "kuery", "query": q},
                    "filter": [],
                    "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
                }),
            },
        },
        "references": [
            {"id": IP_ID,
             "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
             "type": "index-pattern"},
            {"id": TAG_ID, "name": "tag-ref-ais", "type": "tag"},
        ],
    })


# --- Maps saved object: vessel positions on a world map --------------------
#
# Ship-type → EMS Maki icon table.  These are the icons that ship with
# Elastic Maps (Maki set).  Picked to convey vessel role at a glance:
#   - anchor for cargo/tanker (heavy commercial)
#   - ferry for passenger
#   - harbor for tugs/pilot vessels (port-associated craft)
#   - fitness-centre stand-in: triangle for sailing (only triangular icon
#     in the Maki set that reads as "sail")
#   - star for military / law-enforcement
#   - circle-stroked for SAR
#   - marker for everything else
# Per-category vessel layer definitions.  This replaces an earlier
# attempt at DYNAMIC icon dispatch via customIconStops, which Kibana
# rendered most categories as fallback shapes — only cargo (square)
# differentiated visibly.  One layer per operational group with a
# STATIC Maki icon and a distinct color.  More verbose JSON but
# rendering is reliable, and each group becomes a separately-toggleable
# layer in the Kibana layer panel ("show me only authority + SAR" is a
# one-click filter).
#
# Each tuple: (layer_id_suffix, layer_label, KQL filter, icon name,
#              fill color, icon size).  Order = bottom-to-top render
# stack; later layers paint over earlier ones.
_VESSEL_LAYER_GROUPS = [
    # Catch-all unknown — drawn first so typed icons paint over it.
    ("unspecified", "Unknown / Unspecified",
     'ais.ship_type_category: ("unspecified" or "other" or "diving" or "wig")',
     "circle", "#7f7f7f", 5),
    ("fishing", "Fishing",
     'ais.ship_type_category: "fishing"',
     "circle-stroked", "#bcbd22", 6),
    ("pleasure", "Pleasure / Sailing",
     'ais.ship_type_category: ("pleasure" or "sailing")',
     "triangle", "#8c564b", 6),
    ("cargo", "Cargo",
     'ais.ship_type_category: "cargo"',
     "square", "#1f77b4", 7),
    ("tanker", "Tanker",
     'ais.ship_type_category: "tanker"',
     "fuel", "#9467bd", 7),
    ("passenger", "Passenger / High-Speed",
     'ais.ship_type_category: ("passenger" or "hsc")',
     "ferry", "#17becf", 7),
    ("workboat", "Tug / Pilot / Port Tender",
     'ais.ship_type_category: ("tug" or "pilot" or "port_tender" or "towing")',
     "harbor", "#ff7f0e", 7),
    ("industrial", "Dredging / Industrial",
     'ais.ship_type_category: ("dredging" or "anti_pollution")',
     "industry", "#7f7f7f", 7),
    ("authority", "Military / Law Enforcement",
     'ais.ship_type_category: ("military" or "law_enforcement")',
     "star", "#d62728", 8),
    ("sar_medical", "SAR / Medical",
     'ais.ship_type_category: ("sar" or "medical")',
     "cross", "#e6194b", 8),
]


def _vessel_layer(suffix, label, kql, icon, color, size):
    """One per-category vessel layer.  All share the same TOP_HITS dedup
    by MMSI and the same DYNAMIC orientation by COG (so ships face along
    their course)."""
    layer_id = f"ais-vessel-{suffix}-layer"
    ref_name = f"layer_vessel_{suffix}_source_index_pattern"
    return {
        "id": layer_id, "label": label,
        "minZoom": 0, "maxZoom": 24, "alpha": 0.9,
        "sourceDescriptor": {
            "id": f"ais-vessel-{suffix}-source",
            "type": "ES_SEARCH",
            "geoField": "source.geo.location",
            "filterByMapBounds": True,
            "scalingType": "TOP_HITS",
            "topHitsSplitField": "ais.mmsi.value",
            "topHitsSize": 1,
            "sortField": "@timestamp",
            "sortOrder": "desc",
            "tooltipProperties": [
                "@timestamp", "ais.ship_name", "ais.mmsi.value",
                "ais.mmsi.flag_country", "ais.ship_type",
                "ais.ship_type_category", "ais.sog_kt", "ais.cog_deg",
                "ais.true_heading_deg", "ais.nav_status",
                "ais.range.distance_nm", "ais.range.bearing_cardinal",
                "ais.call_sign", "ais.imo", "ais.destination",
            ],
            "applyGlobalQuery": True,
            "applyGlobalTime": True,
            "applyForceRefresh": True,
            "indexPatternRefName": ref_name,
        },
        "style": {
            "type": "VECTOR",
            "properties": {
                "icon": {"type": "STATIC", "options": {"value": icon}},
                "fillColor": {"type": "STATIC",
                              "options": {"color": color}},
                "lineColor": {"type": "STATIC",
                              "options": {"color": "#FFFFFF"}},
                "lineWidth": {"type": "STATIC", "options": {"size": 0.5}},
                "iconSize": {"type": "STATIC", "options": {"size": size}},
                "iconOrientation": {"type": "DYNAMIC", "options": {
                    "field": {"name": "ais.cog_deg", "origin": "source"},
                    "fieldMetaOptions": {"isEnabled": False, "sigma": 3},
                }},
                "labelText": {"type": "STATIC", "options": {"value": ""}},
                "labelColor": {"type": "STATIC",
                               "options": {"color": "#000000"}},
                "labelSize": {"type": "STATIC", "options": {"size": 14}},
                "labelBorderColor": {"type": "STATIC",
                                     "options": {"color": "#FFFFFF"}},
                "labelBorderSize": {"options": {"size": "SMALL"}},
                "symbolizeAs": {"options": {"value": "icon"}},
            },
            "isTimeAware": True,
        },
        "query": {"language": "kuery", "query": kql},
        "type": "GEOJSON_VECTOR",
        "joins": [],
        "visible": True,
        "disableTooltips": False,
    }


def _aton_layer():
    """Aids to Navigation — buoys, racons, lighthouses — rendered as
    lighthouse icons regardless of AtoN sub-type."""
    return {
        "id": "ais-aton-layer",
        "label": "Aids to Navigation",
        "minZoom": 0, "maxZoom": 24, "alpha": 0.7,
        "sourceDescriptor": {
            "id": "ais-aton-source",
            "type": "ES_SEARCH",
            "geoField": "source.geo.location",
            "filterByMapBounds": True,
            "scalingType": "TOP_HITS",
            "topHitsSplitField": "ais.mmsi.value",
            "topHitsSize": 1,
            "sortField": "@timestamp",
            "sortOrder": "desc",
            "tooltipProperties": [
                "@timestamp", "ais.aton.name", "ais.aton.type",
                "ais.mmsi.value", "ais.aton.off_position",
                "ais.aton.virtual",
            ],
            "applyGlobalQuery": True,
            "applyGlobalTime": True,
            "applyForceRefresh": True,
            "indexPatternRefName": "layer_aton_source_index_pattern",
        },
        "style": {
            "type": "VECTOR",
            "properties": {
                "icon": {"type": "STATIC",
                         "options": {"value": "lighthouse"}},
                "fillColor": {"type": "STATIC",
                              "options": {"color": "#d62728"}},
                "lineColor": {"type": "STATIC",
                              "options": {"color": "#FFFFFF"}},
                "lineWidth": {"type": "STATIC", "options": {"size": 0.5}},
                "iconSize": {"type": "STATIC", "options": {"size": 6}},
                "labelText": {"type": "STATIC", "options": {"value": ""}},
                "symbolizeAs": {"options": {"value": "icon"}},
            },
            "isTimeAware": True,
        },
        # Layer-level filter so this layer only renders AtoN docs
        "query": {"query": "ais.message_class: aton",
                  "language": "kuery"},
        "type": "GEOJSON_VECTOR",
        "joins": [],
        "visible": True,
        "disableTooltips": False,
    }


def _distress_layer():
    """Distress events highlighted in red with a cross icon."""
    return {
        "id": "ais-distress-layer",
        "label": "DISTRESS",
        "minZoom": 0, "maxZoom": 24, "alpha": 1.0,
        "sourceDescriptor": {
            "id": "ais-distress-source",
            "type": "ES_SEARCH",
            "geoField": "source.geo.location",
            "filterByMapBounds": True,
            "scalingType": "TOP_HITS",
            "topHitsSplitField": "ais.mmsi.value",
            "topHitsSize": 1,
            "sortField": "@timestamp",
            "sortOrder": "desc",
            "tooltipProperties": [
                "@timestamp", "ais.distress.source",
                "ais.distress.severity", "ais.distress.text",
                "ais.distress.keywords_matched",
                "ais.ship_name", "ais.mmsi.value",
            ],
            "applyGlobalQuery": True,
            "applyGlobalTime": True,
            "applyForceRefresh": True,
            "indexPatternRefName": "layer_distress_source_index_pattern",
        },
        "style": {
            "type": "VECTOR",
            "properties": {
                "icon": {"type": "STATIC", "options": {"value": "cross"}},
                "fillColor": {"type": "STATIC",
                              "options": {"color": "#e6194b"}},
                "lineColor": {"type": "STATIC",
                              "options": {"color": "#FFFFFF"}},
                "lineWidth": {"type": "STATIC", "options": {"size": 1.5}},
                "iconSize": {"type": "STATIC", "options": {"size": 12}},
                "symbolizeAs": {"options": {"value": "icon"}},
            },
            "isTimeAware": True,
        },
        "query": {"query": "ais.distress.is_distress: true",
                  "language": "kuery"},
        "type": "GEOJSON_VECTOR",
        "joins": [],
        "visible": True,
        "disableTooltips": False,
    }


def _anomaly_layer():
    """Red ring around any vessel with any anomaly flag set — operator can
    spot misbehaving vessels in their geographic context at a glance.
    Renders ABOVE the vessel layer."""
    return {
        "id": "ais-anomaly-layer",
        "label": "Anomaly",
        "minZoom": 0, "maxZoom": 24, "alpha": 0.95,
        "sourceDescriptor": {
            "id": "ais-anomaly-source",
            "type": "ES_SEARCH",
            "geoField": "source.geo.location",
            "filterByMapBounds": True,
            "scalingType": "TOP_HITS",
            "topHitsSplitField": "ais.mmsi.value",
            "topHitsSize": 1,
            "sortField": "@timestamp",
            "sortOrder": "desc",
            "tooltipProperties": [
                "@timestamp", "ais.ship_name", "ais.mmsi.value",
                "ais.mmsi.flag_country", "ais.ship_type_category",
                "ais.anomaly.mmsi_suspicious_pattern",
                "ais.anomaly.mmsi_category_message_mismatch",
                "ais.anomaly.mmsi_envelope_mismatch",
                "ais.anomaly.mmsi_invalid_range",
                "ais.anomaly.navstatus_speed_mismatch",
                "ais.anomaly.heading_cog_disagree",
                "ais.anomaly.sog_implausible_high",
                "ais.anomaly.null_island",
            ],
            "applyGlobalQuery": True,
            "applyGlobalTime": True,
            "applyForceRefresh": True,
            "indexPatternRefName": "layer_anomaly_source_index_pattern",
        },
        "style": {
            "type": "VECTOR",
            "properties": {
                "icon": {"type": "STATIC",
                         "options": {"value": "circle-stroked"}},
                "fillColor": {"type": "STATIC",
                              "options": {"color": "#e6194b"}},
                "lineColor": {"type": "STATIC",
                              "options": {"color": "#e6194b"}},
                "lineWidth": {"type": "STATIC", "options": {"size": 2}},
                "iconSize": {"type": "STATIC", "options": {"size": 12}},
                "symbolizeAs": {"options": {"value": "icon"}},
            },
            "isTimeAware": True,
        },
        "query": {"language": "kuery", "query": _ANY_ANOMALY_KQL},
        "type": "GEOJSON_VECTOR",
        "joins": [],
        "visible": True,
        "disableTooltips": False,
    }


def _observer_layer():
    """Static marker for the operator's reference-point coordinate (from
    YAML `observer.geo`).  Anchors `ais.range.*` bearings/distances.
    NOT a physical receiver — when ingesting from a cloud aggregator
    (aisstream.io) the reference point is whatever AOR anchor the
    operator chose (sector center, future DF antenna location, etc.)."""
    return {
        "id": "ais-observer-layer",
        "label": "Reference Point",
        "minZoom": 0, "maxZoom": 24, "alpha": 1.0,
        "sourceDescriptor": {
            "id": "ais-observer-source",
            "type": "ES_SEARCH",
            "geoField": "observer.geo.location",
            "filterByMapBounds": False,
            "scalingType": "TOP_HITS",
            "topHitsSplitField": "observer.name",
            "topHitsSize": 1,
            "sortField": "@timestamp",
            "sortOrder": "desc",
            "tooltipProperties": ["observer.name", "observer.hostname",
                                  "observer.vendor"],
            "applyGlobalQuery": False,
            "applyGlobalTime": True,
            "applyForceRefresh": True,
            "indexPatternRefName": "layer_observer_source_index_pattern",
        },
        "style": {
            "type": "VECTOR",
            "properties": {
                "icon": {"type": "STATIC", "options": {"value": "marker"}},
                "fillColor": {"type": "STATIC",
                              "options": {"color": "#2ca02c"}},
                "lineColor": {"type": "STATIC",
                              "options": {"color": "#FFFFFF"}},
                "lineWidth": {"type": "STATIC", "options": {"size": 1.5}},
                "iconSize": {"type": "STATIC", "options": {"size": 10}},
                "symbolizeAs": {"options": {"value": "icon"}},
            },
            "isTimeAware": True,
        },
        "type": "GEOJSON_VECTOR",
        "joins": [],
        "visible": True,
        "disableTooltips": False,
    }


def map_positions():
    # Layer order matters: later layers render on top.  Stack is:
    #   base map -> per-category vessel layers (unknown first, distinctive
    #   types on top so they're never hidden) -> AtoN -> anomaly rings ->
    #   distress -> observer marker.
    layer_list = [
        {
            "id": "basemap-layer",
            "label": "Roadmap",
            "minZoom": 0, "maxZoom": 24, "alpha": 1,
            "sourceDescriptor": {"type": "EMS_TMS",
                                 "isAutoSelect": True,
                                 "lightModeDefault": "road_map_desaturated"},
            "visible": True, "style": {"type": "TILE"},
            "type": "EMS_VECTOR_TILE",
        },
    ]
    for spec in _VESSEL_LAYER_GROUPS:
        layer_list.append(_vessel_layer(*spec))
    layer_list += [_aton_layer(), _anomaly_layer(),
                   _distress_layer(), _observer_layer()]
    map_state = {
        "zoom": 2,
        "center": {"lon": 0, "lat": 20},
        "timeFilters": {"from": "now-24h", "to": "now"},
        "refreshConfig": {"isPaused": False, "interval": 60000},
        "query": {"query": "", "language": "kuery"},
        "filters": [],
        "settings": {
            "autoFitToDataBounds": True,
            "backgroundColor": "#ffffff",
            "customIcons": _custom_icons(),
            "disableInteractive": False,
            "disableTooltipControl": False,
            "hideToolbarOverlay": False,
            "hideLayerControl": False,
            "hideViewControl": False,
            "initialLocation": "AUTO_FIT_TO_BOUNDS",
            "browserLocation": {"zoom": 2},
            "keydownScrollZoom": False,
            "maxZoom": 24, "minZoom": 0,
            "showScaleControl": True,
            "showSpatialFilters": True,
            "showTimesliderToggleButton": True,
            "spatialFiltersAlpa": 0.3,
            "spatialFiltersFillColor": "#DA8B45",
            "spatialFiltersLineColor": "#DA8B45",
        },
    }
    return base({
        "type": "map",
        "typeMigrationVersion": "8.4.0",
        "id": MAP_ID,
        "attributes": {
            "title": "AIS Vessel Positions",
            "description": "Last 24h vessel positions, type-iconed and oriented by COG; AtoN marks and distress events overlaid.",
            "layerListJSON": json.dumps(layer_list),
            "mapStateJSON": json.dumps(map_state),
            "uiStateJSON": json.dumps({"isLayerTOCOpen": True,
                                       "openTOCDetails": []}),
            "bounds": {"type": "polygon",
                       "coordinates": [[[-180, -85], [180, -85],
                                        [180, 85], [-180, 85],
                                        [-180, -85]]]},
        },
        "references": (
            [{"id": IP_ID,
              "name": f"layer_vessel_{spec[0]}_source_index_pattern",
              "type": "index-pattern"} for spec in _VESSEL_LAYER_GROUPS]
            + [
                {"id": IP_ID, "name": "layer_aton_source_index_pattern",
                 "type": "index-pattern"},
                {"id": IP_ID, "name": "layer_anomaly_source_index_pattern",
                 "type": "index-pattern"},
                {"id": IP_ID, "name": "layer_distress_source_index_pattern",
                 "type": "index-pattern"},
                {"id": IP_ID, "name": "layer_observer_source_index_pattern",
                 "type": "index-pattern"},
                {"id": TAG_ID, "name": "tag-ref-ais", "type": "tag"},
            ]
        ),
    })


# --- Lens: polar coverage (distance vs bearing heatmap from observer) -------

def lens_polar_coverage():
    layer_id = "ais-layer-polar-0001"
    state = {
        "datasourceStates": {
            "formBased": {
                "layers": {
                    layer_id: {
                        "columnOrder": ["col_bearing", "col_dist", "col_count"],
                        "columns": {
                            "col_bearing": {
                                "label": "Bearing (deg)",
                                "dataType": "number",
                                "operationType": "range",
                                "scale": "interval",
                                "sourceField": "ais.range.bearing_deg",
                                "isBucketed": True,
                                "params": {
                                    "type": "range",
                                    "maxBars": 36,
                                    "ranges": [
                                        {"from": 0, "to": 360,
                                         "label": "0-360"}
                                    ],
                                    "format": {"id": "number"},
                                },
                            },
                            "col_dist": {
                                "label": "Distance (nm)",
                                "dataType": "number",
                                "operationType": "range",
                                "scale": "interval",
                                "sourceField": "ais.range.distance_nm",
                                "isBucketed": True,
                                "params": {
                                    "type": "histogram",
                                    "maxBars": 20,
                                    "ranges": [],
                                    "format": {"id": "number"},
                                },
                            },
                            "col_count": {
                                "label": "Positions",
                                "dataType": "number",
                                "operationType": "count",
                                "scale": "ratio",
                                "sourceField": "___records___",
                                "isBucketed": False,
                                "customLabel": True,
                            },
                        },
                        "incompleteColumns": {},
                    }
                }
            }
        },
        "internalReferences": [],
        "adHocDataViews": {},
        "filters": [],
        "query": {"query": "ais.message_class: position",
                  "language": "kuery"},
        "visualization": {
            "shape": "heatmap",
            "layerId": layer_id,
            "layerType": "data",
            "legend": {"isVisible": True, "position": "right"},
            "gridConfig": {"type": "heatmap_grid",
                           "isCellLabelVisible": False,
                           "isYAxisLabelVisible": True,
                           "isXAxisLabelVisible": True,
                           "isYAxisTitleVisible": True,
                           "isXAxisTitleVisible": True},
            "valueAccessor": "col_count",
            "xAccessor": "col_bearing",
            "yAccessor": "col_dist",
        },
    }
    return base({
        "type": "lens",
        "typeMigrationVersion": "8.6.0",
        "id": LENS_POLAR_ID,
        "attributes": {
            "title": "Vessel Coverage (Bearing × Distance)",
            "description": "Polar heatmap of position-message density vs observer-relative bearing and distance.",
            "visualizationType": "lnsHeatmap",
            "state": state,
        },
        "references": [
            {"id": IP_ID,
             "name": f"indexpattern-datasource-layer-{layer_id}",
             "type": "index-pattern"},
            {"id": TAG_ID, "name": "tag-ref-ais", "type": "tag"},
        ],
    })


# --- Dashboard --------------------------------------------------------------

def dashboard(with_map=True):
    panels = []
    refs = [{"id": TAG_ID, "name": "tag-ref-ais", "type": "tag"}]
    y = 0
    if with_map:
        panels.append({
            "version": "8.17.0", "type": "map",
            "gridData": {"x": 0, "y": y, "w": 48, "h": 24, "i": "panel_map"},
            "panelIndex": "panel_map",
            "embeddableConfig": {"enhancements": {}, "isLayerTOCOpen": False,
                                 "hiddenLayers": []},
            "panelRefName": "panel_map",
        })
        refs.append({"id": MAP_ID, "name": "panel_map", "type": "map"})
        y += 24
    panels.append({
        "version": "8.17.0", "type": "lens",
        "gridData": {"x": 0, "y": y, "w": 48, "h": 16, "i": "panel_vessels"},
        "panelIndex": "panel_vessels",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_vessels",
    })
    refs.append({"id": LENS_VESSELS_ID, "name": "panel_vessels",
                 "type": "lens"})
    y += 16
    panels.append({
        "version": "8.17.0", "type": "lens",
        "gridData": {"x": 0, "y": y, "w": 24, "h": 16, "i": "panel_polar"},
        "panelIndex": "panel_polar",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_polar",
    })
    refs.append({"id": LENS_POLAR_ID, "name": "panel_polar", "type": "lens"})
    panels.append({
        "version": "8.17.0", "type": "lens",
        "gridData": {"x": 24, "y": y, "w": 24, "h": 16,
                     "i": "panel_distress_hr"},
        "panelIndex": "panel_distress_hr",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_distress_hr",
    })
    refs.append({"id": LENS_DISTRESS_BY_HR_ID, "name": "panel_distress_hr",
                 "type": "lens"})
    y += 16
    panels.append({
        "version": "8.17.0", "type": "search",
        "gridData": {"x": 0, "y": y, "w": 48, "h": 16,
                     "i": "panel_distress_search"},
        "panelIndex": "panel_distress_search",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_distress_search",
    })
    refs.append({"id": SEARCH_DISTRESS_ID, "name": "panel_distress_search",
                 "type": "search"})
    y += 16
    panels.append({
        "version": "8.17.0", "type": "search",
        "gridData": {"x": 0, "y": y, "w": 48, "h": 16,
                     "i": "panel_anomaly_search"},
        "panelIndex": "panel_anomaly_search",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_anomaly_search",
    })
    refs.append({"id": SEARCH_ANOMALY_ID, "name": "panel_anomaly_search",
                 "type": "search"})

    suffix = "" if with_map else " (no map)"
    return base({
        "type": "dashboard",
        "typeMigrationVersion": "10.2.0",
        "id": DASHBOARD_ID,
        "attributes": {
            "title": "AIS Maritime Watch" + suffix,
            "description": ("Vessel positions, vessels seen, polar coverage, "
                            "distress events, single-message anomalies."),
            "panelsJSON": json.dumps(panels),
            "timeRestore": False,
            "optionsJSON": json.dumps({
                "useMargins": True,
                "syncColors": False,
                "syncCursor": True,
                "syncTooltips": False,
                "hidePanelTitles": False,
            }),
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps({
                    "query": {"language": "kuery", "query": ""},
                    "filter": [],
                }),
            },
        },
        "references": refs,
    })


# ═══════════════════════════════════════════════════════════════════════════
# AIS Operational Watch dashboard — designed for a maritime SIGINT / sector
# watch role.  Three-question framing: where are ships, what type, who's
# misconfigured or up to something.
# ═══════════════════════════════════════════════════════════════════════════


def _lens_metric(lens_id, title, desc, agg_op, source_field,
                 kql="", time_filter_15m=False, color=None):
    """Build a Lens single-value metric saved object.

    *agg_op* is one of 'count', 'unique_count', 'max', 'percentile'.
    *source_field* is the field for count/max/percentile.  For 'count',
    set source_field to '___records___'.
    """
    layer_id = f"layer-{lens_id}"
    col = {
        "label": title,
        "dataType": "number",
        "operationType": agg_op,
        "scale": "ratio",
        "sourceField": source_field,
        "isBucketed": False,
        "customLabel": True,
    }
    if agg_op == "percentile":
        col["params"] = {"percentile": 95}
    state = {
        "datasourceStates": {"formBased": {"layers": {layer_id: {
            "columnOrder": ["col_metric"],
            "columns": {"col_metric": col},
            "incompleteColumns": {},
        }}}},
        "internalReferences": [],
        "adHocDataViews": {},
        "filters": [],
        "query": {"language": "kuery", "query": kql},
        "visualization": {
            "layerId": layer_id,
            "layerType": "data",
            "metricAccessor": "col_metric",
            "color": color or "#1f77b4",
            "subtitle": "last 15m" if time_filter_15m else None,
        },
    }
    return base({
        "type": "lens",
        "typeMigrationVersion": "8.6.0",
        "id": lens_id,
        "attributes": {
            "title": title,
            "description": desc,
            "visualizationType": "lnsMetric",
            "state": state,
        },
        "references": [
            {"id": IP_ID,
             "name": f"indexpattern-datasource-layer-{layer_id}",
             "type": "index-pattern"},
            {"id": TAG_ID, "name": "tag-ref-ais", "type": "tag"},
        ],
    })


def kpi_active_vessels():
    return _lens_metric(
        LENS_KPI_VESSELS_ID, "Active Vessels (15m)",
        "Distinct MMSIs broadcasting position in the last 15 minutes.",
        "unique_count", "ais.mmsi.value",
        kql='ais.mmsi.category: "ship" and ais.message_class: "position"',
        color="#1f77b4")


def kpi_msg_rate():
    return _lens_metric(
        LENS_KPI_MSGRATE_ID, "Messages (15m)",
        "Total AIS messages ingested in the last 15 minutes.",
        "count", "___records___", color="#7f7f7f")


def kpi_anomalies():
    return _lens_metric(
        LENS_KPI_ANOMALIES_ID, "Anomalies (15m)",
        "Distinct MMSIs flagged by any single-message anomaly.",
        "unique_count", "ais.mmsi.value", kql=_ANY_ANOMALY_KQL,
        color="#ff7f0e")


def kpi_distress():
    return _lens_metric(
        LENS_KPI_DISTRESS_ID, "Distress (1h)",
        "Distinct MMSIs in distress state in the last hour.",
        "unique_count", "ais.mmsi.value",
        kql="ais.distress.is_distress: true", color="#e6194b")


def kpi_max_range():
    return _lens_metric(
        LENS_KPI_RANGE_ID, "Max Range (nm)",
        "Greatest observed vessel range from the receiver.",
        "max", "ais.range.distance_nm", color="#2ca02c")


def kpi_feed_lag():
    return _lens_metric(
        LENS_KPI_LAG_ID, "Feed Lag p95 (s)",
        "95th-percentile delay between AIS event time and bridge ingest.",
        "percentile", "ais.lag_s", color="#9467bd")


def lens_msg_rate_by_class():
    """Stacked area: message count over time split by ais.message_class."""
    layer_id = "layer-msgrate-class-0001"
    state = {
        "datasourceStates": {"formBased": {"layers": {layer_id: {
            "columnOrder": ["col_time", "col_class", "col_count"],
            "columns": {
                "col_time": {
                    "label": "@timestamp",
                    "dataType": "date",
                    "operationType": "date_histogram",
                    "scale": "interval",
                    "sourceField": "@timestamp",
                    "isBucketed": True,
                    "params": {"interval": "auto",
                               "dropPartials": False,
                               "includeEmptyRows": True},
                },
                "col_class": {
                    "label": "message class",
                    "dataType": "string",
                    "operationType": "terms",
                    "scale": "ordinal",
                    "sourceField": "ais.message_class",
                    "isBucketed": True,
                    "params": {
                        "size": 10,
                        "orderBy": {"type": "column", "columnId": "col_count"},
                        "orderDirection": "desc",
                        "otherBucket": True, "missingBucket": False,
                        "parentFormat": {"id": "terms"},
                    },
                },
                "col_count": {
                    "label": "messages",
                    "dataType": "number",
                    "operationType": "count",
                    "scale": "ratio",
                    "sourceField": "___records___",
                    "isBucketed": False,
                    "customLabel": True,
                },
            },
            "incompleteColumns": {},
        }}}},
        "internalReferences": [], "adHocDataViews": {}, "filters": [],
        "query": {"language": "kuery", "query": ""},
        "visualization": {
            "preferredSeriesType": "area_stacked",
            "legend": {"isVisible": True, "position": "right"},
            "valueLabels": "hide",
            "fittingFunction": "None",
            "axisTitlesVisibilitySettings": {"x": True, "yLeft": True,
                                              "yRight": True},
            "tickLabelsVisibilitySettings": {"x": True, "yLeft": True,
                                              "yRight": True},
            "labelsOrientation": {"x": 0, "yLeft": 0, "yRight": 0},
            "gridlinesVisibilitySettings": {"x": True, "yLeft": True,
                                             "yRight": True},
            "layers": [{
                "layerId": layer_id,
                "accessors": ["col_count"],
                "position": "top",
                "seriesType": "area_stacked",
                "showGridlines": False,
                "layerType": "data",
                "xAccessor": "col_time",
                "splitAccessor": "col_class",
            }],
        },
    }
    return base({
        "type": "lens",
        "typeMigrationVersion": "8.6.0",
        "id": LENS_MSGRATE_CLASS_ID,
        "attributes": {
            "title": "Message Rate by Class",
            "description": "AIS message count over time, split by message class.",
            "visualizationType": "lnsXY",
            "state": state,
        },
        "references": [
            {"id": IP_ID,
             "name": f"indexpattern-datasource-layer-{layer_id}",
             "type": "index-pattern"},
            {"id": TAG_ID, "name": "tag-ref-ais", "type": "tag"},
        ],
    })


def _lens_categorical_bar(lens_id, title, desc, source_field, kql="",
                          size=15, agg_op="unique_count",
                          metric_field="ais.mmsi.value", shape="bar_horizontal"):
    """Reusable: top-N categorical breakdown rendered as a bar chart."""
    layer_id = f"layer-{lens_id}"
    state = {
        "datasourceStates": {"formBased": {"layers": {layer_id: {
            "columnOrder": ["col_bucket", "col_metric"],
            "columns": {
                "col_bucket": {
                    "label": title,
                    "dataType": "string",
                    "operationType": "terms",
                    "scale": "ordinal",
                    "sourceField": source_field,
                    "isBucketed": True,
                    "params": {
                        "size": size,
                        "orderBy": {"type": "column", "columnId": "col_metric"},
                        "orderDirection": "desc",
                        "otherBucket": False, "missingBucket": False,
                        "parentFormat": {"id": "terms"},
                    },
                },
                "col_metric": {
                    "label": "vessels" if agg_op == "unique_count" else "messages",
                    "dataType": "number",
                    "operationType": agg_op,
                    "scale": "ratio",
                    "sourceField": metric_field,
                    "isBucketed": False,
                    "customLabel": True,
                },
            },
            "incompleteColumns": {},
        }}}},
        "internalReferences": [], "adHocDataViews": {}, "filters": [],
        "query": {"language": "kuery", "query": kql},
        "visualization": {
            "preferredSeriesType": shape,
            "legend": {"isVisible": False, "position": "right"},
            "valueLabels": "show",
            "fittingFunction": "None",
            "axisTitlesVisibilitySettings": {"x": True, "yLeft": True,
                                              "yRight": True},
            "tickLabelsVisibilitySettings": {"x": True, "yLeft": True,
                                              "yRight": True},
            "labelsOrientation": {"x": 0, "yLeft": 0, "yRight": 0},
            "gridlinesVisibilitySettings": {"x": True, "yLeft": True,
                                             "yRight": True},
            "layers": [{
                "layerId": layer_id,
                "accessors": ["col_metric"],
                "position": "top",
                "seriesType": shape,
                "showGridlines": False,
                "layerType": "data",
                "xAccessor": "col_bucket",
            }],
        },
    }
    return base({
        "type": "lens",
        "typeMigrationVersion": "8.6.0",
        "id": lens_id,
        "attributes": {
            "title": title,
            "description": desc,
            "visualizationType": "lnsXY",
            "state": state,
        },
        "references": [
            {"id": IP_ID,
             "name": f"indexpattern-datasource-layer-{layer_id}",
             "type": "index-pattern"},
            {"id": TAG_ID, "name": "tag-ref-ais", "type": "tag"},
        ],
    })


def lens_shiptype_donut():
    return _lens_categorical_bar(
        LENS_SHIPTYPE_DONUT_ID,
        "Ship Type Categories",
        "Distinct vessels in the AOR by ship_type_category.",
        "ais.ship_type_category", size=15)


def lens_flag_bar():
    return _lens_categorical_bar(
        LENS_FLAG_BAR_ID,
        "Top Flag Countries",
        "Distinct vessels by MMSI MID-derived flag country.",
        "ais.mmsi.flag_country", size=15)


def lens_navstatus_bar():
    return _lens_categorical_bar(
        LENS_NAVSTATUS_BAR_ID,
        "Navigation Status Mix",
        "Distinct vessels by navigation status (position reports only).",
        "ais.nav_status", size=15,
        kql='ais.message_class: "position"')


def lens_anomaly_counts():
    """One bar per anomaly type, value = number of distinct MMSIs affected.

    Uses Lens 'filters' bucket agg — each anomaly field becomes its own
    bucket so the chart shows side-by-side counts without needing to scroll
    through individual KPIs.
    """
    layer_id = "layer-anomaly-counts-0001"
    anomaly_fields = [
        ("Suspicious MMSI pattern", "ais.anomaly.mmsi_suspicious_pattern: true"),
        ("Identity changed",        "ais.anomaly.identity_changed: true"),
        ("Category mismatch",       "ais.anomaly.mmsi_category_message_mismatch: true"),
        ("MMSI envelope mismatch",  "ais.anomaly.mmsi_envelope_mismatch: true"),
        ("Invalid MMSI",            "ais.anomaly.mmsi_invalid_range: true"),
        ("NavStatus vs SOG",        "ais.anomaly.navstatus_speed_mismatch: true"),
        ("Heading vs COG",          "ais.anomaly.heading_cog_disagree: true"),
        ("Implausibly fast",        "ais.anomaly.sog_implausible_high: true"),
        ("Null island",             "ais.anomaly.null_island: true"),
    ]
    filters = [{"input": {"language": "kuery", "query": q}, "label": lbl}
               for lbl, q in anomaly_fields]
    state = {
        "datasourceStates": {"formBased": {"layers": {layer_id: {
            "columnOrder": ["col_filters", "col_count"],
            "columns": {
                "col_filters": {
                    "label": "anomaly",
                    "dataType": "string",
                    "operationType": "filters",
                    "scale": "ordinal",
                    "isBucketed": True,
                    "params": {"filters": filters},
                },
                "col_count": {
                    "label": "vessels",
                    "dataType": "number",
                    "operationType": "unique_count",
                    "scale": "ratio",
                    "sourceField": "ais.mmsi.value",
                    "isBucketed": False,
                    "customLabel": True,
                },
            },
            "incompleteColumns": {},
        }}}},
        "internalReferences": [], "adHocDataViews": {}, "filters": [],
        "query": {"language": "kuery", "query": ""},
        "visualization": {
            "preferredSeriesType": "bar_horizontal",
            "legend": {"isVisible": False, "position": "right"},
            "valueLabels": "show",
            "fittingFunction": "None",
            "axisTitlesVisibilitySettings": {"x": True, "yLeft": True,
                                              "yRight": True},
            "tickLabelsVisibilitySettings": {"x": True, "yLeft": True,
                                              "yRight": True},
            "labelsOrientation": {"x": 0, "yLeft": 0, "yRight": 0},
            "gridlinesVisibilitySettings": {"x": True, "yLeft": True,
                                             "yRight": True},
            "layers": [{
                "layerId": layer_id,
                "accessors": ["col_count"],
                "position": "top",
                "seriesType": "bar_horizontal",
                "showGridlines": False,
                "layerType": "data",
                "xAccessor": "col_filters",
            }],
        },
    }
    return base({
        "type": "lens",
        "typeMigrationVersion": "8.6.0",
        "id": LENS_ANOMALY_COUNTS_ID,
        "attributes": {
            "title": "Anomalies by Type",
            "description": "Distinct vessels affected per single-message anomaly.",
            "visualizationType": "lnsXY",
            "state": state,
        },
        "references": [
            {"id": IP_ID,
             "name": f"indexpattern-datasource-layer-{layer_id}",
             "type": "index-pattern"},
            {"id": TAG_ID, "name": "tag-ref-ais", "type": "tag"},
        ],
    })


def lens_distress_severity():
    return _lens_categorical_bar(
        LENS_DISTRESS_SEV_ID,
        "Distress by Source",
        "Distinct vessels in distress, broken out by detection source.",
        "ais.distress.source", size=10,
        kql="ais.distress.is_distress: true",
        shape="bar")


def _saved_search(search_id, title, desc, columns, kql):
    return base({
        "type": "search",
        "typeMigrationVersion": "10.5.0",
        "id": search_id,
        "attributes": {
            "title": title, "description": desc,
            "columns": columns,
            "sort": [["@timestamp", "desc"]],
            "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps({
                "query": {"language": "kuery", "query": kql},
                "filter": [],
                "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
            })},
        },
        "references": [
            {"id": IP_ID,
             "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
             "type": "index-pattern"},
            {"id": TAG_ID, "name": "tag-ref-ais", "type": "tag"},
        ],
    })


def search_suspicious_identity():
    return _saved_search(
        SEARCH_SUSP_IDENTITY_ID,
        "Suspicious Identity",
        "Vessels with placeholder, mismatched, or invalid MMSIs — primary "
        "AIS-deception tell.",
        ["ais.mmsi.value", "ais.ship_name", "ais.mmsi.flag_country",
         "ais.ship_type_category", "ais.message_type",
         "ais.anomaly.mmsi_suspicious_pattern",
         "ais.anomaly.mmsi_category_message_mismatch",
         "ais.anomaly.mmsi_invalid_range",
         "ais.anomaly.mmsi_envelope_mismatch",
         "ais.anomaly.identity_changed",
         "ais.anomaly.previous_identity.ship_name",
         "ais.anomaly.previous_identity.call_sign",
         "ais.anomaly.previous_identity.imo",
         "ais.anomaly.previous_identity.ship_type_raw",
         "source.geo.location"],
        _SUSP_IDENTITY_KQL)


def search_kinematic_inconsistency():
    return _saved_search(
        SEARCH_KINEMATIC_ID,
        "Kinematic Inconsistency",
        "Vessels reporting nav-status / speed / heading combinations that "
        "don't add up — behavioral spoof or sensor failure indicators.",
        ["ais.mmsi.value", "ais.ship_name", "ais.nav_status", "ais.sog_kt",
         "ais.cog_deg", "ais.true_heading_deg", "ais.ship_type_category",
         "ais.anomaly.navstatus_speed_mismatch",
         "ais.anomaly.heading_cog_disagree",
         "ais.anomaly.sog_implausible_high", "source.geo.location"],
        _KINEMATIC_KQL)


def search_aton_off_position():
    return _saved_search(
        SEARCH_ATON_OFF_ID,
        "Aids to Navigation Off-Position",
        "Aids to Navigation (AtoN) reporting themselves as drifted "
        "(off-position) or marked virtual — nav hazards and potential "
        "spoofing indicators.",
        ["ais.aton.name", "ais.aton.type", "ais.mmsi.value",
         "ais.aton.off_position", "ais.aton.virtual", "source.geo.location"],
        'ais.mmsi.category: "aton" and (ais.aton.off_position: true or ais.aton.virtual: true)')


def search_active_vessels():
    """Primary 'show me all active vessels' table — every position-bearing
    record in the current time window with the columns an operator would
    use to identify, classify, and locate a vessel.

    Kibana's table-cell filters work on this directly: click any
    flag_country / ship_type_category / nav_status value and a
    matching filter pins to the dashboard, narrowing every other
    panel at the same time."""
    return _saved_search(
        SEARCH_ACTIVE_VESSELS_ID,
        "Active Vessels",
        "All vessels with a position report in the current time window. "
        "Click any value (flag, type, nav status, etc.) to pin it as a "
        "dashboard filter.",
        [
            "ais.ship_name", "ais.mmsi.value",
            "ais.mmsi.flag_iso_code", "ais.mmsi.flag_country",
            "ais.ship_type_category", "ais.ship_type",
            "ais.nav_status", "ais.sog_kt", "ais.cog_deg",
            "ais.true_heading_deg",
            "ais.range.distance_nm", "ais.range.bearing_cardinal",
            "ais.call_sign", "ais.imo", "ais.destination",
            "ais.dimensions.length_m", "source.geo.location",
        ],
        'ais.message_class: "position" and ais.position.valid: true')


def dashboard_operational():
    """AIS Operational Watch — sector-watch style dashboard."""
    panels = []
    refs = [{"id": TAG_ID, "name": "tag-ref-ais", "type": "tag"}]

    # Row 1 — KPI strip (6 cells, 8 wide each)
    kpi_specs = [
        (LENS_KPI_VESSELS_ID,   "panel_kpi_vessels",   "Active Vessels"),
        (LENS_KPI_MSGRATE_ID,   "panel_kpi_msgrate",   "Message Volume"),
        (LENS_KPI_ANOMALIES_ID, "panel_kpi_anomalies", "Anomalies"),
        (LENS_KPI_DISTRESS_ID,  "panel_kpi_distress",  "Distress"),
        (LENS_KPI_RANGE_ID,     "panel_kpi_range",     "Max Range"),
        (LENS_KPI_LAG_ID,       "panel_kpi_lag",       "Feed Lag"),
    ]
    for i, (lid, pname, ptitle) in enumerate(kpi_specs):
        panels.append({
            "version": "8.17.0", "type": "lens",
            "gridData": {"x": i * 8, "y": 0, "w": 8, "h": 8, "i": pname},
            "panelIndex": pname,
            "title": ptitle,
            "embeddableConfig": {"enhancements": {}},
            "panelRefName": pname,
        })
        refs.append({"id": lid, "name": pname, "type": "lens"})

    # Row 2 — message flow + vessel mix
    y = 8
    panels.append({
        "version": "8.17.0", "type": "lens",
        "gridData": {"x": 0, "y": y, "w": 24, "h": 14, "i": "panel_msgrate_class"},
        "panelIndex": "panel_msgrate_class",
        "title": "Message Rate by Class",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_msgrate_class",
    })
    refs.append({"id": LENS_MSGRATE_CLASS_ID,
                 "name": "panel_msgrate_class", "type": "lens"})
    panels.append({
        "version": "8.17.0", "type": "lens",
        "gridData": {"x": 24, "y": y, "w": 12, "h": 14, "i": "panel_shiptype"},
        "panelIndex": "panel_shiptype",
        "title": "Ship Type Categories",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_shiptype",
    })
    refs.append({"id": LENS_SHIPTYPE_DONUT_ID,
                 "name": "panel_shiptype", "type": "lens"})
    panels.append({
        "version": "8.17.0", "type": "lens",
        "gridData": {"x": 36, "y": y, "w": 12, "h": 14, "i": "panel_flags"},
        "panelIndex": "panel_flags",
        "title": "Top Flag Countries",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_flags",
    })
    refs.append({"id": LENS_FLAG_BAR_ID,
                 "name": "panel_flags", "type": "lens"})

    # Row 3 — nav status + anomaly summary
    y = 22
    panels.append({
        "version": "8.17.0", "type": "lens",
        "gridData": {"x": 0, "y": y, "w": 24, "h": 14, "i": "panel_navstatus"},
        "panelIndex": "panel_navstatus",
        "title": "Navigation Status Mix",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_navstatus",
    })
    refs.append({"id": LENS_NAVSTATUS_BAR_ID,
                 "name": "panel_navstatus", "type": "lens"})
    panels.append({
        "version": "8.17.0", "type": "lens",
        "gridData": {"x": 24, "y": y, "w": 24, "h": 14, "i": "panel_anomaly_counts"},
        "panelIndex": "panel_anomaly_counts",
        "title": "Anomalies by Type",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_anomaly_counts",
    })
    refs.append({"id": LENS_ANOMALY_COUNTS_ID,
                 "name": "panel_anomaly_counts", "type": "lens"})

    # Row 4 — Active Vessels (the primary "show me all ships" table,
    # interactively filterable per cell value)
    y = 36
    panels.append({
        "version": "8.17.0", "type": "search",
        "gridData": {"x": 0, "y": y, "w": 48, "h": 20,
                     "i": "panel_active_vessels"},
        "panelIndex": "panel_active_vessels",
        "title": "Active Vessels",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_active_vessels",
    })
    refs.append({"id": SEARCH_ACTIVE_VESSELS_ID,
                 "name": "panel_active_vessels", "type": "search"})

    # Row 5 — suspicious identity + kinematic inconsistency tables
    y = 56
    panels.append({
        "version": "8.17.0", "type": "search",
        "gridData": {"x": 0, "y": y, "w": 24, "h": 16, "i": "panel_susp_id"},
        "panelIndex": "panel_susp_id",
        "title": "Suspicious Identity",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_susp_id",
    })
    refs.append({"id": SEARCH_SUSP_IDENTITY_ID,
                 "name": "panel_susp_id", "type": "search"})
    panels.append({
        "version": "8.17.0", "type": "search",
        "gridData": {"x": 24, "y": y, "w": 24, "h": 16, "i": "panel_kinematic"},
        "panelIndex": "panel_kinematic",
        "title": "Kinematic Inconsistency",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_kinematic",
    })
    refs.append({"id": SEARCH_KINEMATIC_ID,
                 "name": "panel_kinematic", "type": "search"})

    # Row 6 — distress detail + distress source breakdown
    y = 72
    panels.append({
        "version": "8.17.0", "type": "search",
        "gridData": {"x": 0, "y": y, "w": 36, "h": 14, "i": "panel_distress_detail"},
        "panelIndex": "panel_distress_detail",
        "title": "Active Distress",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_distress_detail",
    })
    refs.append({"id": SEARCH_DISTRESS_ID,
                 "name": "panel_distress_detail", "type": "search"})
    panels.append({
        "version": "8.17.0", "type": "lens",
        "gridData": {"x": 36, "y": y, "w": 12, "h": 14, "i": "panel_distress_src"},
        "panelIndex": "panel_distress_src",
        "title": "Distress by Source",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_distress_src",
    })
    refs.append({"id": LENS_DISTRESS_SEV_ID,
                 "name": "panel_distress_src", "type": "lens"})

    # Row 7 — Aids to Navigation drift watch
    y = 86
    panels.append({
        "version": "8.17.0", "type": "search",
        "gridData": {"x": 0, "y": y, "w": 48, "h": 12, "i": "panel_aton_off"},
        "panelIndex": "panel_aton_off",
        "title": "Aids to Navigation (AtoN) — Off-Position / Virtual",
        "embeddableConfig": {"enhancements": {}},
        "panelRefName": "panel_aton_off",
    })
    refs.append({"id": SEARCH_ATON_OFF_ID,
                 "name": "panel_aton_off", "type": "search"})

    return base({
        "type": "dashboard",
        "typeMigrationVersion": "10.2.0",
        "id": DASHBOARD_OPS_ID,
        "attributes": {
            "title": "AIS Operational Watch",
            "description": ("Sector-watch view: vessel population, type and "
                            "flag mix, nav-status, single-message anomalies, "
                            "suspicious-identity & kinematic-inconsistency "
                            "watchlists, active distress, AtoN drift."),
            "panelsJSON": json.dumps(panels),
            # 30m is the smallest window that reliably surfaces Class B
            # static-data broadcasts (which fire every ~6m but can be
            # bursty).  Operators routinely override via the time picker.
            "timeRestore": True,
            "timeFrom": "now-30m",
            "timeTo": "now",
            "refreshInterval": {"pause": False, "value": 30000},
            "optionsJSON": json.dumps({
                "useMargins": True, "syncColors": False,
                "syncCursor": True, "syncTooltips": False,
                "hidePanelTitles": False,
            }),
            "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps({
                "query": {"language": "kuery", "query": ""},
                "filter": [],
            })},
        },
        "references": refs,
    })


# ═══════════════════════════════════════════════════════════════════════════


def _write(path, objs):
    out = "\n".join(json.dumps(o, separators=(",", ":")) for o in objs) + "\n"
    with open(path, "w") as f:
        f.write(out)
    print(f"wrote {path} — {len(objs)} objects")


def main():
    # Objects that are 100% portable between Kibana and OpenSearch Dashboards.
    common = [
        tag(), index_pattern(),
        # AIS Maritime Watch supporting viz
        lens_vessels_seen(), lens_polar_coverage(),
        lens_distress_over_time(),
        search_distress(), search_anomalies(),
        # AIS Operational Watch — KPIs
        kpi_active_vessels(), kpi_msg_rate(), kpi_anomalies(),
        kpi_distress(), kpi_max_range(), kpi_feed_lag(),
        # AIS Operational Watch — situational
        lens_msg_rate_by_class(), lens_shiptype_donut(),
        lens_flag_bar(), lens_navstatus_bar(),
        lens_anomaly_counts(), lens_distress_severity(),
        # AIS Operational Watch — watchlists
        search_active_vessels(),
        search_suspicious_identity(), search_kinematic_inconsistency(),
        search_aton_off_position(),
        # Operational dashboard itself
        dashboard_operational(),
    ]

    # Kibana — full version with Elastic Maps "map" saved-object.
    _write("kibana_saved_objects.ndjson",
           common + [map_positions(), dashboard(with_map=True)])

    # OpenSearch Dashboards — same lens/search/dashboard set; the OS Maps
    # plugin uses an undocumented saved-object schema that diverges between
    # OSD releases.  Ship the dashboard without the map and let the operator
    # add a Maps panel from the OSD UI (Maps app -> Add layer -> Documents,
    # geo field source.geo.location).  Lens vessel-table, polar coverage,
    # distress timeline, and the saved searches all render natively.
    _write("opensearch_saved_objects.ndjson",
           common + [dashboard(with_map=False)])


if __name__ == "__main__":
    main()
