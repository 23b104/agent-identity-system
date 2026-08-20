from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


class AgentRegisterRequest(BaseModel):
    name: str = Field(..., examples=["invoice-reconciler-bot"])
    purpose: str = Field(..., examples=["Reconciles vendor invoices against POs nightly"])
    owning_team: str = Field(..., examples=["finance-ops"])
    requested_scopes: List[str] = Field(..., examples=[["read:invoices", "write:reconciliation_log"]])
    ttl_days: Optional[int] = Field(None, description="Override default credential lifetime")


class AgentIdentityResponse(BaseModel):
    agent_id: str
    name: str
    purpose: str
    owning_team: str
    scopes: List[str]
    status: str
    created_at: datetime
    expiry_date: datetime
    last_active_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CredentialIssuedResponse(BaseModel):
    agent_id: str
    credential: str  # the JWT itself — shown once, like a real secret
    credential_id: str
    expires_at: datetime
    scopes: List[str]


class RegisterResponse(BaseModel):
    identity: AgentIdentityResponse
    credential: CredentialIssuedResponse


class RotateResponse(BaseModel):
    credential: CredentialIssuedResponse
    revoked_credential_id: str


class ReviewFlag(BaseModel):
    agent_id: str
    name: str
    owning_team: str
    reason: str
    last_active_at: Optional[datetime]
    days_inactive: Optional[int]


class QuarterlyReviewReport(BaseModel):
    generated_at: datetime
    total_active_agents: int
    stale_agents: List[ReviewFlag]
    auto_revoked_this_run: List[ReviewFlag]
    by_team: dict


class AIReviewDecision(BaseModel):
    agent_id: str
    action: str  # "suspend" | "flag" | "no_action"
    reasoning: str
    applied: bool
    reason: Optional[str] = None


class AIReviewRunResponse(BaseModel):
    run_id: str
    triggered_by: str
    model: str
    status: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    decisions: List[AIReviewDecision] = []
    tool_call_count: int = 0

    class Config:
        from_attributes = True


class AIReviewRunDetail(AIReviewRunResponse):
    transcript: List[dict] = []


class SchedulerStatus(BaseModel):
    enabled: bool
    interval_hours: float
    next_run_at: Optional[datetime] = None
