"""Pure scheduling logic (no Google, no database) so it is easy to test."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def generate_slots(first_day: date, last_day: date, *, tz: ZoneInfo, work_start: time, work_end: time,
                   work_days: set, step_min: int, duration_min: int, earliest_utc: datetime) -> list:
    """Every bookable slot start (UTC) between two local dates, inside working hours."""
    out, d = [], first_day
    while d <= last_day:
        if d.weekday() in work_days:
            t = datetime.combine(d, work_start, tzinfo=tz)
            day_end = datetime.combine(d, work_end, tzinfo=tz)
            while t + timedelta(minutes=duration_min) <= day_end:
                u = t.astimezone(UTC)
                if u >= earliest_utc:
                    out.append(u)
                t += timedelta(minutes=step_min)
        d += timedelta(days=1)
    return out


def overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def is_free(slot_start: datetime, duration_min: int, buffer_min: int, busy: list) -> bool:
    """Free means no busy block touches the demo or the buffer around it."""
    s = slot_start - timedelta(minutes=buffer_min)
    e = slot_start + timedelta(minutes=duration_min + buffer_min)
    return not any(overlaps(s, e, bs, be) for bs, be in busy)


def rank_presenters(free: list, week_load: dict, day_load: dict, last_assigned: dict, max_per_day: int) -> list:
    """Best presenter first: fewest demos this week, then whoever was assigned longest ago.
    Anyone at their daily cap is dropped."""
    floor = datetime.min.replace(tzinfo=UTC)
    ok = [p for p in free if day_load.get(p, 0) < max_per_day]
    return sorted(ok, key=lambda p: (week_load.get(p, 0), last_assigned.get(p) or floor, p))


@dataclass(frozen=True)
class Eligible:
    start: datetime
    load: int          # week load of the presenter who would get this slot
    local_date: date   # in the prospect's timezone


def choose_offers(eligible: list, n: int = 3, min_gap_min: int = 120) -> list:
    """Pick n slots spread over different days, preferring slots whose presenter is least loaded."""
    by_day = defaultdict(list)
    for e in eligible:
        by_day[e.local_date].append(e)
    offers = []
    for d in sorted(by_day):
        offers.append(min(by_day[d], key=lambda e: (e.load, e.start)))
        if len(offers) == n:
            return sorted(offers, key=lambda e: e.start)
    for d in sorted(by_day):  # fewer days than n: add more times, at least min_gap apart
        for e in sorted(by_day[d], key=lambda e: (e.load, e.start)):
            if len(offers) == n:
                break
            if all(abs((e.start - o.start).total_seconds()) >= min_gap_min * 60 for o in offers):
                offers.append(e)
    return sorted(offers, key=lambda e: e.start)
