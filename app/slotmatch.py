"""Turn what a caller accepted ("Tuesday at 10 AM", "the second one") into one of the slots we offered."""
import re
from datetime import datetime

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ORDINALS = {"first": 0, "1st": 0, "1": 0, "one": 0, "second": 1, "2nd": 1, "2": 1, "two": 1,
            "third": 2, "3rd": 2, "3": 2, "three": 2}
TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b")


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9: ]+", " ", s.lower()).split())


def match_offer(text, offers: list):
    """Return the single matching offer ({"slot_start", "spoken"}) or None if unclear or ambiguous."""
    t = (text or "").strip()
    if not t or t.lower() in {"none", "null", "n/a", "na", "no", "unknown"} or not offers:
        return None
    try:  # 1. exact timestamp
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if dt.tzinfo:
            hits = [o for o in offers if datetime.fromisoformat(o["slot_start"]) == dt]
            if len(hits) == 1:
                return hits[0]
    except ValueError:
        pass
    nt = _norm(t)  # 2. the sentence we offered, or part of it
    hits = [o for o in offers if _norm(o["spoken"]) in nt or (len(nt) > 8 and nt in _norm(o["spoken"]))]
    if len(hits) == 1:
        return hits[0]
    # 3. weekday and/or time of day
    day = next((d for d in WEEKDAYS if d in nt), None)
    m = TIME_RE.search(nt)
    hour = minute = None
    if m:
        hour, minute = int(m.group(1)) % 12 + (12 if m.group(3) == "p" else 0), int(m.group(2) or 0)
    if day or m:
        cands = []
        for o in offers:
            local = datetime.fromisoformat(o["slot_start"])
            if day and local.strftime("%A").lower() != day:
                continue
            if m and (local.hour, local.minute) != (hour, minute):
                continue
            cands.append(o)
        if len(cands) == 1:
            return cands[0]
    # 4. "the second one", "option 3", "2"
    om = re.match(r"^(?:option |number |the )?(first|second|third|1st|2nd|3rd|one|two|three|1|2|3)\b", nt)
    if om and ORDINALS[om.group(1)] < len(offers):
        return offers[ORDINALS[om.group(1)]]
    return None
