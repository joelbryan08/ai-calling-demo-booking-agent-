from fastapi.testclient import TestClient

from app.admin import make_token
from app.main import app

client = TestClient(app)


def test_login_and_guard():
    assert client.get("/campaigns").status_code == 401
    assert client.get("/api/overview").status_code == 401
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    tok = client.post("/api/login", json={"password": "test-admin"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get("/campaigns", headers=h).status_code == 200
    ov = client.get("/api/overview", headers=h).json()
    assert ov["leads"] == 0 and ov["upcoming"] == [] and client.get("/api/demos", headers=h).json() == []
    assert client.get("/api/calls/99", headers=h).status_code == 404
    assert client.get("/api/dnc", headers=h).json() == []
    assert client.get("/campaigns", headers={"Authorization": "Bearer 1.abc"}).status_code == 401
    assert client.get("/ui/").status_code == 200 and client.get("/tools/dev/fake_busy").status_code != 200
    assert make_token().count(".") == 1


def test_settings_charts_live_and_prompt_override():
    from app.voice.prompt import greeting, system_prompt
    from app.voice.session import LIVE
    from datetime import date
    h = {"Authorization": "Bearer " + make_token()}
    r = client.put("/api/settings", headers=h, json={"agent_name": "Riya", "opening_line": "Hello from Riya!", "extra_rules": "Never mention competitors."})
    assert r.json()["values"]["agent_name"] == "Riya"
    cfg = client.get("/api/settings", headers=h).json()["values"]
    assert greeting(cfg) == "Hello from Riya!" and "You are Riya" in system_prompt("A", "B", date(2026, 11, 23), cfg)
    assert "Never mention competitors." in system_prompt("A", "B", date(2026, 11, 23), cfg)
    client.put("/api/settings", headers=h, json={"agent_name": "", "opening_line": "", "extra_rules": ""})
    assert client.get("/api/settings", headers=h).json()["values"]["agent_name"] == "Diya"      # blank = default again
    ov = client.get("/api/overview", headers=h).json()
    assert len(ov["daily"]) == 14 and ov["by_presenter"] == {}
    assert client.get("/api/live", headers=h).json() == [] and not LIVE


def test_call_detail_shows_summary_and_recording():
    from pathlib import Path
    from app.config import settings
    from app.db import SessionLocal
    from app.models import Call, CallInsight, Campaign, CampaignLead, Lead
    with SessionLocal() as db:
        camp, lead = Campaign(name="t"), Lead(phone="+919800000001", name="Asha")
        db.add_all([camp, lead]); db.flush()
        cl = CampaignLead(campaign_id=camp.id, lead_id=lead.id); db.add(cl); db.flush()
        call = Call(campaign_lead_id=cl.id, transcript="Agent: hi\nCaller: hello"); db.add(call); db.flush()
        db.add(CallInsight(call_id=call.id, summary="Wants a demo.", sentiment="positive", pain_points="churn", next_step="book"))
        db.commit(); cid = call.id
    Path(settings.recordings_dir).mkdir(exist_ok=True)
    (Path(settings.recordings_dir) / f"call-{cid}.wav").write_bytes(b"RIFFxxxx")
    h = {"Authorization": "Bearer " + make_token()}
    d = client.get(f"/api/calls/{cid}", headers=h).json()
    assert d["summary"] == "Wants a demo." and d["has_recording"] is True and d["sentiment"] == "positive"
    r = client.get(f"/api/calls/{cid}/recording", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert client.get(f"/api/calls/{cid}/recording").status_code == 401
    assert client.get("/api/calls/9999/recording", headers=h).status_code == 404
