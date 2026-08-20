from collections import defaultdict
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Agent, Credential, AgentStatus, AuditEvent, _now, as_utc
from app.schemas import ReviewFlag, QuarterlyReviewReport


def run_quarterly_review(db: Session) -> QuarterlyReviewReport:
    now = _now()
    auto_revoked = []

    # 1. Auto-revoke sweep: any active agent whose expiry has passed.
    active_agents = db.query(Agent).filter(Agent.status == AgentStatus.active).all()
    for agent in active_agents:
        if as_utc(agent.expiry_date) <= now:
            agent.status = AgentStatus.decommissioned
            for c in db.query(Credential).filter(
                Credential.agent_id == agent.agent_id, Credential.revoked == False  # noqa: E712
            ).all():
                c.revoked = True
                c.revoked_at = now
                c.revoked_reason = "auto-revoke: expiry passed (quarterly sweep)"
            db.add(AuditEvent(
                agent_id=agent.agent_id, event_type="auto_revoked",
                actor="system:quarterly-review", detail="Expiry passed without renewal.",
            ))
            auto_revoked.append(ReviewFlag(
                agent_id=agent.agent_id, name=agent.name, owning_team=agent.owning_team,
                reason="expiry passed — auto-revoked", last_active_at=agent.last_active_at,
                days_inactive=(now - as_utc(agent.last_active_at)).days if agent.last_active_at else None,
            ))
    db.commit()

    # 2. Recompute active set after the sweep, flag staleness.
    still_active = db.query(Agent).filter(Agent.status == AgentStatus.active).all()
    stale = []
    by_team = defaultdict(lambda: {"total": 0, "stale": 0})

    for agent in still_active:
        by_team[agent.owning_team]["total"] += 1
        if agent.last_active_at is None:
            days_inactive = (now - as_utc(agent.created_at)).days
        else:
            days_inactive = (now - as_utc(agent.last_active_at)).days

        if days_inactive >= settings.STALE_THRESHOLD_DAYS:
            by_team[agent.owning_team]["stale"] += 1
            stale.append(ReviewFlag(
                agent_id=agent.agent_id, name=agent.name, owning_team=agent.owning_team,
                reason=f"no API call in {days_inactive} days (threshold: {settings.STALE_THRESHOLD_DAYS})",
                last_active_at=agent.last_active_at, days_inactive=days_inactive,
            ))
            db.add(AuditEvent(
                agent_id=agent.agent_id, event_type="flagged_stale",
                actor="system:quarterly-review", detail=f"{days_inactive} days inactive",
            ))
    db.commit()

    return QuarterlyReviewReport(
        generated_at=now,
        total_active_agents=len(still_active),
        stale_agents=stale,
        auto_revoked_this_run=auto_revoked,
        by_team=dict(by_team),
    )
