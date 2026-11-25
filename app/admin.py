"""Dashboard login + read-only endpoints for the UI. Management routes require a signed-in admin."""
import hashlib
import hmac
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import Booking, Call, CallInsight, Campaign, CampaignLead, DoNotCall, Lead
from . import runtime_settings
from .scheduling import as_utc
from .voice.session import LIVE

router = APIRouter(prefix="/api")
PROTECTED = ("/campaigns", "/dnc", "/dev", "/api")
_fails: dict = {}


def _sig(exp: str) -> str:
    key = hashlib.sha256(f"{settings.admin_password}|{settings.webhook_secret}".encode()).digest()
    return hmac.new(key, exp.encode(), "sha256").hexdigest()


def make_token(ttl: int = 43200) -> str:
    exp = str(int(time.time()) + ttl)
    return f"{exp}.{_sig(exp)}"


def valid(token: str) -> bool:
    try:
        exp, sig = token.split(".")
        return int(exp) > time.time() and hmac.compare_digest(sig, _sig(exp))
    except ValueError:
        return False


async def admin_guard(request: Request, call_next):
    p = request.url.path
    if p.startswith(PROTECTED) and p != "/api/login":
        if not settings.admin_password:
            return JSONResponse({"detail": "Set ADMIN_PASSWORD in .env to use the dashboard."}, 503)
        if not valid(request.headers.get("authorization", "")[7:]):
            return JSONResponse({"detail": "Not signed in"}, 401)
    return await call_next(request)


class Login(BaseModel):
    password: str


@router.post("/login")
def login(body: Login, request: Request):
    ip = request.client.host if request.client else "?"
    n, t = _fails.get(ip, (0, 0.0))
    n = 0 if time.time() - t > 60 else n
    if n >= 6:
        raise HTTPException(429, "Too many attempts. Wait a minute.")
    if not settings.admin_password:
        raise HTTPException(503, "Set ADMIN_PASSWORD in .env first.")
    if not hmac.compare_digest(body.password.encode(), settings.admin_password.encode()):
        _fails[ip] = (n + 1, time.time())
        raise HTTPException(401, "Wrong password")
    _fails.pop(ip, None)
    return {"token": make_token()}


def _demos(db: Session, upcoming: bool, limit: int) -> list:
    q = select(Booking).where(Booking.status == "booked")
    if upcoming:
        q = q.where(Booking.start_utc >= datetime.now(timezone.utc)).order_by(Booking.start_utc)
    else:
        q = q.order_by(Booking.start_utc.desc())
    return [{"id": b.id, "start": as_utc(b.start_utc).isoformat(), "presenter": b.presenter_email,
             "name": b.prospect_name, "phone": b.prospect_phone, "company": b.company, "meet_link": b.meet_link}
            for b in db.scalars(q.limit(limit))]


def _charts(db: Session) -> dict:
    ist, now = ZoneInfo(settings.presenter_timezone), datetime.now(timezone.utc)
    days = [(now.astimezone(ist) - timedelta(days=d)).date() for d in range(13, -1, -1)]
    day = lambda t: as_utc(t).astimezone(ist).date()  # noqa: E731
    cut = now - timedelta(days=15)
    calls = Counter(day(t) for (t,) in db.execute(select(Call.created_at).where(Call.created_at >= cut)))
    made = Counter(day(t) for (t,) in db.execute(select(Booking.created_at).where(Booking.created_at >= cut)))
    by_p = dict(db.execute(select(Booking.presenter_email, func.count()).where(Booking.status == "booked")
                           .group_by(Booking.presenter_email)).all())
    return {"daily": [{"d": d.isoformat(), "calls": calls[d], "demos": made[d]} for d in days], "by_presenter": by_p}


@router.get("/live")
def live():
    return [s.snapshot() for s in list(LIVE.values())]


class SettingsIn(BaseModel):
    agent_name: str | None = Field(None, max_length=60)
    company_name: str | None = Field(None, max_length=60)
    product_name: str | None = Field(None, max_length=60)
    problem_area: str | None = Field(None, max_length=200)
    product_pitch: str | None = Field(None, max_length=500)
    opening_line: str | None = Field(None, max_length=300)
    extra_rules: str | None = Field(None, max_length=2000)


@router.get("/settings")
def get_settings():
    return {"values": runtime_settings.load(), "defaults": runtime_settings.defaults()}


@router.put("/settings")
def put_settings(body: SettingsIn):
    runtime_settings.save(body.model_dump(exclude_none=True))
    return {"values": runtime_settings.load()}


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    outcomes = {k or "in progress": v for k, v in db.execute(select(Call.outcome, func.count()).group_by(Call.outcome))}
    count = lambda q: db.scalar(q) or 0  # noqa: E731
    return {"leads": count(select(func.count()).select_from(Lead)), "calls": sum(outcomes.values()),
            "outcomes": outcomes, "running": count(select(func.count()).select_from(Campaign).where(Campaign.status == "running")),
            "demos": count(select(func.count()).select_from(Booking).where(Booking.status == "booked")),
            "upcoming": _demos(db, True, 6), **_charts(db)}


@router.get("/demos")
def demos(db: Session = Depends(get_db)):
    return _demos(db, False, 300)


@router.get("/calls/{call_id}")
def call_detail(call_id: int, db: Session = Depends(get_db)):
    row = db.execute(select(Call, Lead).join(CampaignLead, CampaignLead.id == Call.campaign_lead_id)
                     .join(Lead, Lead.id == CampaignLead.lead_id).where(Call.id == call_id)).first()
    if not row:
        raise HTTPException(404, "call not found")
    c, lead = row
    ins = db.get(CallInsight, call_id)
    return {"id": c.id, "name": lead.name, "phone": lead.phone, "status": c.status, "outcome": c.outcome,
            "duration_sec": c.duration_sec, "transcript": c.transcript or "",
            "summary": ins.summary if ins else "", "sentiment": ins.sentiment if ins else "",
            "pain_points": ins.pain_points if ins else "", "next_step": ins.next_step if ins else "",
            "has_recording": (Path(settings.recordings_dir) / f"call-{c.id}.wav").is_file()}


@router.get("/calls/{call_id}/recording")
def call_recording(call_id: int):
    path = Path(settings.recordings_dir) / f"call-{int(call_id)}.wav"
    if not path.is_file():
        raise HTTPException(404, "no recording for this call")
    return FileResponse(path, media_type="audio/wav")


@router.get("/dnc")
def dnc(db: Session = Depends(get_db)):
    return [{"phone": d.phone, "reason": d.reason, "added": as_utc(d.created_at).isoformat()}
            for d in db.scalars(select(DoNotCall).order_by(DoNotCall.created_at.desc()).limit(500))]
