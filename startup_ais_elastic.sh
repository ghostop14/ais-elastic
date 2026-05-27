#!/bin/bash
# Boot-time wrapper: wait for network/ES to come up, then start the bridge.

sleep 60s
cd /opt/sdr/ais/ais-elastic
exec ./run_ais_elastic.sh
