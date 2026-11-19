#!/usr/bin/env bash
# Demo-only overlay: turns on the simulated ops feed and its poller for the
# current cluster. The checked-in manifests keep both off.
set -euo pipefail
kubectl set env deployment/flight-api \
  OPS_FEED_SIMULATION=true OPS_FEED_POLL_ENABLED=true OPS_FEED_POLL_INTERVAL_SECONDS=1
