from datetime import datetime, timezone


def utc_timestamp():
    #ISO 8601 UTC with milliseconds, e.g. 2026-09-28T04:38:15.123Z.
    #Every stored ts uses this format, so string order == time order.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
