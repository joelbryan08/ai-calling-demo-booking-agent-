from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def as_utc(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; we always store UTC, so treat naive as UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def window_status(now_utc: datetime, tz_name: str, start_hhmm: str, end_hhmm: str):
    """Is `now` inside the lead-local calling window? Returns (ok, next_start_utc or None)."""
    local = as_utc(now_utc).astimezone(ZoneInfo(tz_name))
    s, e = time.fromisoformat(start_hhmm), time.fromisoformat(end_hhmm)
    start_dt = local.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
    end_dt = local.replace(hour=e.hour, minute=e.minute, second=0, microsecond=0)
    if start_dt <= local < end_dt:
        return True, None
    nxt = start_dt if local < start_dt else start_dt + timedelta(days=1)
    return False, nxt.astimezone(timezone.utc)
