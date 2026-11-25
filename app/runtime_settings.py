from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import Setting

KEYS = ("agent_name", "company_name", "product_name", "problem_area", "product_pitch", "opening_line", "extra_rules")


def defaults() -> dict:
    s = settings
    return {"agent_name": s.agent_name, "company_name": s.company_name, "product_name": s.product_name,
            "problem_area": s.problem_area, "product_pitch": s.product_pitch, "opening_line": "", "extra_rules": ""}


def load() -> dict:
    cfg = defaults()
    with SessionLocal() as db:
        for row in db.scalars(select(Setting)):
            if row.key in cfg and row.value.strip():     # blank = fall back to the default
                cfg[row.key] = row.value.strip()
    return cfg


def save(values: dict):
    with SessionLocal() as db:
        for k in KEYS:
            if k in values:
                row = db.get(Setting, k)
                if row:
                    row.value = str(values[k])[:2000]
                else:
                    db.add(Setting(key=k, value=str(values[k])[:2000]))
        db.commit()
