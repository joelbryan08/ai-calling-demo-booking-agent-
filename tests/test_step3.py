from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.tools as tools
from app import scheduler as sch
from app.db import SessionLocal
from app.google_clients import get_calendar, get_sheets
from app.main import app
from app.models import Booking, DoNotCall

client = TestClient(app)
H = {"Authorization": "Bearer test-key"}
NOW = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)  # Monday 11:30 IST
TUE_10 = "2026-11-24T10:00:00+05:30"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(tools, "_now", lambda: NOW)


def avail(**kw):
    body = {"start_date": "2026-11-23", "end_date": "2026-11-27", "timezone": "Asia/Kolkata", **kw}
    r = client.post("/tools/check_availability", json=body, headers=H)
    assert r.status_code == 200
    return r.json()


def book(slot=TUE_10, email="x@y.com", **kw):
    body = {"slot_start": slot, "timezone": "Asia/Kolkata", "prospect_name": "Asha", "prospect_email": email, **kw}
    if email is None:
        del body["prospect_email"]
    return client.post("/tools/book_demo", json=body, headers=H).json()


def bookings():
    with SessionLocal() as db:
        return list(db.scalars(select(Booking).order_by(Booking.id)))


def test_auth_required():
    body = {"start_date": "2026-11-23"}
    assert client.post("/tools/check_availability", json=body).status_code == 401
    assert client.post("/tools/check_availability", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_offers_are_spread_over_days_and_respect_notice():
    r = avail()
    assert r["status"] == "ok"
    starts = [s["slot_start"] for s in r["slots"]]
    assert starts == ["2026-11-23T14:30:00+05:30", "2026-11-24T10:00:00+05:30", "2026-11-25T10:00:00+05:30"]
    assert r["slots"][0]["spoken"] == "Monday 23 November at 2:30 PM"


def test_weekend_has_no_slots_and_time_of_day_filter():
    assert avail(start_date="2026-11-28", end_date="2026-11-29")["status"] == "no_slots"
    for s in avail(time_of_day="afternoon", max_slots=5)["slots"]:
        assert 12 <= int(s["slot_start"][11:13]) < 17


def test_bookings_rotate_across_presenters():
    for i, s in enumerate(avail()["slots"]):
        assert book(s["slot_start"], email=f"p{i}@y.com")["status"] == "booked"
    assert {b.presenter_email for b in bookings()} == {"a@co.com", "b@co.com", "c@co.com"}


def test_same_slot_fills_three_presenters_then_is_taken():
    results = [book(email=f"p{i}@y.com") for i in range(4)]
    assert [r["status"] for r in results] == ["booked", "booked", "booked", "slot_taken"]
    assert len({b.presenter_email for b in bookings()}) == 3


def test_busy_presenters_are_skipped_and_full_slot_not_offered():
    for p in ("a@co.com", "b@co.com"):
        client.post("/tools/dev/fake_busy", params={"presenter": p, "start": TUE_10, "minutes": 30}, headers=H)
    assert book()["status"] == "booked"
    assert bookings()[0].presenter_email == "c@co.com"
    assert TUE_10 not in [s["slot_start"] for s in avail(start_date="2026-11-24", end_date="2026-11-24", max_slots=5)["slots"]]


def test_retried_booking_is_idempotent():
    first, second = book(), book()
    assert first["booking_id"] == second["booking_id"] and second["already_booked"]
    assert len(bookings()) == 1 and len(get_calendar().events) == 1


def test_invalid_slots_rejected():
    for bad in ("2026-11-28T10:00:00+05:30",   # Saturday
                "2026-11-24T10:10:00+05:30",   # not on the 30-minute grid
                "2026-11-24T20:00:00+05:30",   # after hours
                "2026-11-23T11:00:00+05:30"):  # in the past / too soon
        assert book(bad)["status"] == "invalid_slot", bad


def test_unreadable_calendar_fails_closed(monkeypatch):
    cal, real = get_calendar(), get_calendar().busy

    def flaky(email, s, e):
        if email == "a@co.com":
            raise RuntimeError("google down")
        return real(email, s, e)

    monkeypatch.setattr(cal, "busy", flaky)
    assert book()["status"] == "booked"
    assert bookings()[0].presenter_email != "a@co.com"


def test_calendar_event_failure_returns_error(monkeypatch):
    monkeypatch.setattr(get_calendar(), "create_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert book()["status"] == "error" and bookings() == []


def test_log_call_sheet_dnc_duplicates_and_retry():
    body = {"phone": "9876543210", "outcome": "do_not_call", "interaction_id": "i-1", "notes": "=IMPORTXML(1)"}
    assert client.post("/tools/log_call", json=body, headers=H).json() == {"status": "logged", "sheet": "ok"}
    assert len(get_sheets().rows) == 1
    with SessionLocal() as db:
        assert db.get(DoNotCall, "+919876543210")
    assert client.post("/tools/log_call", json=body, headers=H).json()["duplicate"] is True
    assert len(get_sheets().rows) == 1

    get_sheets().fail = True
    r = client.post("/tools/log_call", json={"phone": "9876543211", "outcome": "no_answer"}, headers=H).json()
    assert r["sheet"] == "pending_retry"
    get_sheets().fail = False
    assert client.post("/tools/retry_sheet_logs", headers=H).json() == {"retried_ok": 1, "still_failing": 0}
    assert len(get_sheets().rows) == 2


def test_scheduler_units():
    t = datetime(2026, 11, 24, 4, 30, tzinfo=timezone.utc)
    busy_end = [(t - sch.timedelta(minutes=60), t - sch.timedelta(minutes=10))]
    assert not sch.is_free(t, 20, 15, busy_end)   # ends 10 min before: inside the 15 min buffer
    assert sch.is_free(t, 20, 15, [(t - sch.timedelta(minutes=60), t - sch.timedelta(minutes=20))])
    assert sch.rank_presenters(["a", "b", "c"], {"a": 2, "b": 0, "c": 1}, {}, {}, 3) == ["b", "c", "a"]
    assert sch.rank_presenters(["a", "b"], {}, {"a": 3}, {}, 3) == ["b"]        # a is at the daily cap
    assert sch.rank_presenters(["a", "b"], {}, {}, {"a": t}, 3) == ["b", "a"]    # b never assigned -> first


def test_booked_call_row_gets_presenter_and_meeting_link():
    b = book(phone="9876543210", interaction_id="call-7")
    assert b["status"] == "booked"
    client.post("/tools/log_call", json={"phone": "9876543210", "outcome": "booked", "interaction_id": "call-7"}, headers=H)
    row = get_sheets().rows[-1]
    assert len(row) == 11
    assert row[9] == bookings()[0].presenter_email and row[10].startswith("https://meet.google.com/fake")
    # no interaction id: falls back to matching the phone number
    client.post("/tools/log_call", json={"phone": "+919876543210", "outcome": "booked"}, headers=H)
    assert get_sheets().rows[-1][9] == row[9]
    # not-booked calls leave the two new columns empty
    client.post("/tools/log_call", json={"phone": "9876543299", "outcome": "no_answer"}, headers=H)
    assert get_sheets().rows[-1][9:] == ["", ""]


# ----------------------------------------------------------------------------- slot matching + call_end hook
from app.slotmatch import match_offer

OFFERS = [{"slot_start": "2026-11-23T14:30:00+05:30", "spoken": "Monday 23 November at 2:30 PM"},
          {"slot_start": "2026-11-24T10:00:00+05:30", "spoken": "Tuesday 24 November at 10:00 AM"},
          {"slot_start": "2026-11-25T10:00:00+05:30", "spoken": "Wednesday 25 November at 10:00 AM"}]


@pytest.mark.parametrize("said,idx", [
    ("Tuesday 24 November at 10:00 AM", 1), ("tuesday at 10 am", 1), ("Monday", 0), ("2:30 pm", 0),
    ("the second one", 1), ("option 3", 2), ("2026-11-25T10:00:00+05:30", 2), ("wednesday at 10", 2)])
def test_match_offer(said, idx):
    assert match_offer(said, OFFERS) == OFFERS[idx]


@pytest.mark.parametrize("said", ["", "none", "10 am", "sometime next week", "Friday"])
def test_match_offer_refuses_unclear(said):
    assert match_offer(said, OFFERS) is None   # "10 am" is ambiguous (Tue and Wed)


def end(**kw):
    body = {"interaction_id": "int-1", "phone": "9876543210", "call_outcome": "booked", "prospect_name": "Asha",
            "company": "Acme", "prospect_email": "asha@acme.com", "chosen_slot": "Tuesday at 10 AM", **kw}
    r = client.post("/tools/call_end", json=body, headers=H)
    assert r.status_code == 200
    return r.json()


def test_availability_returns_speech_and_remembers_offers():
    r = avail(interaction_id="int-1")
    assert r["speech"].startswith("I have Monday 23 November at 2:30 PM, Tuesday 24 November at 10:00 AM, or ")
    out = end()
    assert out["outcome"] == "booked" and out["booking"]["status"] == "booked"
    assert len(bookings()) == 1
    row = get_sheets().rows[-1]
    assert row[4] == "booked" and row[5] == "Tuesday 24 November at 10:00 AM" and row[9] and row[10].startswith("https://meet")


def test_call_end_is_idempotent():
    avail(interaction_id="int-1")
    end(), end()
    assert len(bookings()) == 1 and len(get_sheets().rows) == 1


def test_call_end_unmatched_slot_or_bad_email_is_flagged_not_lost():
    avail(interaction_id="int-1")
    out = end(chosen_slot="sometime next week")
    assert out["outcome"] == "callback" and bookings() == []
    assert "NEEDS FOLLOW-UP" in get_sheets().rows[-1][7]
    avail(interaction_id="int-2")
    out = end(interaction_id="int-2", prospect_email="not-an-email")
    assert out["outcome"] == "callback" and "invalid email" in get_sheets().rows[-1][7]


def test_call_end_other_outcomes_and_dnc():
    assert end(call_outcome="Not Interested", interaction_id="a")["outcome"] == "not_interested"
    assert end(call_outcome="do not call", phone="9811111111", interaction_id="b")["outcome"] == "do_not_call"
    with SessionLocal() as db:
        assert db.get(DoNotCall, "+919811111111")
    assert end(call_outcome="", chosen_slot="", prospect_email="", interaction_id="c")["outcome"] == "callback"


def test_booking_without_email_is_deduped_by_phone_and_sends_no_invite():
    a = book(email=None, phone="9876543210")
    b = book(email=None, phone="9876543210")
    assert a["status"] == "booked" and "invite_sent_to" not in a
    assert b["booking_id"] == a["booking_id"] and b["already_booked"] is True
    assert len(bookings()) == 1 and bookings()[0].prospect_phone == "+919876543210"
    assert list(get_calendar().events.values())[0]["attendee"] is None
