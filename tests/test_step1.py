import os
os.environ["DATABASE_URL"] = "sqlite:///./test.db"
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
from app.admin import make_token
client.headers["Authorization"] = "Bearer " + make_token()
CSV = (b"name,mobile,company,email,timezone\n"
       b"Asha,98765 43210,Acme,a@acme.com,Asia/Kolkata\n"
       b"Ravi,+919812345678,Beta,,\n"
       b"Dup,9876543210,Acme,,\n"
       b"Bad,12345,X,,\n"
       b"BadTZ,9811111111,Y,,Mars/Base\n"
       b"Blocked,9822222222,Z,,\n")


def test_flow():
    client.post("/dnc", json={"phone": "9822222222", "reason": "opted out"})
    cid = client.post("/campaigns", json={"name": "Test"}).json()["id"]
    assert client.post(f"/campaigns/{cid}/start").status_code == 409  # no leads yet
    r = client.post(f"/campaigns/{cid}/leads/upload", files={"file": ("l.csv", CSV)}).json()
    assert r["added"] == 2 and r["rejected"] == 2 and r["duplicates_in_file"] == 1 and r["skipped_dnc"] == 1
    r2 = client.post(f"/campaigns/{cid}/leads/upload", files={"file": ("l.csv", CSV)}).json()
    assert r2["added"] == 0 and r2["already_in_campaign"] == 2
    assert client.post(f"/campaigns/{cid}/start").json()["status"] == "running"
    assert client.post(f"/campaigns/{cid}/leads/upload", files={"file": ("l.csv", CSV)}).status_code == 409
    assert client.post(f"/campaigns/{cid}/pause").json()["status"] == "paused"
    assert client.post(f"/campaigns/{cid}/cancel").json()["status"] == "cancelled"
    assert client.get(f"/campaigns/{cid}").json()["total_leads"] == 2
