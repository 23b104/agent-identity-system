import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import verify_admin
from app import crud
from app.models import Agent, AgentStatus
from app.schemas import (
    AgentRegisterRequest, RegisterResponse, AgentIdentityResponse,
    RotateResponse, CredentialIssuedResponse,
)

log = logging.getLogger("agent_identity")
router = APIRouter(prefix="/agents", tags=["agents"])


def _to_identity(agent: Agent) -> AgentIdentityResponse:
    return AgentIdentityResponse(
        agent_id=agent.agent_id, name=agent.name, purpose=agent.purpose,
        owning_team=agent.owning_team, scopes=agent.scopes.split(","),
        status=agent.status.value, created_at=agent.created_at,
        expiry_date=agent.expiry_date, last_active_at=agent.last_active_at,
    )


@router.post("/register", response_model=RegisterResponse, dependencies=[Depends(verify_admin)])
def register(req: AgentRegisterRequest, db: Session = Depends(get_db)):
    agent, token, cred = crud.register_agent(
        db, req.name, req.purpose, req.owning_team, req.requested_scopes, req.ttl_days
    )
    log.info("agent_registered agent_id=%s team=%s scopes=%s", agent.agent_id, agent.owning_team, agent.scopes)
    return RegisterResponse(
        identity=_to_identity(agent),
        credential=CredentialIssuedResponse(
            agent_id=agent.agent_id, credential=token, credential_id=cred.credential_id,
            expires_at=cred.expires_at, scopes=agent.scopes.split(","),
        ),
    )


@router.get("", response_model=list[AgentIdentityResponse], dependencies=[Depends(verify_admin)])
def list_agents(db: Session = Depends(get_db)):
    return [_to_identity(a) for a in db.query(Agent).order_by(Agent.created_at.desc()).all()]


@router.get("/{agent_id}", response_model=AgentIdentityResponse, dependencies=[Depends(verify_admin)])
def get_agent(agent_id: str, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.agent_id == agent_id).first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    return _to_identity(agent)


@router.post("/{agent_id}/rotate", response_model=RotateResponse, dependencies=[Depends(verify_admin)])
def rotate(agent_id: str, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.agent_id == agent_id).first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    if agent.status != AgentStatus.active:
        raise HTTPException(400, f"Cannot rotate credential for a {agent.status.value} agent")
    token, new_cred, old = crud.rotate_credential(db, agent)
    log.info("credential_rotated agent_id=%s old_count=%d", agent_id, len(old))
    return RotateResponse(
        credential=CredentialIssuedResponse(
            agent_id=agent.agent_id, credential=token, credential_id=new_cred.credential_id,
            expires_at=new_cred.expires_at, scopes=agent.scopes.split(","),
        ),
        revoked_credential_id=",".join(c.credential_id for c in old) if old else "none",
    )


@router.post("/{agent_id}/suspend", response_model=AgentIdentityResponse, dependencies=[Depends(verify_admin)])
def suspend(agent_id: str, reason: str = "manual admin suspension", db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.agent_id == agent_id).first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    agent = crud.suspend_agent(db, agent, actor="admin", reason=reason)
    log.warning("agent_suspended agent_id=%s reason=%s", agent_id, reason)
    return _to_identity(agent)
