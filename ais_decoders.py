#!/usr/bin/python3

"""ais_decoders — Stateless decoders for AIS message fields.

Pure-function decoders for MMSI category, ITU Maritime Identification Digits
(MID) → flag country, nav-status, ship-type, AtoN type, EPFD, message class,
and single-message distress / anomaly detection.  No I/O, no caches — safe
to call from any thread on the hot path of the indexer.
"""

import re

# ---------------------------------------------------------------------------
# Sentinels (ITU-R M.1371)
# ---------------------------------------------------------------------------

LAT_NOT_AVAILABLE = 91.0
LON_NOT_AVAILABLE = 181.0
SOG_NOT_AVAILABLE = 102.3
SOG_HIGH_SENTINEL = 102.2  # >= 102.2 kt
COG_NOT_AVAILABLE = 360.0
TRUE_HEADING_NOT_AVAILABLE = 511
ROT_NOT_AVAILABLE = -128
TIMESTAMP_NOT_AVAILABLE = 60
TIMESTAMP_MANUAL = 61
TIMESTAMP_DEAD_RECKONING = 62
TIMESTAMP_INOPERATIVE = 63

# ---------------------------------------------------------------------------
# Distress keyword set (case-insensitive, matched against safety message text)
# ---------------------------------------------------------------------------

_DISTRESS_KEYWORDS_CRITICAL = (
    "MAYDAY", "M'AIDER", "SOS", "DISTRESS", "SINKING",
    "ABANDON SHIP", "ABANDONING", "ON FIRE",
)
_DISTRESS_KEYWORDS_HIGH = ("PAN PAN", "PAN-PAN", "PANPAN", "MEDICO")
_DISTRESS_KEYWORDS_MEDIUM = ("SECURITE", "SECURITÉ")


# ---------------------------------------------------------------------------
# Cardinal direction
# ---------------------------------------------------------------------------

_CARDINALS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def bearing_to_cardinal(deg: float) -> str:
    """16-point compass direction for a bearing in degrees."""
    return _CARDINALS[round(deg / 22.5) % 16]


# ---------------------------------------------------------------------------
# Navigation status (ITU-R M.1371 Table 45)
# ---------------------------------------------------------------------------

_NAV_STATUS = {
    0: "under_way_using_engine",
    1: "at_anchor",
    2: "not_under_command",
    3: "restricted_maneuverability",
    4: "constrained_by_draught",
    5: "moored",
    6: "aground",
    7: "engaged_in_fishing",
    8: "under_way_sailing",
    9: "reserved_hsc",
    10: "reserved_wig",
    11: "power_driven_towing_astern",
    12: "power_driven_pushing_ahead",
    13: "reserved",
    14: "ais_sart_active",
    15: "undefined",
}

# Nav statuses that imply the vessel should NOT be moving (>3kt = anomaly)
_NAV_STATUS_STATIONARY = frozenset({1, 5, 6})


def decode_nav_status(raw: int) -> str:
    """Return human-readable nav status keyword."""
    return _NAV_STATUS.get(raw, "undefined")


def nav_status_implies_stationary(raw: int) -> bool:
    return raw in _NAV_STATUS_STATIONARY


# ---------------------------------------------------------------------------
# EPFD / FixType (ITU-R M.1371 Table 24)
# ---------------------------------------------------------------------------

_EPFD = {
    0: "undefined",
    1: "gps",
    2: "glonass",
    3: "gps_glonass_combined",
    4: "loran_c",
    5: "chayka",
    6: "integrated_navigation",
    7: "surveyed",
    8: "galileo",
    15: "internal_gnss",
}


def decode_epfd(raw: int) -> str:
    return _EPFD.get(raw, "undefined")


# ---------------------------------------------------------------------------
# Ship type (ITU-R M.1371 Table 53) and category rollup
# ---------------------------------------------------------------------------

_SHIP_TYPE_CATEGORY = {
    # 0 not available
    0: ("not_available", "unspecified"),
    # 1-19 reserved
    # 20-29 WIG (Wing in Ground)
    **{n: ("wig", "wig") for n in range(20, 30)},
    # 30 fishing, 31 towing, 32 towing-long, 33 dredging, 34 diving,
    # 35 military, 36 sailing, 37 pleasure, 38-39 reserved
    30: ("fishing", "fishing"),
    31: ("towing", "towing"),
    32: ("towing_long_or_wide", "towing"),
    33: ("dredging_underwater_ops", "dredging"),
    34: ("diving_ops", "diving"),
    35: ("military_ops", "military"),
    36: ("sailing", "sailing"),
    37: ("pleasure_craft", "pleasure"),
    # 40-49 HSC (High Speed Craft)
    **{n: ("hsc", "hsc") for n in range(40, 50)},
    # 50-59 special craft
    50: ("pilot_vessel", "pilot"),
    51: ("search_and_rescue", "sar"),
    52: ("tug", "tug"),
    53: ("port_tender", "port_tender"),
    54: ("anti_pollution", "anti_pollution"),
    55: ("law_enforcement", "law_enforcement"),
    56: ("local_vessel", "other"),
    57: ("local_vessel", "other"),
    58: ("medical_transport", "medical"),
    59: ("noncombatant", "military"),
    # 60-69 passenger
    **{n: ("passenger", "passenger") for n in range(60, 70)},
    # 70-79 cargo
    70: ("cargo", "cargo"),
    71: ("cargo_hazard_a", "cargo"),
    72: ("cargo_hazard_b", "cargo"),
    73: ("cargo_hazard_c", "cargo"),
    74: ("cargo_hazard_d", "cargo"),
    75: ("cargo_reserved", "cargo"),
    76: ("cargo_reserved", "cargo"),
    77: ("cargo_reserved", "cargo"),
    78: ("cargo_reserved", "cargo"),
    79: ("cargo_no_additional_info", "cargo"),
    # 80-89 tanker
    80: ("tanker", "tanker"),
    81: ("tanker_hazard_a", "tanker"),
    82: ("tanker_hazard_b", "tanker"),
    83: ("tanker_hazard_c", "tanker"),
    84: ("tanker_hazard_d", "tanker"),
    85: ("tanker_reserved", "tanker"),
    86: ("tanker_reserved", "tanker"),
    87: ("tanker_reserved", "tanker"),
    88: ("tanker_reserved", "tanker"),
    89: ("tanker_no_additional_info", "tanker"),
    # 90-99 other
    **{n: ("other", "other") for n in range(90, 100)},
}


def decode_ship_type(raw):
    """Return (label, category) for an AIS ship-type integer 0-99."""
    if raw is None:
        return None, None
    return _SHIP_TYPE_CATEGORY.get(int(raw), ("unknown", "other"))


# ---------------------------------------------------------------------------
# AtoN type (ITU-R M.1371 Table 70)
# ---------------------------------------------------------------------------

_ATON_TYPE = {
    0: "default_not_specified",
    1: "reference_point",
    2: "racon",
    3: "fixed_structure_offshore",
    4: "reserved",
    5: "fixed_light_no_sectors",
    6: "fixed_light_sectors",
    7: "fixed_leading_light_front",
    8: "fixed_leading_light_rear",
    9: "fixed_beacon_cardinal_n",
    10: "fixed_beacon_cardinal_e",
    11: "fixed_beacon_cardinal_s",
    12: "fixed_beacon_cardinal_w",
    13: "fixed_beacon_port",
    14: "fixed_beacon_starboard",
    15: "fixed_beacon_preferred_channel_port",
    16: "fixed_beacon_preferred_channel_starboard",
    17: "fixed_beacon_isolated_danger",
    18: "fixed_beacon_safe_water",
    19: "fixed_beacon_special_mark",
    20: "floating_cardinal_n",
    21: "floating_cardinal_e",
    22: "floating_cardinal_s",
    23: "floating_cardinal_w",
    24: "floating_port",
    25: "floating_starboard",
    26: "floating_preferred_channel_port",
    27: "floating_preferred_channel_starboard",
    28: "floating_isolated_danger",
    29: "floating_safe_water",
    30: "floating_special_mark",
    31: "lightvessel_lanby_rigs",
}


def decode_aton_type(raw: int) -> str:
    return _ATON_TYPE.get(raw, "unknown")


# ---------------------------------------------------------------------------
# Message family rollup
# ---------------------------------------------------------------------------

_MESSAGE_CLASS = {
    "PositionReport": "position",
    "StandardClassBPositionReport": "position",
    "ExtendedClassBPositionReport": "position",
    "LongRangeAisBroadcastMessage": "position",
    "ShipStaticData": "static",
    "StaticDataReport": "static",
    "AddressedSafetyMessage": "safety",
    "SafetyBroadcastMessage": "safety",
    "AidsToNavigationReport": "aton",
    "StandardSearchAndRescueAircraftReport": "sar",
    "BaseStationReport": "base_station",
    "AddressedBinaryMessage": "binary",
    "BinaryBroadcastMessage": "binary",
    "BinaryAcknowledge": "binary",
    "SingleSlotBinaryMessage": "binary",
    "MultiSlotBinaryMessage": "binary",
    "GnssBroadcastBinaryMessage": "binary",
    "DataLinkManagementMessage": "link_layer",
    "ChannelManagement": "link_layer",
    "GroupAssignmentCommand": "link_layer",
    "Interrogation": "link_layer",
    "AssignedModeCommand": "link_layer",
    "CoordinatedUTCInquiry": "link_layer",
    "UnknownMessage": "other",
}


def message_class(message_type: str) -> str:
    return _MESSAGE_CLASS.get(message_type, "other")


# ---------------------------------------------------------------------------
# MMSI category & ITU Maritime Identification Digits
# ---------------------------------------------------------------------------

# ITU MID table (positions 1-3 of a 9-digit ship MMSI, or positions 2-4 of
# certain non-ship MMSI categories).  Sourced from the ITU MID Wikipedia
# table; ranges expanded to individual MID -> (country_name, iso_alpha_2).
def _expand_mids(pairs):
    out = {}
    for mids, name, iso in pairs:
        for m in mids:
            out[m] = (name, iso)
    return out


_MID_PAIRS = [
    # Europe
    ([201], "Albania", "AL"),
    ([202], "Andorra", "AD"),
    ([203], "Austria", "AT"),
    ([204], "Azores", "PT"),
    ([205], "Belgium", "BE"),
    ([206], "Belarus", "BY"),
    ([207], "Bulgaria", "BG"),
    ([208], "Vatican City", "VA"),
    ([209, 210, 212], "Cyprus", "CY"),
    ([211, 218], "Germany", "DE"),
    ([213], "Georgia", "GE"),
    ([214], "Moldova", "MD"),
    ([215, 229, 248, 249, 256], "Malta", "MT"),
    ([216], "Armenia", "AM"),
    ([219, 220], "Denmark", "DK"),
    ([224, 225], "Spain", "ES"),
    ([226, 227, 228], "France", "FR"),
    ([230], "Finland", "FI"),
    ([231], "Faroe Islands", "FO"),
    ([232, 233, 234, 235], "United Kingdom", "GB"),
    ([236], "Gibraltar", "GI"),
    ([237, 239, 240, 241], "Greece", "GR"),
    ([238], "Croatia", "HR"),
    ([242], "Morocco", "MA"),
    ([243], "Hungary", "HU"),
    ([244, 245, 246], "Netherlands", "NL"),
    ([247], "Italy", "IT"),
    ([250], "Ireland", "IE"),
    ([251], "Iceland", "IS"),
    ([252], "Liechtenstein", "LI"),
    ([253], "Luxembourg", "LU"),
    ([254], "Monaco", "MC"),
    ([255], "Madeira", "PT"),
    ([257, 258, 259], "Norway", "NO"),
    ([261], "Poland", "PL"),
    ([262], "Montenegro", "ME"),
    ([263], "Portugal", "PT"),
    ([264], "Romania", "RO"),
    ([265, 266], "Sweden", "SE"),
    ([267], "Slovakia", "SK"),
    ([268], "San Marino", "SM"),
    ([269], "Switzerland", "CH"),
    ([270], "Czech Republic", "CZ"),
    ([271], "Turkey", "TR"),
    ([272], "Ukraine", "UA"),
    ([273], "Russian Federation", "RU"),
    ([274], "North Macedonia", "MK"),
    ([275], "Latvia", "LV"),
    ([276], "Estonia", "EE"),
    ([277], "Lithuania", "LT"),
    ([278], "Slovenia", "SI"),
    ([279], "Serbia", "RS"),
    # North America & Caribbean
    ([303], "Alaska", "US"),
    ([304, 305], "Antigua and Barbuda", "AG"),
    ([306], "Curacao/Sint Maarten/Bonaire", "NL"),
    ([307], "Aruba", "AW"),
    ([308, 309, 311], "Bahamas", "BS"),
    ([310], "Bermuda", "BM"),
    ([312], "Belize", "BZ"),
    ([314], "Barbados", "BB"),
    ([316], "Canada", "CA"),
    ([319], "Cayman Islands", "KY"),
    ([321], "Costa Rica", "CR"),
    ([323], "Cuba", "CU"),
    ([325], "Dominica", "DM"),
    ([327], "Dominican Republic", "DO"),
    ([329], "Guadeloupe", "GP"),
    ([330], "Grenada", "GD"),
    ([331], "Greenland", "GL"),
    ([332], "Guatemala", "GT"),
    ([334], "Honduras", "HN"),
    ([336], "Haiti", "HT"),
    ([338, 366, 367, 368, 369], "United States", "US"),
    ([339], "Jamaica", "JM"),
    ([341], "Saint Kitts and Nevis", "KN"),
    ([343], "Saint Lucia", "LC"),
    ([345], "Mexico", "MX"),
    ([347], "Martinique", "MQ"),
    ([348], "Montserrat", "MS"),
    ([350], "Nicaragua", "NI"),
    ([351, 352, 353, 354, 355, 356, 357, 370, 371, 372, 373, 374], "Panama", "PA"),
    ([358], "Puerto Rico", "PR"),
    ([359], "El Salvador", "SV"),
    ([361], "Saint Pierre and Miquelon", "PM"),
    ([362], "Trinidad and Tobago", "TT"),
    ([364], "Turks and Caicos Islands", "TC"),
    ([375, 376, 377], "Saint Vincent and the Grenadines", "VC"),
    ([378], "British Virgin Islands", "VG"),
    ([379], "United States Virgin Islands", "VI"),
    # Asia
    ([401], "Afghanistan", "AF"),
    ([403], "Saudi Arabia", "SA"),
    ([405], "Bangladesh", "BD"),
    ([408], "Bahrain", "BH"),
    ([410], "Bhutan", "BT"),
    ([412, 413, 414], "China", "CN"),
    ([416], "Taiwan", "TW"),
    ([417], "Sri Lanka", "LK"),
    ([419], "India", "IN"),
    ([422], "Iran", "IR"),
    ([423], "Azerbaijan", "AZ"),
    ([425], "Iraq", "IQ"),
    ([428], "Israel", "IL"),
    ([431, 432], "Japan", "JP"),
    ([434], "Turkmenistan", "TM"),
    ([436], "Kazakhstan", "KZ"),
    ([437], "Uzbekistan", "UZ"),
    ([438], "Jordan", "JO"),
    ([440, 441], "South Korea", "KR"),
    ([443], "Palestinian Authority", "PS"),
    ([445], "North Korea", "KP"),
    ([447], "Kuwait", "KW"),
    ([450], "Lebanon", "LB"),
    ([451], "Laos", "LA"),
    ([453], "Macao", "MO"),
    ([455], "Maldives", "MV"),
    ([457], "Mongolia", "MN"),
    ([459], "Nepal", "NP"),
    ([461], "Oman", "OM"),
    ([463], "Pakistan", "PK"),
    ([466], "Qatar", "QA"),
    ([468], "Syria", "SY"),
    ([470, 471], "United Arab Emirates", "AE"),
    ([472], "Tajikistan", "TJ"),
    ([473, 475], "Yemen", "YE"),
    ([477], "Bosnia and Herzegovina", "BA"),
    ([478], "Hong Kong", "HK"),
    # Pacific & Southeast Asia
    ([501], "Adelie Land", "TF"),
    ([503], "Australia", "AU"),
    ([506], "Myanmar", "MM"),
    ([508], "Brunei", "BN"),
    ([510], "Micronesia", "FM"),
    ([511], "Palau", "PW"),
    ([512], "New Zealand", "NZ"),
    ([514, 515], "Cambodia", "KH"),
    ([516], "Christmas Island", "CX"),
    ([518], "Cook Islands", "CK"),
    ([520], "Fiji", "FJ"),
    ([523], "Cocos (Keeling) Islands", "CC"),
    ([525], "Indonesia", "ID"),
    ([529], "Kiribati", "KI"),
    ([531], "Laos", "LA"),
    ([533], "Malaysia", "MY"),
    ([536], "Northern Mariana Islands", "MP"),
    ([538], "Marshall Islands", "MH"),
    ([540], "New Caledonia", "NC"),
    ([542], "Niue", "NU"),
    ([544], "Nauru", "NR"),
    ([546], "French Polynesia", "PF"),
    ([548], "Philippines", "PH"),
    ([550], "Timor-Leste", "TL"),
    ([553], "Papua New Guinea", "PG"),
    ([555], "Pitcairn Islands", "PN"),
    ([557], "Solomon Islands", "SB"),
    ([559], "American Samoa", "AS"),
    ([561], "Samoa", "WS"),
    ([563, 564, 565, 566], "Singapore", "SG"),
    ([567], "Thailand", "TH"),
    ([570], "Tonga", "TO"),
    ([572], "Tuvalu", "TV"),
    ([574], "Vietnam", "VN"),
    ([576, 577], "Vanuatu", "VU"),
    ([578], "Wallis and Futuna", "WF"),
    # Africa, Atlantic, Indian Ocean
    ([601], "South Africa", "ZA"),
    ([603], "Angola", "AO"),
    ([605], "Algeria", "DZ"),
    ([607], "Saint Paul and Amsterdam Islands", "TF"),
    ([608], "Ascension Island", "AC"),
    ([609], "Burundi", "BI"),
    ([610], "Benin", "BJ"),
    ([611], "Botswana", "BW"),
    ([612], "Central African Republic", "CF"),
    ([613], "Cameroon", "CM"),
    ([615], "Congo", "CG"),
    ([616, 620], "Comoros", "KM"),
    ([617], "Cape Verde", "CV"),
    ([618], "Crozet Archipelago", "TF"),
    ([619], "Cote d'Ivoire", "CI"),
    ([621], "Djibouti", "DJ"),
    ([622], "Egypt", "EG"),
    ([624], "Ethiopia", "ET"),
    ([625], "Eritrea", "ER"),
    ([626], "Gabon", "GA"),
    ([627], "Ghana", "GH"),
    ([629], "Gambia", "GM"),
    ([630], "Guinea-Bissau", "GW"),
    ([631], "Equatorial Guinea", "GQ"),
    ([632], "Guinea", "GN"),
    ([633], "Burkina Faso", "BF"),
    ([634], "Kenya", "KE"),
    ([635], "Kerguelen Islands", "TF"),
    ([636, 637], "Liberia", "LR"),
    ([638], "South Sudan", "SS"),
    ([642], "Libya", "LY"),
    ([644], "Lesotho", "LS"),
    ([645], "Mauritius", "MU"),
    ([647], "Madagascar", "MG"),
    ([649], "Mali", "ML"),
    ([650], "Mozambique", "MZ"),
    ([654], "Mauritania", "MR"),
    ([655], "Malawi", "MW"),
    ([656], "Niger", "NE"),
    ([657], "Nigeria", "NG"),
    ([659], "Namibia", "NA"),
    ([660], "Reunion", "RE"),
    ([661], "Rwanda", "RW"),
    ([662], "Sudan", "SD"),
    ([663], "Senegal", "SN"),
    ([664], "Seychelles", "SC"),
    ([665], "Saint Helena", "SH"),
    ([666], "Somalia", "SO"),
    ([667], "Sierra Leone", "SL"),
    ([668], "Sao Tome and Principe", "ST"),
    ([669], "Swaziland", "SZ"),
    ([670], "Chad", "TD"),
    ([671], "Togo", "TG"),
    ([672], "Tunisia", "TN"),
    ([674, 677], "Tanzania", "TZ"),
    ([675], "Uganda", "UG"),
    ([676], "Democratic Republic of the Congo", "CD"),
    ([678], "Zambia", "ZM"),
    ([679], "Zimbabwe", "ZW"),
    # South America
    ([701], "Argentina", "AR"),
    ([710], "Brazil", "BR"),
    ([720], "Bolivia", "BO"),
    ([725], "Chile", "CL"),
    ([730], "Colombia", "CO"),
    ([735], "Ecuador", "EC"),
    ([740], "Falkland Islands", "FK"),
    ([745], "French Guiana", "GF"),
    ([750], "Guyana", "GY"),
    ([755], "Paraguay", "PY"),
    ([760], "Peru", "PE"),
    ([765], "Suriname", "SR"),
    ([770], "Uruguay", "UY"),
    ([775], "Venezuela", "VE"),
]

_MID_TABLE = _expand_mids(_MID_PAIRS)


def mid_lookup(mid: int):
    """Return (country_name, iso_alpha_2) or (None, None)."""
    return _MID_TABLE.get(mid, (None, None))


# MMSI category from prefix (ITU-R M.585)
#   00MIDxxxx  — coast station
#   0MIDxxxxx  — group of ships
#   111MIDxxx  — SAR aircraft
#   8MIDxxxxx  — handheld VHF / portable
#   970xxxxxx  — AIS-SART (search-and-rescue transmitter)
#   972xxxxxx  — MOB (man overboard device)
#   974xxxxxx  — AIS-EPIRB
#   98MIDxxxx  — auxiliary craft (associated with parent ship)
#   99MIDxxxx  — AtoN (Aid to Navigation)
#   MIDxxxxxx  — ship (most common, MID = first 3 digits)


def classify_mmsi(mmsi):
    """Return dict {category, mid, valid, distress} describing an MMSI.

    `category` is one of: ship, group, coast_station, sar_aircraft, handheld,
    sart, mob, epirb, auxiliary_craft, aton, invalid.
    `mid` is the 3-digit MID (or None if not derivable).
    `valid` is True iff the MMSI is a 9-digit positive integer.
    `distress` is True for SART / MOB / EPIRB categories.
    """
    if mmsi is None:
        return {"category": "invalid", "mid": None, "valid": False,
                "distress": False}
    try:
        n = int(mmsi)
    except (TypeError, ValueError):
        return {"category": "invalid", "mid": None, "valid": False,
                "distress": False}
    if n < 1 or n > 999_999_999:
        return {"category": "invalid", "mid": None, "valid": False,
                "distress": False}

    s = f"{n:09d}"

    # 970 / 972 / 974 distress beacons (full 3-digit prefix, no MID embedded)
    if s.startswith("970"):
        return {"category": "sart", "mid": None, "valid": True, "distress": True}
    if s.startswith("972"):
        return {"category": "mob", "mid": None, "valid": True, "distress": True}
    if s.startswith("974"):
        return {"category": "epirb", "mid": None, "valid": True, "distress": True}

    # 111MIDxxx — SAR aircraft (MID in positions 4-6)
    if s.startswith("111"):
        return {"category": "sar_aircraft", "mid": int(s[3:6]),
                "valid": True, "distress": False}

    # 99MIDxxxx — AtoN (MID in positions 3-5)
    if s.startswith("99"):
        return {"category": "aton", "mid": int(s[2:5]),
                "valid": True, "distress": False}

    # 98MIDxxxx — auxiliary craft (MID in positions 3-5)
    if s.startswith("98"):
        return {"category": "auxiliary_craft", "mid": int(s[2:5]),
                "valid": True, "distress": False}

    # 00MIDxxxx — coast station (MID in positions 3-5)
    if s.startswith("00"):
        return {"category": "coast_station", "mid": int(s[2:5]),
                "valid": True, "distress": False}

    # 0MIDxxxxx — group of ships (MID in positions 2-4)
    if s.startswith("0"):
        return {"category": "group", "mid": int(s[1:4]),
                "valid": True, "distress": False}

    # 8MIDxxxxx — handheld VHF (MID in positions 2-4)
    if s.startswith("8"):
        return {"category": "handheld", "mid": int(s[1:4]),
                "valid": True, "distress": False}

    # Ship — MID in positions 1-3
    return {"category": "ship", "mid": int(s[0:3]),
            "valid": True, "distress": False}


# ---------------------------------------------------------------------------
# Suspicious-MMSI-pattern detection
# ---------------------------------------------------------------------------

# Known placeholder / factory-default MMSIs that AIS field surveys see
# regularly. These are technically valid 9-digit numbers but no real-world
# vessel registry would issue them.  All-same-digit patterns dominate; a few
# explicit ones cover sequential-digit defaults and the literal "1234567890"
# test patterns transponder installers leave behind.
_KNOWN_PLACEHOLDER_MMSIS = frozenset({
    111111111, 222222222, 333333333, 444444444,
    555555555, 666666666, 777777777, 888888888, 999999999,
    123456789, 987654321, 100000000, 123456780,
})


def mmsi_pattern_suspicious(mmsi):
    """True if *mmsi* matches an obvious placeholder/test pattern.

    Catches all-same-digit MMSIs (e.g. 555555555), simple ascending /
    descending sequences (123456789), and degenerate two-digit alternating
    patterns (e.g. 121212121, 989898989).  These are field-survey markers
    of misconfigured transponders, NOT real vessel identities.
    """
    if mmsi is None:
        return False
    try:
        n = int(mmsi)
    except (TypeError, ValueError):
        return False
    if n in _KNOWN_PLACEHOLDER_MMSIS:
        return True
    s = f"{n:09d}"
    distinct = set(s)
    if len(distinct) == 1:
        return True
    # Two-digit alternating pattern (e.g. 121212121).
    if len(distinct) == 2 and len(set(s[::2])) == 1 and len(set(s[1::2])) == 1:
        return True
    return False


# ---------------------------------------------------------------------------
# Rate of turn decoding
# ---------------------------------------------------------------------------

def decode_rate_of_turn(raw):
    """Decode AIS ROT field to deg/min.

    Raw is `sign(rot) * 4.733 * sqrt(|rot_deg_per_min|)`.  Sentinel -128 =
    not available.  ±127 = >5 deg/30s.  Returns float deg/min or None.
    """
    if raw is None or raw == ROT_NOT_AVAILABLE:
        return None
    sign = 1 if raw >= 0 else -1
    return sign * (raw / 4.733) ** 2


# ---------------------------------------------------------------------------
# AIS timestamp quality (the per-message Timestamp field, 0-63 seconds)
# ---------------------------------------------------------------------------

def decode_timestamp_quality(raw):
    if raw is None:
        return None
    if raw == TIMESTAMP_NOT_AVAILABLE:
        return "unavailable"
    if raw == TIMESTAMP_MANUAL:
        return "manual"
    if raw == TIMESTAMP_DEAD_RECKONING:
        return "dead_reckoning"
    if raw == TIMESTAMP_INOPERATIVE:
        return "inoperative"
    if 0 <= raw <= 59:
        return "gnss"
    return None


# ---------------------------------------------------------------------------
# Position validity
# ---------------------------------------------------------------------------

def position_valid(lat, lon):
    """True iff (lat, lon) is a real geographic point (no AIS sentinels)."""
    if lat is None or lon is None:
        return False
    if lat == LAT_NOT_AVAILABLE or lon == LON_NOT_AVAILABLE:
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return False
    return True


def position_is_null_island(lat, lon):
    return lat == 0.0 and lon == 0.0


# ---------------------------------------------------------------------------
# Distress detection (single-message, stateless)
# ---------------------------------------------------------------------------

def detect_distress(message_type, mmsi_info, body):
    """Return dict {is_distress, source, severity, keywords_matched, text}.

    `mmsi_info` is the output of classify_mmsi(); `body` is the inner
    MessageBody dict (e.g. PositionReport content).  Severity follows
    syslog levels: 2=critical, 3=high, 4=medium, 5=low (None when not distress).
    """
    out = {"is_distress": False, "source": None, "severity": None,
           "keywords_matched": [], "text": None}

    # MMSI-class distress beacons (SART / MOB / EPIRB)
    if mmsi_info["distress"]:
        out["is_distress"] = True
        out["source"] = f"{mmsi_info['category']}_mmsi"
        out["severity"] = 2
        return out

    # Position report with NavigationalStatus == 14 (ais_sart_active)
    if body and body.get("NavigationalStatus") == 14:
        out["is_distress"] = True
        out["source"] = "navstatus_sart"
        out["severity"] = 2
        return out

    # Safety message text scan
    if message_type in ("SafetyBroadcastMessage", "AddressedSafetyMessage"):
        text = (body or {}).get("Text") or ""
        out["text"] = text.strip()
        if text:
            upper = text.upper()
            crit = [k for k in _DISTRESS_KEYWORDS_CRITICAL if k in upper]
            high = [k for k in _DISTRESS_KEYWORDS_HIGH if k in upper]
            med = [k for k in _DISTRESS_KEYWORDS_MEDIUM if k in upper]
            if crit:
                out["is_distress"] = True
                out["source"] = "safety_text"
                out["severity"] = 2
                out["keywords_matched"] = crit
            elif high:
                out["is_distress"] = True
                out["source"] = "safety_text"
                out["severity"] = 3
                out["keywords_matched"] = high
            elif med:
                out["severity"] = 4
                out["keywords_matched"] = med
            else:
                out["severity"] = 5
    return out


# ---------------------------------------------------------------------------
# IMO check digit (ISO 6346-like): 7-digit number, last digit is checksum
# ---------------------------------------------------------------------------

def imo_checksum_valid(imo):
    """True iff *imo* is a 7-digit number whose check digit verifies."""
    if imo is None:
        return False
    try:
        n = int(imo)
    except (TypeError, ValueError):
        return False
    if not (1_000_000 <= n <= 9_999_999):
        return False
    digits = [int(c) for c in str(n)]
    total = sum(d * w for d, w in zip(digits[:6], (7, 6, 5, 4, 3, 2)))
    return total % 10 == digits[6]


# ---------------------------------------------------------------------------
# Time parsing — aisstream.io emits Go time.String() format
# ---------------------------------------------------------------------------

# e.g. "2026-05-15 20:39:29.245834886 +0000 UTC"
_GO_TIME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?) ([+\-]\d{4}) UTC$")


def parse_go_time(s):
    """Parse Go's time.String() into a Python datetime (UTC, aware).

    Returns None on parse failure.  Accepts ISO-8601 as a fallback.
    """
    import datetime
    if not s:
        return None
    m = _GO_TIME_RE.match(s)
    if m:
        ts = m.group(1)  # tz suffix from group(2) ignored — always UTC
        # nanoseconds truncated to microseconds
        if "." in ts:
            head, frac = ts.split(".")
            frac = (frac + "000000")[:6]
            ts = f"{head}.{frac}"
        try:
            dt = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            try:
                dt = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        return dt.replace(tzinfo=datetime.timezone.utc)
    # Fallback: try ISO-8601
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# AIS name field cleanup — strips trailing AIS padding ('@' and spaces).
# ---------------------------------------------------------------------------

def clean_ais_string(s):
    if s is None:
        return None
    return s.replace("@", "").strip() or None
