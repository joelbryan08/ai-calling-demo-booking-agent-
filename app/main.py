import asyncio
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .db import engine, get_db
from .models import Base, Campaign, Lead, CampaignLead, Call, DoNotCall
from .csv_import import parse_leads_csv, normalize_phone
from .config import settings
from .dialer import dialer_loop, apply_hangup
from .webhooks import router as webhook_router
from .tools import router as tools_router
from .voice.routes import router as voice_router
from .admin import admin_guard, router as admin_router
from pathlib import Path
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

Base.metadata.create_all(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(dialer_loop()) if settings.dialer_enabled else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Demo Booking Agent", lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(tools_router)
app.include_router(voice_router)
app.include_router(admin_router)
app.middleware("http")(admin_guard)
app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/")

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
TRANSITIONS = {
    "start": ({"draft"}, "running"),
    "pause": ({"running"}, "paused"),
    "resume": ({"paused"}, "running"),
    "cancel": ({"draft", "running", "paused"}, "cancelled"),
}


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    caller_number: str | None = None
    window_start: str = Field("10:00", pattern=HHMM)
    window_end: str = Field("20:00", pattern=HHMM)
    default_timezone: str = "Asia/Kolkata"
    max_parallel: int = Field(2, ge=1, le=20)
    max_attempts: int = Field(3, ge=1, le=10)
    retry_gap_minutes: int = Field(120, ge=5, le=1440)

    @model_validator(mode="after")
    def _window(self):
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be earlier than window_end")
        return self

    @field_validator("default_timezone")
    @classmethod
    def _tz(cls, v):
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone")
        return v


class DncIn(BaseModel):
    phone: str
    reason: str | None = None


def _campaign_or_404(db: Session, cid: int) -> Campaign:
    c = db.get(Campaign, cid)
    if not c:
        raise HTTPException(404, "campaign not found")
    return c


def _campaign_dict(db: Session, c: Campaign) -> dict:
    counts = dict(db.execute(
        select(CampaignLead.status, func.count()).where(CampaignLead.campaign_id == c.id)
        .group_by(CampaignLead.status)).all())
    return {
        "id": c.id, "name": c.name, "status": c.status, "caller_number": c.caller_number,
        "window": f"{c.window_start}-{c.window_end}", "default_timezone": c.default_timezone,
        "max_parallel": c.max_parallel, "max_attempts": c.max_attempts,
        "retry_gap_minutes": c.retry_gap_minutes, "lead_counts": counts,
        "total_leads": sum(counts.values()),
    }


@app.post("/campaigns", status_code=201)
def create_campaign(body: CampaignIn, db: Session = Depends(get_db)):
    c = Campaign(**body.model_dump())
    db.add(c)
    db.commit()
    return _campaign_dict(db, c)


@app.get("/campaigns")
def list_campaigns(db: Session = Depends(get_db)):
    return [_campaign_dict(db, c) for c in db.scalars(select(Campaign).order_by(Campaign.id.desc()))]


@app.get("/campaigns/{cid}")
def get_campaign(cid: int, db: Session = Depends(get_db)):
    return _campaign_dict(db, _campaign_or_404(db, cid))


@app.post("/campaigns/{cid}/leads/upload")
async def upload_leads(cid: int, file: UploadFile = File(...), region: str = Query("IN", min_length=2, max_length=2),
                       db: Session = Depends(get_db)):
    c = _campaign_or_404(db, cid)
    if c.status not in ("draft", "paused"):
        raise HTTPException(409, f"can't add leads while campaign is {c.status}")
    try:
        valid, rejected, dupes, total = parse_leads_csv(await file.read(), region.upper())
    except ValueError as e:
        raise HTTPException(400, str(e))

    phones = [r["phone"] for r in valid]
    dnc = set(db.scalars(select(DoNotCall.phone).where(DoNotCall.phone.in_(phones)))) if phones else set()
    existing = {l.phone: l for l in db.scalars(select(Lead).where(Lead.phone.in_(phones)))} if phones else {}
    in_campaign = set(db.scalars(
        select(Lead.phone).join(CampaignLead, CampaignLead.lead_id == Lead.id)
        .where(CampaignLead.campaign_id == cid)))

    added = skipped_dnc = already = 0
    for r in valid:
        if r["phone"] in dnc:
            skipped_dnc += 1
            continue
        if r["phone"] in in_campaign:
            already += 1
            continue
        lead = existing.get(r["phone"])
        if lead is None:
            lead = Lead(**r)
            db.add(lead)
        else:  # fill blanks only, never overwrite
            for k, v in r.items():
                if v and not getattr(lead, k):
                    setattr(lead, k, v)
        db.flush()
        db.add(CampaignLead(campaign_id=cid, lead_id=lead.id))
        added += 1
    db.commit()
    return {"total_rows": total, "added": added, "rejected": len(rejected),
            "rejected_details": rejected[:50], "duplicates_in_file": dupes,
            "already_in_campaign": already, "skipped_dnc": skipped_dnc}


@app.get("/campaigns/{cid}/leads")
def list_campaign_leads(cid: int, status: str | None = None, limit: int = Query(50, le=500), offset: int = 0,
                        db: Session = Depends(get_db)):
    _campaign_or_404(db, cid)
    q = (select(CampaignLead, Lead).join(Lead, Lead.id == CampaignLead.lead_id)
         .where(CampaignLead.campaign_id == cid).order_by(CampaignLead.id).limit(limit).offset(offset))
    if status:
        q = q.where(CampaignLead.status == status)
    return [{"id": cl.id, "status": cl.status, "attempts": cl.attempts, "phone": l.phone, "name": l.name,
             "company": l.company, "email": l.email, "timezone": l.timezone} for cl, l in db.execute(q)]


@app.post("/campaigns/{cid}/{action}")
def change_state(cid: int, action: str, db: Session = Depends(get_db)):
    if action not in TRANSITIONS:
        raise HTTPException(404, "unknown action")
    c = _campaign_or_404(db, cid)
    allowed_from, new = TRANSITIONS[action]
    if c.status not in allowed_from:
        raise HTTPException(409, f"can't {action} a {c.status} campaign")
    if action == "start":
        pending = db.scalar(select(func.count()).select_from(CampaignLead)
                            .where(CampaignLead.campaign_id == cid, CampaignLead.status == "pending"))
        if not pending:
            raise HTTPException(409, "campaign has no pending leads")
        if not (c.caller_number or settings.default_caller_number):
            raise HTTPException(409, "set caller_number on the campaign or DEFAULT_CALLER_NUMBER in .env")
    c.status = new
    db.commit()
    return _campaign_dict(db, c)


@app.post("/dnc", status_code=201)
def add_dnc(body: DncIn, db: Session = Depends(get_db)):
    phone = normalize_phone(body.phone)
    if not phone:
        raise HTTPException(400, "invalid phone number")
    if not db.get(DoNotCall, phone):
        db.add(DoNotCall(phone=phone, reason=body.reason))
        db.commit()
    return {"phone": phone}


@app.get("/campaigns/{cid}/calls")
def list_calls(cid: int, limit: int = Query(50, le=500), offset: int = 0, db: Session = Depends(get_db)):
    _campaign_or_404(db, cid)
    q = (select(Call, Lead).join(CampaignLead, CampaignLead.id == Call.campaign_lead_id)
         .join(Lead, Lead.id == CampaignLead.lead_id).where(CampaignLead.campaign_id == cid)
         .order_by(Call.id.desc()).limit(limit).offset(offset))
    return [{"call_id": k.id, "phone": l.phone, "name": l.name, "status": k.status, "outcome": k.outcome,
             "duration_sec": k.duration_sec, "started_at": k.started_at, "ended_at": k.ended_at,
             "provider_call_id": k.provider_call_id} for k, l in db.execute(q)]


@app.post("/dev/calls/{call_id}/simulate")
def simulate_call_end(call_id: int, status: str = "completed", db: Session = Depends(get_db)):
    """Fake-provider only: finish a call as if the telephony provider reported `status`
    (completed / busy / no-answer / failed)."""
    if settings.provider != "fake":
        raise HTTPException(404, "not available")
    if not apply_hangup(db, call_id, status, 30 if status == "completed" else 0):
        raise HTTPException(404, "call not found")
    return {"ok": True}
