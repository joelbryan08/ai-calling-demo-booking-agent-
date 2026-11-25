"""Calendar + Sheets backends. `fake` runs in memory (safe for testing); `google` uses a service account
with domain-wide delegation for the presenters' calendars."""
import csv
import json
import logging
import os
import uuid
from datetime import datetime

from .config import settings

log = logging.getLogger("google")
# Calendar scopes are the ONLY ones the admin authorizes for delegation (acting as the presenters).
# Sheets uses the service account's own identity, so it needs no delegation.
CAL_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
]
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ----------------------------------------------------------------------------- fakes
class FakeCalendar:
    def __init__(self):
        self.busy_map, self.events, self._n = {}, {}, 0

    def busy(self, email, start, end):
        return [(s, e) for s, e in self.busy_map.get(email, []) if s < end and start < e]

    def add_busy(self, email, start, end):
        self.busy_map.setdefault(email, []).append((start, end))

    def create_event(self, email, *, summary, description, start, end, tz, attendee):
        self._n += 1
        eid = f"fake-event-{self._n}"
        self.add_busy(email, start, end)
        self.events[eid] = {"email": email, "summary": summary, "start": start, "end": end, "attendee": attendee}
        return {"event_id": eid, "meet_link": f"https://meet.google.com/fake-{self._n:04d}",
                "html_link": f"https://calendar.example/{eid}"}

    def delete_event(self, email, event_id):
        ev = self.events.pop(event_id, None)
        if ev:
            self.busy_map[email] = [(s, e) for s, e in self.busy_map.get(email, [])
                                    if not (s == ev["start"] and e == ev["end"])]


class FakeSheets:
    def __init__(self):
        self.rows, self.fail = [], False

    def append_row(self, row):
        if self.fail:
            raise RuntimeError("sheets unavailable")
        self.rows.append(row)
        path = os.getenv("FAKE_SHEET_CSV", "fake_sheet.csv")
        if path:  # lets you open the "sheet" in Excel while testing
            with open(path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)


# ----------------------------------------------------------------------------- google
def _creds(scopes, subject=None):
    from google.oauth2 import service_account
    if settings.google_sa_json:
        c = service_account.Credentials.from_service_account_info(json.loads(settings.google_sa_json), scopes=scopes)
    elif settings.google_sa_file:
        c = service_account.Credentials.from_service_account_file(settings.google_sa_file, scopes=scopes)
    else:
        raise RuntimeError("Set GOOGLE_SERVICE_ACCOUNT_FILE (or GOOGLE_SERVICE_ACCOUNT_JSON)")
    return c.with_subject(subject) if subject else c


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class GoogleCalendar:
    def _svc(self, email):
        from googleapiclient.discovery import build
        return build("calendar", "v3", credentials=_creds(CAL_SCOPES, email), cache_discovery=False)

    def busy(self, email, start, end):
        body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": "primary"}]}
        cal = self._svc(email).freebusy().query(body=body).execute()["calendars"]["primary"]
        if cal.get("errors"):
            raise RuntimeError(f"freebusy error for {email}: {cal['errors']}")
        return [(_parse(b["start"]), _parse(b["end"])) for b in cal.get("busy", [])]

    def create_event(self, email, *, summary, description, start, end, tz, attendee):
        body = {
            "summary": summary, "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": tz},
            "end": {"dateTime": end.isoformat(), "timeZone": tz},
            **({"attendees": [{"email": attendee}]} if attendee else {}),
            "conferenceData": {"createRequest": {"requestId": uuid.uuid4().hex,
                                                 "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
            "extendedProperties": {"private": {"source": "demo-agent"}},
        }
        ev = self._svc(email).events().insert(calendarId="primary", body=body, conferenceDataVersion=1,
                                              sendUpdates="all").execute()
        meet = ev.get("hangoutLink") or next(
            (p["uri"] for p in ev.get("conferenceData", {}).get("entryPoints", []) if p.get("entryPointType") == "video"), None)
        return {"event_id": ev["id"], "meet_link": meet, "html_link": ev.get("htmlLink")}

    def delete_event(self, email, event_id):
        self._svc(email).events().delete(calendarId="primary", eventId=event_id, sendUpdates="all").execute()


class GoogleSheets:
    def append_row(self, row):
        from googleapiclient.discovery import build
        if not settings.sheet_id:
            raise RuntimeError("Set SHEET_ID")
        svc = build("sheets", "v4", credentials=_creds(SHEETS_SCOPES), cache_discovery=False)
        # RAW: text stays text, so a value like "=IMPORTXML(...)" said on a call can't become a formula
        svc.spreadsheets().values().append(
            spreadsheetId=settings.sheet_id, range=f"{settings.sheet_tab}!A1", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": [row]}).execute()


# ----------------------------------------------------------------------------- access
_cal = _sheets = None


def get_calendar():
    global _cal
    if _cal is None:
        _cal = GoogleCalendar() if settings.calendar_backend == "google" else FakeCalendar()
    return _cal


def get_sheets():
    global _sheets
    if _sheets is None:
        _sheets = GoogleSheets() if settings.calendar_backend == "google" else FakeSheets()
    return _sheets


def reset_fakes():
    global _cal, _sheets
    _cal = _sheets = None
