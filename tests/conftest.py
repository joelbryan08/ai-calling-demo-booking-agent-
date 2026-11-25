import os
os.environ["DATABASE_URL"] = "sqlite:///./test.db"
os.environ["TELEPHONY_PROVIDER"] = "fake"
os.environ["DIALER_ENABLED"] = "false"
os.environ["DEFAULT_CALLER_NUMBER"] = "+911400000000"
os.environ["CALENDAR_BACKEND"] = "fake"
os.environ["TOOLS_API_KEY"] = "test-key"
os.environ["ADMIN_PASSWORD"] = "test-admin"
os.environ["PRESENTERS"] = "a@co.com,b@co.com,c@co.com"
os.environ["FAKE_SHEET_CSV"] = ""
os.environ["RECORDINGS_DIR"] = "test_recordings"
os.environ["AGENT_NAME"] = "Diya"
os.environ["COMPANY_NAME"] = "Dexkor"
os.environ["PROBLEM_AREA"] = "customer churn and support"

import pytest
from app.db import engine
from app.models import Base
from app.google_clients import reset_fakes


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    reset_fakes()
    from app.voice.session import LIVE
    LIVE.clear()
    yield
    import shutil
    shutil.rmtree("test_recordings", ignore_errors=True)
