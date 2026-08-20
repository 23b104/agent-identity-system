from datetime import timedelta
from typing import List
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Agent, Credential, AgentStatus, AuditEvent, _now
from app.auth import create_agent_credential


def register_agent(db: Session, name: str, purpose: str, owning_team: str,
                    requested_scopes: List[str], ttl_days: int | None):
    ttl = ttl_days or settings.DEFAULT_CREDENTIAL_TTL_DAYS
    now = _now()
    agent = Agent(
        name=name,
        purpose=purpose,
        owning_team=owning_team,
        scopes=",".join(requested_scopes),
        status=AgentStatus.active,
        created_at=now,
        expiry_date=now + timedelta(days=ttl),
    )
    db.add(agent)
    db.flush()

    token, cred = create_agent_credential(db, agent, ttl)

    db.add(AuditEvent(
        agent_id=agent.agent_id,
        event_type="created",
        actor="admin",
        detail=f"Registered with scopes={agent.scopes}, ttl_days={ttl}",
    ))
    db.commit()
    db.refresh(agent)
    return agent, token, cred


def rotate_credential(db: Session, agent: Agent, ttl_days: int | None = None):
    ttl = ttl_days or settings.DEFAULT_CREDENTIAL_TTL_DAYS
    now = _now()

    old_active = (
        db.query(Credential)
        .filter(Credential.agent_id == agent.agent_id, Credential.revoked == False)  # noqa: E712
        .all()
    )
    for c in old_active:
        c.revoked = True
        c.revoked_at = now
        c.revoked_reason = "rotated"

    agent.expiry_date = now + timedelta(days=ttl)
    token, new_cred = create_agent_credential(db, agent, ttl)

    db.add(AuditEvent(
        agent_id=agent.agent_id,
        event_type="rotated",
        actor="admin",
        detail=f"Rotated; {len(old_active)} old credential(s) revoked.",
    ))
    db.commit()
    db.refresh(agent)
    return token, new_cred, old_active


def suspend_agent(db: Session, agent: Agent, actor: str, reason: str):
    agent.status = AgentStatus.suspended
    for c in db.query(Credential).filter(Credential.agent_id == agent.agent_id, Credential.revoked == False).all():  # noqa: E712
        c.revoked = True
        c.revoked_at = _now()
        c.revoked_reason = reason
    db.add(AuditEvent(agent_id=agent.agent_id, event_type="suspended", actor=actor, detail=reason))
    db.commit()
    return agent
