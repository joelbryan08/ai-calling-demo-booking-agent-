"""Calendar + Sheets backends. `fake` runs in memory (safe for testing); `google` uses a service account
with domain-wide delegation for the presenters' calendars."""
import csv
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

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


def _is_delegation_error(e: Exception) -> bool:
    """True when Google refused because domain-wide delegation isn't set up (so shared-calendar mode is the way)."""
    t = f"{type(e).__name__} {e}".lower()
    return any(k in t for k in ("unauthorized_client", "refresherror", "invalid_grant", "forbiddenforserviceaccounts",
                                "not authorized to access this resource"))


def service_account_email() -> str:
    try:
        info = json.loads(settings.google_sa_json) if settings.google_sa_json else json.load(open(settings.google_sa_file))
        return info.get("client_email", "")
    except Exception:
        return ""


def meeting_link_for(email: str) -> str:
    return dict((k.strip().lower(), v.strip()) for k, v in settings.meeting_links).get(email.lower()) or settings.default_meeting_link


class GoogleCalendar:
    """Two ways to reach a presenter's calendar, chosen per presenter (GOOGLE_MODE=auto):
    delegated: the app acts as the presenter (needs a Workspace admin once). Meet links and invites work.
    shared:    the presenter shared their calendar with the service account (no admin). Free/busy and events work;
               Google doesn't allow invites or auto Meet links here, so a fixed meeting link is used instead."""

    def __init__(self):
        self._modes = {}      # email -> (mode, checked_at)

    def _service(self, email, delegated):
        from googleapiclient.discovery import build
        return build("calendar", "v3", credentials=_creds(CAL_SCOPES, email if delegated else None), cache_discovery=False)

    def mode(self, email) -> str:
        if settings.google_mode in ("delegated", "shared"):
            return settings.google_mode
        hit = self._modes.get(email)
        if hit and time.time() - hit[1] < (600 if hit[0] == "delegated" else 300):   # re-check shared often, so a later admin approval is noticed
            return hit[0]
        now = datetime.now(timezone.utc)
        try:
            self._service(email, True).freebusy().query(body={"timeMin": now.isoformat(), "timeMax": (now + timedelta(minutes=1)).isoformat(),
                                                              "items": [{"id": "primary"}]}).execute()
            mode = "delegated"
        except Exception as e:
            if not _is_delegation_error(e):
                raise
            mode = "shared"
        self._modes[email] = (mode, time.time())
        return mode

    def busy(self, email, start, end):
        mode = self.mode(email)
        cal_id = "primary" if mode == "delegated" else email
        body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": cal_id}]}
        cal = self._service(email, mode == "delegated").freebusy().query(body=body).execute()["calendars"][cal_id]
        if cal.get("errors"):
            raise RuntimeError(f"cannot read {email}'s calendar ({mode} mode): {cal['errors']}")
        return [(_parse(b["start"]), _parse(b["end"])) for b in cal.get("busy", [])]

    def create_event(self, email, *, summary, description, start, end, tz, attendee):
        delegated = self.mode(email) == "delegated"
        body = {"summary": summary, "description": description, "start": {"dateTime": start.isoformat(), "timeZone": tz},
                "end": {"dateTime": end.isoformat(), "timeZone": tz}, "extendedProperties": {"private": {"source": "demo-agent"}}}
        link = None
        if delegated:
            if attendee:
                body["attendees"] = [{"email": attendee}]
            body["conferenceData"] = {"createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        else:
            link = meeting_link_for(email)
            extra = ([f"Join: {link}"] if link else []) + ([f"Prospect email: {attendee}"] if attendee else [])
            body["description"] = "\n".join([description] + extra)
            if link:
                body["location"] = link
        ev = self._service(email, delegated).events().insert(
            calendarId="primary" if delegated else email, body=body, conferenceDataVersion=1 if delegated else 0,
            sendUpdates="all" if delegated else "none").execute()
        meet = (ev.get("hangoutLink") or next((p["uri"] for p in ev.get("conferenceData", {}).get("entryPoints", [])
                                               if p.get("entryPointType") == "video"), None)) if delegated else link
        return {"event_id": ev["id"], "meet_link": meet, "html_link": ev.get("htmlLink")}

    def delete_event(self, email, event_id):
        delegated = self.mode(email) == "delegated"
        self._service(email, delegated).events().delete(calendarId="primary" if delegated else email, eventId=event_id,
                                                        sendUpdates="all" if delegated else "none").execute()

    def diagnose(self, email) -> dict:
        now = datetime.now(timezone.utc)
        try:
            mode = self.mode(email)
            self.busy(email, now, now + timedelta(hours=1))
            return {"email": email, "mode": mode, "ok": True, "detail": "", "link": bool(meeting_link_for(email))}
        except Exception as e:
            return {"email": email, "mode": "?", "ok": False, "detail": f"{type(e).__name__}: {str(e)[:180]}", "link": bool(meeting_link_for(email))}


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


def check_sheet() -> str:
    """'' if the sheet is reachable, otherwise a plain-English problem."""
    from googleapiclient.discovery import build
    try:
        build("sheets", "v4", credentials=_creds(SHEETS_SCOPES), cache_discovery=False).spreadsheets().get(
            spreadsheetId=settings.sheet_id, fields="properties.title").execute()
        return ""
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:160]}"


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
