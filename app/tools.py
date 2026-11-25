"""The three tools the voice agent calls mid-call: check_availability, book_demo, log_call."""
import hmac
import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import scheduler as sch
from .config import settings
from .csv_import import normalize_phone
from .db import get_db
from .google_clients import get_calendar, get_sheets
from .models import Booking, CallLog, DoNotCall, Offer
from .slotmatch import match_offer
from .scheduling import as_utc

log = logging.getLogger("tools")
router = APIRouter(prefix="/tools", tags=["agent tools"])
bearer = HTTPBearer(auto_error=False)
_BOOK_LOCK = threading.Lock()
UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def require_key(creds: HTTPAuthorizationCredentials | None = Depends(bearer)):
    key = settings.tools_api_key
    if settings.calendar_backend == "google" and key in ("", "dev-tools-key"):
        raise HTTPException(503, "Set TOOLS_API_KEY to a long random string before using Google")
    if not creds or not hmac.compare_digest(creds.credentials, key):
        raise HTTPException(401, "bad or missing bearer token")


def _tz(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _spoken(dt: datetime, tz) -> str:
    d = dt.astimezone(tz)
    return f"{d:%A} {d.day} {d:%B} at {d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def _safe_busy(cal, email, start, end):
    """If a calendar can't be read, treat that presenter as busy. Never book on unknown availability."""
    try:
        return cal.busy(email, start, end)
    except Exception:
        log.exception("calendar lookup failed for %s; treating as busy", email)
        return [(start, end)]


def _loads(db: Session, now: datetime, w_start: datetime):
    ptz = ZoneInfo(settings.presenter_timezone)
    lower = min(now, w_start) - timedelta(days=1)
    week, day = {}, {}
    for b in db.scalars(select(Booking).where(Booking.status == "booked", Booking.start_utc >= lower)):
        st = as_utc(b.start_utc)
        if now <= st < now + timedelta(days=7):
            week[b.presenter_email] = week.get(b.presenter_email, 0) + 1
        key = (b.presenter_email, st.astimezone(ptz).date())
        day[key] = day.get(key, 0) + 1
    last = {e: as_utc(t) for e, t in db.execute(
        select(Booking.presenter_email, func.max(Booking.created_at)).group_by(Booking.presenter_email)) if t}
    return week, day, last


def _plan(db: Session, cal, slots: list, now: datetime):
    """slot -> presenters who could take it, best first. Also returns this week's load per presenter."""
    if not slots:
        return {}, {}
    dur, buf = settings.demo_minutes, settings.buffer_minutes
    w_start = min(slots) - timedelta(minutes=buf)
    w_end = max(slots) + timedelta(minutes=dur + buf)
    busy = {p: _safe_busy(cal, p, w_start, w_end) for p in settings.presenters}
    week, day, last = _loads(db, now, w_start)
    ptz = ZoneInfo(settings.presenter_timezone)
    plan = {}
    for s in slots:
        free = [p for p in settings.presenters if sch.is_free(s, dur, buf, busy[p])]
        dl = {p: day.get((p, s.astimezone(ptz).date()), 0) for p in free}
        ranked = sch.rank_presenters(free, week, dl, last, settings.max_demos_per_day)
        if ranked:
            plan[s] = ranked
    return plan, week


def _universe(first: date, last: date, prospect_tz, now: datetime, time_of_day: str = "any") -> list:
    ptz = ZoneInfo(settings.presenter_timezone)
    slots = sch.generate_slots(
        first - timedelta(days=1), last + timedelta(days=1), tz=ptz,
        work_start=sch.parse_hhmm(settings.work_start), work_end=sch.parse_hhmm(settings.work_end),
        work_days=set(settings.work_days), step_min=settings.slot_step_minutes,
        duration_min=settings.demo_minutes, earliest_utc=now + timedelta(hours=settings.min_notice_hours))
    out = []
    for s in slots:
        local = s.astimezone(prospect_tz)
        if not (first <= local.date() <= last):
            continue
        h = local.hour
        if time_of_day == "morning" and h >= 12:
            continue
        if time_of_day == "afternoon" and not (12 <= h < 17):
            continue
        if time_of_day == "evening" and h < 17:
            continue
        out.append(s)
    return out


# ----------------------------------------------------------------------------- check_availability
class CheckIn(BaseModel):
    start_date: date
    end_date: date | None = None
    timezone: str = "Asia/Kolkata"
    time_of_day: Literal["any", "morning", "afternoon", "evening"] = "any"
    max_slots: int = Field(3, ge=1, le=5)
    interaction_id: str | None = Field(None, max_length=100)


def _speech(spoken: list) -> str:
    if len(spoken) == 1:
        return f"I have {spoken[0]}."
    return "I have " + ", ".join(spoken[:-1]) + f"{',' if len(spoken) > 2 else ''} or {spoken[-1]}."


def _save_offers(db: Session, interaction_id: str, slots: list):
    row = db.scalar(select(Offer).where(Offer.interaction_id == interaction_id))
    old = json.loads(row.slots_json) if row else []
    seen = {x["slot_start"] for x in old}
    merged = old + [x for x in slots if x["slot_start"] not in seen]
    if row:
        row.slots_json = json.dumps(merged)
    else:
        db.add(Offer(interaction_id=interaction_id, slots_json=json.dumps(merged)))
    db.commit()


@router.post("/check_availability", dependencies=[Depends(require_key)])
def check_availability(body: CheckIn, db: Session = Depends(get_db)):
    tz = _tz(body.timezone)
    if tz is None:
        return {"status": "error", "message": "Unknown timezone."}
    now = _now()
    first = max(body.start_date, now.astimezone(tz).date())
    last = body.end_date if body.end_date and body.end_date >= first else first + timedelta(days=2)
    last = min(last, first + timedelta(days=settings.max_lookahead_days))
    slots = _universe(first, last, tz, now, body.time_of_day)
    plan, week = _plan(db, get_calendar(), slots, now)
    eligible = [sch.Eligible(s, week.get(r[0], 0), s.astimezone(tz).date()) for s, r in plan.items()]
    offers = sch.choose_offers(eligible, n=body.max_slots)
    if not offers:
        return {"status": "no_slots", "message": "No free slots in that range. Ask for other days or times."}
    slots_out = [{"slot_start": o.start.astimezone(tz).isoformat(), "spoken": _spoken(o.start, tz)} for o in offers]
    if body.interaction_id:
        _save_offers(db, body.interaction_id, slots_out)
    return {"status": "ok", "timezone": body.timezone, "speech": _speech([x["spoken"] for x in slots_out]),
            "slots": slots_out}


# ----------------------------------------------------------------------------- book_demo
class BookIn(BaseModel):
    slot_start: datetime
    timezone: str = "Asia/Kolkata"
    prospect_name: str = Field(min_length=1, max_length=200)
    prospect_email: str | None = Field(None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)
    company: str | None = Field(None, max_length=200)
    phone: str | None = Field(None, max_length=32)
    interaction_id: str | None = Field(None, max_length=100)


def _booked(b: Booking, tz, already: bool = False) -> dict:
    out = {"status": "booked", "booking_id": b.id, "start": as_utc(b.start_utc).astimezone(tz).isoformat(),
           "spoken": _spoken(as_utc(b.start_utc), tz), "meet_link": b.meet_link}
    if b.prospect_email:
        out["invite_sent_to"] = b.prospect_email
    if already:
        out["already_booked"] = True
    return out


@router.post("/book_demo", dependencies=[Depends(require_key)])
def book_demo(body: BookIn, db: Session = Depends(get_db)):
    tz = _tz(body.timezone)
    if tz is None:
        return {"status": "error", "message": "Unknown timezone."}
    slot = body.slot_start if body.slot_start.tzinfo else body.slot_start.replace(tzinfo=tz)
    slot = slot.astimezone(UTC)
    now, email, cal = _now(), (body.prospect_email or "").lower(), get_calendar()
    phone = (normalize_phone(body.phone) or body.phone) if body.phone else None
    with _BOOK_LOCK:
        dup_q = select(Booking).where(Booking.start_utc == slot, Booking.status == "booked")
        if email:
            dup = db.scalar(dup_q.where(Booking.prospect_email == email))
        elif phone:
            dup = db.scalar(dup_q.where(Booking.prospect_phone == phone))
        else:
            dup = None
        if dup:  # the same tool call retried: return the existing booking
            return _booked(dup, tz, already=True)
        day = slot.astimezone(ZoneInfo(settings.presenter_timezone)).date()
        if slot not in _universe(day - timedelta(days=1), day + timedelta(days=1), ZoneInfo(settings.presenter_timezone), now):
            return {"status": "invalid_slot", "message": "That time can't be booked. Call check_availability again."}
        plan, _ = _plan(db, cal, [slot], now)
        ranked = plan.get(slot, [])
        if not ranked:
            return {"status": "slot_taken", "message": "That slot was just taken. Call check_availability again."}
        end = slot + timedelta(minutes=settings.demo_minutes)
        calendar_error = False
        for presenter in ranked:
            try:
                ev = cal.create_event(
                    presenter, summary=f"{settings.company_name} demo: {body.company or body.prospect_name}",
                    description=(f"Prospect: {body.prospect_name}\nCompany: {body.company or '-'}\n"
                                 f"Phone: {body.phone or '-'}\nBooked by the AI calling agent."),
                    start=slot, end=end, tz=settings.presenter_timezone, attendee=email or None)
            except Exception:
                log.exception("create_event failed for %s", presenter)
                calendar_error = True
                continue
            b = Booking(presenter_email=presenter, start_utc=slot, end_utc=end, prospect_name=body.prospect_name,
                        prospect_email=email, prospect_phone=(normalize_phone(body.phone) or body.phone) if body.phone else None,
                        company=body.company,
                        prospect_timezone=body.timezone, event_id=ev["event_id"], meet_link=ev.get("meet_link"),
                        html_link=ev.get("html_link"), interaction_id=body.interaction_id)
            db.add(b)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                try:
                    cal.delete_event(presenter, ev["event_id"])
                except Exception:
                    log.exception("could not remove orphan event %s", ev["event_id"])
                continue
            return _booked(b, tz)
        if calendar_error:
            return {"status": "error", "message": "Calendar is unavailable right now. Offer to follow up by message."}
        return {"status": "slot_taken", "message": "That slot was just taken. Call check_availability again."}


# ----------------------------------------------------------------------------- log_call
class LogIn(BaseModel):
    phone: str = Field(min_length=3, max_length=32)
    outcome: Literal["booked", "callback", "not_interested", "do_not_call", "wrong_person", "no_answer", "voicemail"]
    prospect_name: str | None = Field(None, max_length=200)
    company: str | None = Field(None, max_length=200)
    demo_time: str | None = Field(None, max_length=100)
    pain_points: str | None = Field(None, max_length=2000)
    notes: str | None = Field(None, max_length=2000)
    interaction_id: str | None = Field(None, max_length=100)


def _booking_for(db: Session, c: CallLog):
    """The demo this call produced: matched by interaction id, else by phone number (newest first)."""
    if c.outcome != "booked":
        return None
    if c.interaction_id:
        b = db.scalar(select(Booking).where(Booking.interaction_id == c.interaction_id, Booking.status == "booked")
                      .order_by(Booking.id.desc()))
        if b:
            return b
    return db.scalar(select(Booking).where(Booking.prospect_phone == c.phone, Booking.status == "booked")
                     .order_by(Booking.id.desc()))


def _sheet_row(c: CallLog, db: Session) -> list:
    ts = as_utc(c.created_at).astimezone(ZoneInfo(settings.presenter_timezone)).strftime("%Y-%m-%d %H:%M:%S")
    b = _booking_for(db, c)
    return [ts, c.phone, c.prospect_name or "", c.company or "", c.outcome, c.demo_time or "",
            c.pain_points or "", c.notes or "", c.interaction_id or "",
            b.presenter_email if b else "", (b.meet_link or "") if b else ""]


@router.post("/log_call", dependencies=[Depends(require_key)])
def log_call(body: LogIn, db: Session = Depends(get_db)):
    if body.interaction_id:
        old = db.scalar(select(CallLog).where(CallLog.interaction_id == body.interaction_id))
        if old:
            return {"status": "logged", "duplicate": True, "sheet": "ok" if old.sheet_status == "ok" else "pending_retry"}
    phone = normalize_phone(body.phone) or body.phone.strip()
    row = CallLog(phone=phone, outcome=body.outcome, prospect_name=body.prospect_name, company=body.company,
                  demo_time=body.demo_time, pain_points=body.pain_points, notes=body.notes,
                  interaction_id=body.interaction_id)
    db.add(row)
    if body.outcome == "do_not_call" and phone.startswith("+") and not db.get(DoNotCall, phone):
        db.add(DoNotCall(phone=phone, reason="opted out on call"))
    db.flush()
    try:
        get_sheets().append_row(_sheet_row(row, db))
        row.sheet_status = "ok"
    except Exception:
        log.exception("sheet append failed; saved locally, retry later")
        row.sheet_status = "failed"
    db.commit()
    return {"status": "logged", "sheet": "ok" if row.sheet_status == "ok" else "pending_retry"}


# ----------------------------------------------------------------------------- call_end (end-of-call hook)
class CallEndIn(BaseModel):
    """Sent once when a call ends. Everything is optional text because it comes from variables the voice
    platform extracted from the conversation; empty strings are normal."""
    interaction_id: str | None = Field(None, max_length=100)
    phone: str = Field(min_length=3, max_length=32)
    call_outcome: str = Field("", max_length=50)
    prospect_name: str = Field("", max_length=200)
    company: str = Field("", max_length=200)
    prospect_email: str = Field("", max_length=200)
    chosen_slot: str = Field("", max_length=200)
    pain_points: str = Field("", max_length=2000)
    notes: str = Field("", max_length=2000)
    timezone: str = "Asia/Kolkata"


OUTCOMES = {"booked", "callback", "not_interested", "do_not_call", "wrong_person", "no_answer", "voicemail"}


@router.post("/call_end", dependencies=[Depends(require_key)])
def call_end(body: CallEndIn, db: Session = Depends(get_db)):
    """Books the demo the caller agreed to (if any) and logs the call. A failed booking is never lost:
    the call is logged as 'callback' with a note so a human can follow up."""
    outcome = body.call_outcome.strip().lower().replace(" ", "_").replace("-", "_")
    if outcome not in OUTCOMES:
        outcome = "booked" if body.chosen_slot.strip() else "callback"
    notes, demo_time, booking = body.notes.strip(), None, None
    if outcome == "booked":
        offers = []
        if body.interaction_id:
            row = db.scalar(select(Offer).where(Offer.interaction_id == body.interaction_id))
            offers = json.loads(row.slots_json) if row else []
        slot = match_offer(body.chosen_slot, offers)
        problem = None
        if slot is None:
            problem = f"could not match chosen slot '{body.chosen_slot}' to an offered time"
        else:
            try:
                booking = book_demo(BookIn(
                    slot_start=slot["slot_start"], timezone=body.timezone,
                    prospect_name=body.prospect_name.strip() or "Prospect",
                    prospect_email=body.prospect_email.strip() or None, company=body.company.strip() or None,
                    phone=body.phone, interaction_id=body.interaction_id), db)
            except ValueError:
                problem = f"invalid email '{body.prospect_email}'"
            else:
                if booking["status"] == "booked":
                    demo_time = slot["spoken"]
                else:
                    problem = f"booking returned {booking['status']}"
        if problem:
            outcome = "callback"
            notes = f"BOOKING NEEDS FOLLOW-UP: {problem}. {notes}".strip()
    logged = log_call(LogIn(
        phone=body.phone, outcome=outcome, prospect_name=body.prospect_name or None, company=body.company or None,
        demo_time=demo_time, pain_points=body.pain_points or None, notes=notes or None,
        interaction_id=body.interaction_id), db)
    return {"status": "ok", "outcome": outcome, "booking": booking, "log": logged}


@router.post("/retry_sheet_logs", dependencies=[Depends(require_key)])
def retry_sheet_logs(db: Session = Depends(get_db)):
    done = failed = 0
    for row in db.scalars(select(CallLog).where(CallLog.sheet_status == "failed").order_by(CallLog.id)):
        try:
            get_sheets().append_row(_sheet_row(row, db))
            row.sheet_status = "ok"
            done += 1
        except Exception:
            failed += 1
    db.commit()
    return {"retried_ok": done, "still_failing": failed}


# ----------------------------------------------------------------------------- dev helper (fake backend only)
@router.post("/dev/fake_busy", dependencies=[Depends(require_key)])
def fake_busy(presenter: str, start: datetime, minutes: int = 60):
    """Block a presenter's time on the fake calendar, to test assignment and conflicts."""
    if settings.calendar_backend != "fake":
        raise HTTPException(404, "only available with CALENDAR_BACKEND=fake")
    s = start.astimezone(UTC)
    get_calendar().add_busy(presenter.lower(), s, s + timedelta(minutes=minutes))
    return {"blocked": presenter.lower(), "from": s.isoformat(), "minutes": minutes}
