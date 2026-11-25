import asyncio
import hmac
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import settings
from ..db import SessionLocal
from ..models import Call, CampaignLead, Lead
from .clients import RealtimeSTT, SarvamLLM, SarvamTTS
from .. import runtime_settings
from .session import CallSession
from .tools_bridge import CallContext

log = logging.getLogger("voice")
router = APIRouter()


def load_context(call_id: int) -> CallContext:
    with SessionLocal() as db:
        call = db.get(Call, call_id)
        cl = db.get(CampaignLead, call.campaign_lead_id) if call else None
        lead = db.get(Lead, cl.lead_id) if cl else None
        return CallContext(call_id=call_id, lead_name=(lead.name or "") if lead else "",
                           company=(lead.company or "") if lead else "", phone=lead.phone if lead else "",
                           interaction_id=f"call-{call_id}")


@router.websocket("/voice/ws")
async def voice_ws(ws: WebSocket, call_id: int, token: str = ""):
    if not hmac.compare_digest(token, settings.webhook_secret):
        await ws.close(code=4403)
        return
    await ws.accept()
    ctx = await asyncio.to_thread(load_context, call_id)
    cfg = await asyncio.to_thread(runtime_settings.load)
    session = CallSession(ws.send_text, ws.close, ctx, stt_factory=RealtimeSTT, llm=SarvamLLM(), tts=SarvamTTS(), cfg=cfg)
    try:
        async for message in ws.iter_text():
            await session.on_plivo_message(message)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("voice socket failed")
    finally:
        await session.finish()
