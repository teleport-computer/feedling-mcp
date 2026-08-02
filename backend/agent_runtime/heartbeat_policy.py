"""Shared supervisor-heartbeat freshness and retention policy."""

# Six beats at the normal 15-second cadence. The health/send readers and the
# writer-side stale-row cleanup share this value so a row cannot remain counted
# long after it has become officially unhealthy. Cleanup runs every beat, giving
# a worst-case removal bound of about 105 seconds, inside the 300-second deploy
# suppression grace.
SUPERVISOR_HEARTBEAT_MAX_AGE_SEC = 90.0
