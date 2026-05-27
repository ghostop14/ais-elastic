#!/bin/bash
# Foreground run script — required env: AISSTREAM_API_KEY, ES_URL, AIS_BBOX,
# OBSERVER_COORD.  Set OBSERVER_NAME to override hostname.

set -euo pipefail
: "${AISSTREAM_API_KEY:?must be set}"
: "${ES_URL:?must be set (e.g. http://user:pass@host:9200)}"
: "${AIS_BBOX:?must be set (NW_LAT,NW_LON,SE_LAT,SE_LON)}"
: "${OBSERVER_COORD:?must be set (LAT,LON[,ALT_M])}"

cd /opt/sdr/ais/ais-elastic

exec python3 ./ais_elastic.py \
    --backend-type "${BACKEND_TYPE:-elasticsearch}" \
    --server-url "$ES_URL" \
    --bbox "$AIS_BBOX" \
    --observer-coord "$OBSERVER_COORD" \
    --observer-name "${OBSERVER_NAME:-$(hostname)}"
