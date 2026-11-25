from datetime import datetime, timezone
from fastapi.testclient import TestClient
from app.main import app
from app.dialer import tick
from app.scheduling import window_status
from app.config import settings

client = TestClient(app)
from app.admin import make_token
client.headers["Authorization"] = "Bearer " + make_token()
NOON_IST = datetime(2026, 11, 20, 6, 30, tzinfo=timezone.utc)   # 12:00 in Asia/Kolkata
LATER = datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc)      # 3h later
EARLY = datetime(2026, 11, 20, 2, 0, tzinfo=timezone.utc)       # 07:30 IST, before 10:00 window


def setup_campaign(n=3, **kw):
    cid = client.post("/campaigns", json={"name": "t", **kw}).json()["id"]
    csv = "name,phone\n" + "\n".join(f"L{i},98765432{i:02d}" for i in range(n))
    client.post(f"/campaigns/{cid}/leads/upload", files={"file": ("l.csv", csv.encode())})
    client.post(f"/campaigns/{cid}/start")
    return cid


def calls(cid):
    return client.get(f"/campaigns/{cid}/calls").json()


def counts(cid):
    return client.get(f"/campaigns/{cid}").json()["lead_counts"]


def test_window_status():
    assert window_status(NOON_IST, "Asia/Kolkata", "10:00", "20:00") == (True, None)
    ok, nxt = window_status(EARLY, "Asia/Kolkata", "10:00", "20:00")
    assert not ok and nxt == datetime(2026, 11, 20, 4, 30, tzinfo=timezone.utc)
    ok, nxt = window_status(datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc), "Asia/Kolkata", "10:00", "20:00")
    assert not ok and nxt.day == 21


def test_respects_parallel_limit_and_retries(monkeypatch):
    # the hangup webhooks stamp retry times with the dialer clock, so pin it to the same fake time as tick()
    clock = {"now": NOON_IST}
    monkeypatch.setattr("app.dialer._now", lambda: clock["now"])
    cid = setup_campaign(3, max_parallel=2, max_attempts=2, retry_gap_minutes=120)
    tick(NOON_IST)
    assert len(calls(cid)) == 2 and counts(cid)["in_progress"] == 2
    tick(NOON_IST)                                   # still at capacity
    assert len(calls(cid)) == 2
    first, second = sorted(c["call_id"] for c in calls(cid))
    client.post(f"/dev/calls/{first}/simulate?status=completed")
    client.post(f"/dev/calls/{second}/simulate?status=busy")
    tick(NOON_IST)                                   # frees a slot -> third lead; busy lead waits for retry gap
    assert len(calls(cid)) == 3
    third = max(c["call_id"] for c in calls(cid))
    client.post(f"/dev/calls/{third}/simulate?status=completed")
    tick(NOON_IST)
    assert len(calls(cid)) == 3                      # busy lead not retried yet
    clock["now"] = LATER
    tick(LATER)                                      # retry gap passed
    assert len(calls(cid)) == 4
    last = max(c["call_id"] for c in calls(cid))
    client.post(f"/dev/calls/{last}/simulate?status=no-answer")   # attempt 2 of 2 -> exhausted
    c = client.get(f"/campaigns/{cid}").json()
    assert c["lead_counts"] == {"done": 2, "failed": 1} and c["status"] == "completed"


def test_outside_window_and_dnc():
    cid = setup_campaign(2)
    client.post("/dnc", json={"phone": "9876543200"})
    tick(EARLY)
    assert calls(cid) == []                          # outside window: nothing dialled
    tick(NOON_IST)                                   # (next_attempt_at is 10:00 IST = 04:30 UTC)
    got = calls(cid)
    assert len(got) == 1 and got[0]["phone"] == "+919876543201"
    assert counts(cid)["skipped"] == 1


def test_webhooks_need_token_and_log_calls():
    cid = setup_campaign(1)
    tick(NOON_IST)
    call_id = calls(cid)[0]["call_id"]
    assert client.post(f"/plivo/hangup?call_id={call_id}&token=nope", data={"CallStatus": "completed"}).status_code == 403
    r = client.post(f"/plivo/answer?call_id={call_id}&token={settings.webhook_secret}", data={"CallUUID": "abc"})
    assert r.status_code == 200 and "<Speak>" in r.text
    client.post(f"/plivo/hangup?call_id={call_id}&token={settings.webhook_secret}",
                data={"CallStatus": "completed", "Duration": "42", "CallUUID": "abc"})
    c = calls(cid)[0]
    assert c["outcome"] == "answered" and c["duration_sec"] == 42 and c["started_at"]
    client.post(f"/plivo/hangup?call_id={call_id}&token={settings.webhook_secret}", data={"CallStatus": "busy"})
    assert calls(cid)[0]["outcome"] == "answered"    # duplicate webhook ignored


def test_tunnel_not_ready_waits_without_burning_an_attempt(monkeypatch):
    from app.providers import TunnelNotReady

    class Flaky:
        n = 0

        def place_call(self, **kw):
            Flaky.n += 1
            if Flaky.n == 1:
                raise TunnelNotReady("x.trycloudflare.com is not visible on public DNS yet")
            return "uuid-1"

    monkeypatch.setattr("app.dialer.get_provider", lambda: Flaky())
    cid = setup_campaign(1, max_attempts=1)
    tick(NOON_IST)
    assert calls(cid) == [] and Flaky.n == 1          # no call row, nothing wasted
    tick(NOON_IST)                                     # still inside the 30 s wait
    assert Flaky.n == 1
    tick(LATER)
    assert Flaky.n == 2 and len(calls(cid)) == 1


def test_tunnel_ready_checks_dns_and_gives_up_gracefully(monkeypatch):
    import app.providers as P
    state = {"answer": False}

    class R:
        def json(self):
            return {"Status": 0, "Answer": [{"data": "1.2.3.4"}] if state["answer"] else None}

    monkeypatch.setattr("httpx.get", lambda *a, **k: R())
    P._ready_hosts.clear(); P._first_miss.clear()
    assert P.tunnel_ready("x.trycloudflare.com") is False        # not visible yet
    monkeypatch.setattr(P.time, "time", lambda t0=P.time.time(): t0 + 200)
    assert P.tunnel_ready("x.trycloudflare.com") is True         # waited long enough: let Plivo decide
    P._first_miss.clear(); state["answer"] = True
    assert P.tunnel_ready("y.trycloudflare.com") is True and "y.trycloudflare.com" in P._ready_hosts
    monkeypatch.setattr("httpx.get", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    assert P.tunnel_ready("z.trycloudflare.com") is True         # check itself failed: don't block calls
