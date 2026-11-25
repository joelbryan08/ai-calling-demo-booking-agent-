"""Runs the agent's tool calls directly against our own scheduling code (no HTTP hop)."""
import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

from .. import tools as T
from ..config import settings
from ..db import SessionLocal
from ..models import Lead
from sqlalchemy import select

log = logging.getLogger("voice")


@dataclass
class CallContext:
    call_id: int | None = None
    lead_name: str = ""
    company: str = ""
    phone: str = ""
    interaction_id: str = ""
    demo_time: str | None = None


def _sync(name: str, args: dict, ctx: CallContext) -> dict:
    tz = settings.presenter_timezone
    with SessionLocal() as db:
        if name == "check_availability":
            today = T._now().astimezone(ZoneInfo(tz)).date()
            first = args.get("preferred_date") or today.isoformat()
            body = T.CheckIn(start_date=first, timezone=tz, time_of_day=args.get("time_of_day") or "any",
                             interaction_id=ctx.interaction_id or None)
            return T.check_availability(body, db)
        if name == "book_demo":
            body = T.BookIn(slot_start=args.get("slot_start", ""), timezone=tz,
                            prospect_name=ctx.lead_name or "Prospect", prospect_email=None,
                            company=ctx.company or None, phone=ctx.phone or None,
                            interaction_id=ctx.interaction_id or None)
            return T.book_demo(body, db)
        if name == "log_call":
            wa, wa_number = args.get("whatsapp"), (args.get("whatsapp_number") or "").strip()
            wa_note = {"yes": "WhatsApp: confirmed on this number", "no": "WhatsApp: not on this number",
                       "different_number": f"WhatsApp: use {wa_number or 'a different number (not captured)'}"}.get(wa)
            notes = " | ".join(x for x in [(args.get("notes") or "").strip(), wa_note] if x) or None
            body = T.LogIn(phone=ctx.phone or "unknown", outcome=args.get("outcome") or "callback",
                           prospect_name=ctx.lead_name or None, company=ctx.company or None, demo_time=ctx.demo_time,
                           pain_points=args.get("pain_points") or None, notes=notes,
                           interaction_id=ctx.interaction_id or None)
            result = T.log_call(body, db)
            if wa == "yes" and ctx.phone:     # their yes to "we'll message you there" doubles as WhatsApp opt-in
                lead = db.scalar(select(Lead).where(Lead.phone == ctx.phone))
                if lead:
                    lead.whatsapp_consent = True
                    db.commit()
            return result
    return {"status": "error", "message": f"unknown tool {name}"}


async def run_tool(name: str, args: dict, ctx: CallContext) -> dict:
    try:
        return await asyncio.to_thread(_sync, name, args, ctx)
    except ValueError as e:   # pydantic validation: tell the model what to fix, in plain words
        log.info("tool %s rejected arguments: %s", name, e)
        hint = {"book_demo": "slot_start must look like 2026-11-24T15:00:00+05:30. Fix it and try again.",
                "check_availability": "preferred_date must be YYYY-MM-DD."}.get(name, "Invalid arguments.")
        return {"status": "error", "message": hint}
    except Exception:
        log.exception("tool %s failed", name)
        return {"status": "error", "message": "That didn't work. Offer to follow up by email instead."}
