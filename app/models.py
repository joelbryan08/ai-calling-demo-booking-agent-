from datetime import datetime, timezone
from sqlalchemy import String, Integer, Boolean, ForeignKey, DateTime, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft/running/paused/completed/cancelled
    caller_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    window_start: Mapped[str] = mapped_column(String(5), default="10:00")  # lead-local time
    window_end: Mapped[str] = mapped_column(String(5), default="20:00")
    default_timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    max_parallel: Mapped[int] = mapped_column(Integer, default=2)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    retry_gap_minutes: Mapped[int] = mapped_column(Integer, default=120)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # E.164
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    whatsapp_consent: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CampaignLead(Base):
    __tablename__ = "campaign_leads"
    __table_args__ = (UniqueConstraint("campaign_id", "lead_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending/in_progress/done/failed/skipped
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)


class Call(Base):  # filled in by the dialer in the next step
    __tablename__ = "calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_lead_id: Mapped[int] = mapped_column(ForeignKey("campaign_leads.id"), index=True)
    provider_call_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    booking_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DoNotCall(Base):
    __tablename__ = "do_not_call"
    phone: Mapped[str] = mapped_column(String(20), primary_key=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (UniqueConstraint("presenter_email", "start_utc"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    presenter_email: Mapped[str] = mapped_column(String(200), index=True)
    start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    prospect_name: Mapped[str] = mapped_column(String(200))
    prospect_email: Mapped[str] = mapped_column(String(200), index=True)
    prospect_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    prospect_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    meet_link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    html_link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    interaction_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="booked")  # booked / cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CallLog(Base):
    __tablename__ = "call_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    interaction_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    phone: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(30))
    prospect_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    demo_time: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pain_points: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sheet_status: Mapped[str] = mapped_column(String(10), default="pending")  # ok / failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Offer(Base):
    """Slots the voice agent offered during one call, so the end-of-call hook can book the one the caller chose."""
    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    interaction_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    slots_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    """Script/settings edited from the dashboard; they override the .env defaults and apply to the next call."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class CallInsight(Base):
    """AI summary of one call, written after the call ends."""
    __tablename__ = "call_insights"
    call_id: Mapped[int] = mapped_column(primary_key=True)
    summary: Mapped[str] = mapped_column(Text)
    sentiment: Mapped[str] = mapped_column(String(10), default="neutral")
    pain_points: Mapped[str] = mapped_column(Text, default="")
    next_step: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
