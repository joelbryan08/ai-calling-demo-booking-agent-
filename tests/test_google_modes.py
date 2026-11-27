import dataclasses
from datetime import datetime, timezone

import pytest

import app.google_clients as G
from app.config import settings

START = datetime(2026, 11, 24, 4, 30, tzinfo=timezone.utc)
END = datetime(2026, 11, 24, 5, 0, tzinfo=timezone.utc)


class RefreshError(Exception):       # same class name Google's library uses when delegation is not authorized
    pass


class Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeGoogle(G.GoogleCalendar):
    def __init__(self, dwd=True, shared=True):
        super().__init__()
        self.dwd, self.shared, self.calls = dwd, shared, []

    def _service(self, email, delegated):
        outer = self

        class S:
            def freebusy(s): return s
            def events(s): return s

            def query(s, body):
                def run():
                    if delegated and not outer.dwd:
                        raise RefreshError("unauthorized_client: Client is unauthorized to retrieve access tokens")
                    cid = body["items"][0]["id"]
                    if not delegated and not outer.shared:
                        return {"calendars": {cid: {"errors": [{"reason": "notFound"}]}}}
                    return {"calendars": {cid: {"busy": [{"start": "2026-11-24T04:30:00Z", "end": "2026-11-24T05:00:00Z"}]}}}
                return Exec(run)

            def insert(s, **kw):
                outer.calls.append(("insert", delegated, kw))
                return Exec(lambda: {"id": "e1", "htmlLink": "h", "hangoutLink": "https://meet.google.com/auto"})

            def delete(s, **kw):
                outer.calls.append(("delete", delegated, kw))
                return Exec(lambda: None)
        return S()


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(G, "settings", dataclasses.replace(
        settings, google_mode="auto", meeting_links=(("a@co.com", "https://meet.google.com/aaa-bbbb-ccc"),),
        default_meeting_link="https://zoom.us/j/999"))


def make(cal, email, attendee=None):
    return cal.create_event(email, summary="Demo", description="Booked by agent", start=START, end=END, tz="Asia/Kolkata", attendee=attendee)


def test_falls_back_to_shared_calendars_when_delegation_is_not_set_up():
    cal = FakeGoogle(dwd=False)
    assert cal.mode("a@co.com") == "shared"
    assert cal.busy("a@co.com", START, END) == [(START, END)]
    assert make(cal, "a@co.com", attendee="x@y.com")["meet_link"] == "https://meet.google.com/aaa-bbbb-ccc"
    _, delegated, kw = cal.calls[-1]
    assert not delegated and kw["calendarId"] == "a@co.com" and kw["sendUpdates"] == "none" and kw["conferenceDataVersion"] == 0
    assert "conferenceData" not in kw["body"] and "attendees" not in kw["body"]
    assert kw["body"]["location"] == "https://meet.google.com/aaa-bbbb-ccc" and "Prospect email: x@y.com" in kw["body"]["description"]
    assert make(cal, "b@co.com")["meet_link"] == "https://zoom.us/j/999"      # no personal link: the default one


def test_uses_delegation_when_it_is_available():
    cal = FakeGoogle(dwd=True)
    assert cal.mode("a@co.com") == "delegated"
    assert make(cal, "a@co.com", attendee="x@y.com")["meet_link"] == "https://meet.google.com/auto"
    _, delegated, kw = cal.calls[-1]
    assert delegated and kw["calendarId"] == "primary" and kw["sendUpdates"] == "all"
    assert kw["body"]["attendees"] == [{"email": "x@y.com"}] and "conferenceData" in kw["body"]


def test_notices_when_an_admin_enables_delegation_later(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(G.time, "time", lambda: now["t"])
    cal = FakeGoogle(dwd=False)
    assert cal.mode("a@co.com") == "shared"
    cal.dwd, now["t"] = True, 1100.0
    assert cal.mode("a@co.com") == "shared"          # still inside the 5 minute cache
    now["t"] = 1400.0
    assert cal.mode("a@co.com") == "delegated"


def test_unshared_calendar_fails_closed_and_is_diagnosed():
    cal = FakeGoogle(dwd=False, shared=False)
    with pytest.raises(RuntimeError, match="cannot read a@co.com"):
        cal.busy("a@co.com", START, END)
    d = cal.diagnose("a@co.com")
    assert d["ok"] is False and "cannot read" in d["detail"]
    assert FakeGoogle(dwd=False).diagnose("a@co.com")["ok"] is True


def test_forced_mode_and_error_detection(monkeypatch):
    monkeypatch.setattr(G, "settings", dataclasses.replace(G.settings, google_mode="shared"))
    assert FakeGoogle(dwd=True).mode("a@co.com") == "shared"
    assert G._is_delegation_error(RefreshError("x")) and G._is_delegation_error(Exception("invalid_grant: Not a valid email"))
    assert not G._is_delegation_error(ConnectionError("network down"))
