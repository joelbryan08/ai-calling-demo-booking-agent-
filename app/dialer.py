import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, or_, update

from .config import settings
from .db import SessionLocal
from .models import Campaign, Lead, CampaignLead, Call, DoNotCall
from .providers import TunnelNotReady, get_provider
from .scheduling import window_status

log = logging.getLogger("dialer")

STATUS_TO_OUTCOME = {"completed": "answered", "busy": "busy", "timeout": "no_answer",
                     "no-answer": "no_answer", "cancel": "no_answer"}  # anything else -> failed


def _now():
    return datetime.now(timezone.utc)


def finish_attempt(db, call: Call, outcome: str, now: datetime, duration: int | None = None):
    """Close a call and decide what happens to the lead next (done / retry / exhausted)."""
    cl = db.get(CampaignLead, call.campaign_lead_id)
    db.refresh(cl)
    c = db.get(Campaign, cl.campaign_id)
    call.outcome, call.ended_at, call.duration_sec = outcome, now, duration
    cl.last_outcome = outcome
    if outcome == "answered":
        cl.status, cl.next_attempt_at = "done", None
    elif cl.attempts < c.max_attempts:
        cl.status = "pending"
        cl.next_attempt_at = now + timedelta(minutes=c.retry_gap_minutes)
    else:
        cl.status = "failed"  # retries exhausted
    db.commit()
    maybe_complete(db, c)


def maybe_complete(db, c: Campaign):
    if c.status != "running":
        return
    open_leads = db.scalar(select(func.count()).select_from(CampaignLead).where(
        CampaignLead.campaign_id == c.id, CampaignLead.status.in_(["pending", "in_progress"])))
    if not open_leads:
        c.status = "completed"
        db.commit()


def apply_hangup(db, call_id: int, provider_status: str, duration: int | None, provider_uuid: str | None = None):
    call = db.get(Call, call_id)
    if not call or call.ended_at:  # unknown or duplicate webhook
        return call
    call.status = (provider_status or "failed").lower()
    if provider_uuid and not call.provider_call_id:
        call.provider_call_id = provider_uuid
    finish_attempt(db, call, STATUS_TO_OUTCOME.get(call.status, "failed"), _now(), duration)
    return call


def _reap_stale(db, now: datetime):
    cutoff = now - timedelta(minutes=settings.stale_call_minutes)
    for call in db.scalars(select(Call).where(Call.ended_at.is_(None), Call.created_at < cutoff)).all():
        log.warning("call %s never reported back; marking failed", call.id)
        call.status = "no_callback"
        finish_attempt(db, call, "failed", now)


def _dial_campaign(db, c: Campaign, provider, now: datetime):
    in_progress = db.scalar(select(func.count()).select_from(CampaignLead).where(
        CampaignLead.campaign_id == c.id, CampaignLead.status == "in_progress"))
    capacity = c.max_parallel - in_progress
    if capacity <= 0:
        return
    caller = c.caller_number or settings.default_caller_number
    rows = db.execute(
        select(CampaignLead, Lead).join(Lead, Lead.id == CampaignLead.lead_id)
        .where(CampaignLead.campaign_id == c.id, CampaignLead.status == "pending",
               or_(CampaignLead.next_attempt_at.is_(None), CampaignLead.next_attempt_at <= now))
        .order_by(CampaignLead.id).limit(capacity * 10)).all()
    placed = 0
    for cl, lead in rows:
        if placed >= capacity:
            break
        if db.get(DoNotCall, lead.phone):
            cl.status, cl.last_outcome = "skipped", "dnc"
            db.commit()
            continue
        ok, next_start = window_status(now, lead.timezone or c.default_timezone, c.window_start, c.window_end)
        if not ok:
            cl.next_attempt_at = next_start  # don't re-check this lead until the window opens
            db.commit()
            continue
        claimed = db.execute(update(CampaignLead)
                             .where(CampaignLead.id == cl.id, CampaignLead.status == "pending")
                             .values(status="in_progress", attempts=CampaignLead.attempts + 1)).rowcount
        if claimed != 1:
            continue
        call = Call(campaign_lead_id=cl.id, status="queued")
        db.add(call)
        db.commit()
        try:
            call.provider_call_id = provider.place_call(from_number=caller, to_number=lead.phone, call_id=call.id)
            call.status = "initiated"
            db.commit()
        except TunnelNotReady as e:      # not the lead's fault: wait 30s and don't use up an attempt
            log.warning("%s - waiting for the tunnel address to go live; retrying in 30s", e)
            cl_now = db.get(CampaignLead, call.campaign_lead_id)
            db.refresh(cl_now)
            cl_now.status, cl_now.attempts = "pending", max(0, cl_now.attempts - 1)
            cl_now.next_attempt_at = now + timedelta(seconds=30)
            db.delete(call)
            db.commit()
            break
        except Exception:
            log.exception("could not place call %s", call.id)
            call.status = "place_failed"
            finish_attempt(db, call, "failed", now)
        placed += 1


def tick(now: datetime | None = None):
    now = now or _now()
    provider = get_provider()
    with SessionLocal() as db:
        _reap_stale(db, now)
        for c in db.scalars(select(Campaign).where(Campaign.status == "running")).all():
            _dial_campaign(db, c, provider, now)
            maybe_complete(db, c)


async def dialer_loop():
    log.info("dialer started (provider=%s)", settings.provider)
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception:
            log.exception("dialer tick failed")
        await asyncio.sleep(settings.dialer_interval)
