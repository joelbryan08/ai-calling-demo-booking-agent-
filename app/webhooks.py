import hmac
import os
from xml.sax.saxutils import escape
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from .config import settings
from .db import get_db
from .dialer import apply_hangup, _now
from .models import Call

router = APIRouter()

# Step 3 replaces this with a <Stream> to the Sarvam voice agent.
PLACEHOLDER_XML = ('<Response><Speak>Hello, this is a test call from the demo booking agent. '
                   'Goodbye.</Speak></Response>')


def _check(token: str):
    if not hmac.compare_digest(token or "", settings.webhook_secret):
        raise HTTPException(403, "bad token")


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@router.api_route("/plivo/answer", methods=["GET", "POST"])
async def plivo_answer(request: Request, call_id: int, token: str = "", db=Depends(get_db)):
    _check(token)
    form = await request.form()
    call = db.get(Call, call_id)
    if call and not call.ended_at:
        call.status, call.started_at = "answered", _now()
        call.provider_call_id = call.provider_call_id or form.get("CallUUID")
        db.commit()
    if os.getenv("VOICE_MODE", "test_message").lower() == "stream":
        ws_base = settings.public_base_url.replace("https://", "wss://").replace("http://", "ws://")
        url = escape(f"{ws_base}/voice/ws?call_id={call_id}&token={settings.webhook_secret}")
        xml = ('<Response><Stream bidirectional="true" keepCallAlive="true" '
               f'contentType="audio/x-mulaw;rate=8000">{url}</Stream><Hangup/></Response>')
        return Response(xml, media_type="application/xml")
    return Response(PLACEHOLDER_XML, media_type="application/xml")


@router.post("/plivo/hangup")
async def plivo_hangup(request: Request, call_id: int, token: str = "", db=Depends(get_db)):
    _check(token)
    form = await request.form()
    apply_hangup(db, call_id, form.get("CallStatus", "failed"), _int(form.get("Duration")), form.get("CallUUID"))
    return {"ok": True}
