"""
This module is the enforcement point. Two separate trust boundaries live here:

1. Admin auth (X-Admin-Key header) — gates provisioning/suspension actions,
   the way an IAM admin console would. Swap for real SSO/OIDC in production
   (see README bonus section for Okta/Auth0 wiring).

2. Agent credential auth (Bearer JWT) — every agent call must present a
   scoped, time-bounded JWT. Verification checks, in order:
     - signature valid
     - token not expired (JWT `exp`)
     - credential not revoked (DB lookup by `jti`)
     - owning identity record not expired/suspended/decommissioned (auto-revoke)
     - requested scope is present in the token's scope claim
   Any failure -> 401/403. This is what makes scope enforcement real rather
   than cosmetic: a read-only agent's token simply does not contain the
   "write" scope, so no amount of client-side trickery grants it.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import Depends, Header, HTTPException, status
from jose import jwt, JWTError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Agent, Credential, AgentStatus, AuditEvent, _now, as_utc


def create_agent_credential(db: Session, agent: Agent, ttl_days: int) -> tuple[str, Credential]:
    now = _now()
    expires_at = now + timedelta(days=ttl_days)
    jti = uuid.uuid4().hex

    payload = {
        "sub": agent.agent_id,
        "scopes": agent.scopes,  # comma-separated string, e.g. "read,write"
        "team": agent.owning_team,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": jti,
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

    cred = Credential(
        agent_id=agent.agent_id,
        jti=jti,
        issued_at=now,
        expires_at=expires_at,
        revoked=False,
    )
    db.add(cred)
    db.flush()
    return token, cred


def verify_admin(x_admin_key: str = Header(..., alias="X-Admin-Key")):
    if x_admin_key != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    return True


class ScopeChecker:
    """FastAPI dependency factory: Depends(ScopeChecker('write'))"""

    def __init__(self, required_scope: str):
        self.required_scope = required_scope

    def __call__(
        self,
        authorization: str = Header(..., description="Bearer <agent JWT>"),
        db: Session = Depends(get_db),
    ) -> Agent:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Expected 'Bearer <token>' Authorization header")
        token = authorization.removeprefix("Bearer ").strip()

        try:
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        except JWTError as e:
            raise HTTPException(status_code=401, detail=f"Invalid or expired token: {e}")

        jti = payload.get("jti")
        agent_id = payload.get("sub")

        cred = db.query(Credential).filter(Credential.jti == jti).first()
        if cred is None or cred.revoked:
            raise HTTPException(status_code=401, detail="Credential has been revoked")

        agent = db.query(Agent).filter(Agent.agent_id == agent_id).first()
        if agent is None:
            raise HTTPException(status_code=401, detail="Unknown agent")

        # --- auto-revoke enforcement at call time ---
        now = _now()
        if as_utc(agent.expiry_date) <= now and agent.status == AgentStatus.active:
            agent.status = AgentStatus.decommissioned
            cred.revoked = True
            cred.revoked_at = now
            cred.revoked_reason = "auto-revoke: identity expiry passed"
            db.add(AuditEvent(
                agent_id=agent.agent_id,
                event_type="auto_revoked",
                actor="system:auto-revoke",
                detail="Expiry date passed without renewal; credential invalidated at call time.",
            ))
            db.commit()
            raise HTTPException(status_code=403, detail="Agent identity expired — auto-revoked. Rotate/renew required.")

        if agent.status != AgentStatus.active:
            raise HTTPException(status_code=403, detail=f"Agent is {agent.status.value}, not active")

        scopes: List[str] = [s.strip() for s in (payload.get("scopes") or "").split(",") if s.strip()]
        if self.required_scope not in scopes:
            raise HTTPException(
                status_code=403,
                detail=f"Credential lacks required scope '{self.required_scope}'. Granted scopes: {scopes}",
            )

        agent.last_active_at = now
        db.commit()
        return agent
