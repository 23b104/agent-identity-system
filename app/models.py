import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, DateTime, Enum, ForeignKey, Text, Boolean, Integer
)
from sqlalchemy.orm import relationship
from app.database import Base


def _now():
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    """
    SQLite does not persist tzinfo, so datetimes read back from the DB are
    naive even though they were stored as UTC. This normalizes any datetime
    (naive or aware) to an aware UTC datetime so comparisons never raise
    'can't compare offset-naive and offset-aware datetimes'. Postgres in
    production preserves tzinfo natively, so this is a safe no-op there.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def gen_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class AgentStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"
    decommissioned = "decommissioned"


class Agent(Base):
    """
    The identity record itself — the machine-identity analogue of a human
    user account row. One row per agent, regardless of how many credentials
    it has rotated through.
    """
    __tablename__ = "agents"

    agent_id = Column(String, primary_key=True, default=lambda: gen_id("agent"))
    name = Column(String, nullable=False)
    purpose = Column(Text, nullable=False)
    owning_team = Column(String, nullable=False, index=True)
    scopes = Column(String, nullable=False)  # comma-separated, e.g. "read,write"
    status = Column(Enum(AgentStatus), default=AgentStatus.active, nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    expiry_date = Column(DateTime(timezone=True), nullable=False)
    last_active_at = Column(DateTime(timezone=True), nullable=True)

    # audit trail for every status change (human or AI-initiated)
    events = relationship("AuditEvent", back_populates="agent", cascade="all, delete-orphan")
    credentials = relationship("Credential", back_populates="agent", cascade="all, delete-orphan")


class Credential(Base):
    """
    A single issued credential (JWT) for an agent. Rotation revokes the old
    row and creates a new one, so history of every credential ever issued
    is preserved for audit purposes.
    """
    __tablename__ = "credentials"

    credential_id = Column(String, primary_key=True, default=lambda: gen_id("cred"))
    agent_id = Column(String, ForeignKey("agents.agent_id"), nullable=False, index=True)
    jti = Column(String, unique=True, nullable=False)  # JWT ID, embedded in the token
    issued_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, default=False, nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_reason = Column(String, nullable=True)

    agent = relationship("Agent", back_populates="credentials")


class AIReviewRun(Base):
    """
    One row per autonomous review run (scheduled or manually triggered).
    Stores the full tool-call transcript so a human can audit exactly what
    the agent looked at and why it decided what it decided — this is also
    what gives the agent MEMORY: future runs query this table to see their
    own past reasoning about a given agent before deciding again.
    """
    __tablename__ = "ai_review_runs"

    run_id = Column(String, primary_key=True, default=lambda: gen_id("run"))
    triggered_by = Column(String, nullable=False)  # "scheduler" | "admin:manual"
    model = Column(String, nullable=False)
    started_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String, default="running", nullable=False)  # running|completed|failed
    error = Column(Text, nullable=True)
    transcript = Column(Text, nullable=True)  # JSON: full list of tool calls + reasoning + results
    summary = Column(Text, nullable=True)     # JSON: per-agent final decisions


class AuditEvent(Base):
    """
    Append-only audit log. Every provisioning, rotation, suspension,
    auto-revoke, and AI-initiated action is recorded here with an actor
    field so you can always answer 'who/what did this and why'.
    """
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(String, ForeignKey("agents.agent_id"), nullable=False, index=True)
    event_type = Column(String, nullable=False)  # e.g. "created", "rotated", "suspended", "auto_revoked"
    actor = Column(String, nullable=False)  # "admin", "system:auto-revoke", "ai-reviewer:llama-3.3-70b"
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    agent = relationship("Agent", back_populates="events")
