import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import verify_admin
from app.review import run_quarterly_review
from app.ai_reviewer import run_agentic_review
from app.models import AIReviewRun
from app.scheduler import scheduler
from app.config import settings
from app.schemas import (
    QuarterlyReviewReport, AIReviewRunResponse, AIReviewRunDetail,
    AIReviewDecision, SchedulerStatus,
)

log = logging.getLogger("agent_identity")
router = APIRouter(prefix="/review", tags=["access review"])


def _to_run_response(run: AIReviewRun, with_transcript: bool = False):
    decisions = []
    if run.summary:
        try:
            decisions = [AIReviewDecision(**d) for d in json.loads(run.summary)]
        except (json.JSONDecodeError, TypeError):
            decisions = []
    transcript = []
    tool_call_count = 0
    if run.transcript:
        try:
            transcript = json.loads(run.transcript)
            tool_call_count = sum(1 for t in transcript if t.get("type") == "tool_call")
        except json.JSONDecodeError:
            transcript = []

    base = dict(
        run_id=run.run_id, triggered_by=run.triggered_by, model=run.model,
        status=run.status, started_at=run.started_at, finished_at=run.finished_at,
        error=run.error, decisions=decisions, tool_call_count=tool_call_count,
    )
    if with_transcript:
        return AIReviewRunDetail(**base, transcript=transcript)
    return AIReviewRunResponse(**base)


@router.post("/quarterly", response_model=QuarterlyReviewReport, dependencies=[Depends(verify_admin)])
def quarterly_review(db: Session = Depends(get_db)):
    """Deterministic rule-based review: stale detection + expiry auto-revoke sweep."""
    report = run_quarterly_review(db)
    log.info("quarterly_review stale=%d auto_revoked=%d", len(report.stale_agents), len(report.auto_revoked_this_run))
    return report


@router.post("/ai-review", response_model=AIReviewRunResponse, dependencies=[Depends(verify_admin)])
def ai_review(db: Session = Depends(get_db)):
    """
    Manually trigger the autonomous agent loop right now (the same loop the
    scheduler runs automatically every AI_REVIEW_INTERVAL_HOURS). The model
    calls tools itself to inspect agents, consult its own past decisions,
    and act — see GET /review/ai-review/{run_id} for the full transcript.
    """
    try:
        run = run_agentic_review(db, triggered_by="admin:manual")
        return _to_run_response(run)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Agent run failed: {e}")


@router.get("/ai-review/runs", response_model=list[AIReviewRunResponse], dependencies=[Depends(verify_admin)])
def list_ai_review_runs(db: Session = Depends(get_db)):
    """History of every autonomous review run — scheduled and manual — newest first. This IS the agent's memory."""
    runs = db.query(AIReviewRun).order_by(AIReviewRun.started_at.desc()).limit(50).all()
    return [_to_run_response(r) for r in runs]


@router.get("/ai-review/{run_id}", response_model=AIReviewRunDetail, dependencies=[Depends(verify_admin)])
def get_ai_review_run(run_id: str, db: Session = Depends(get_db)):
    """Full transcript of one run: every tool call the agent made, its reasoning text, and results."""
    run = db.query(AIReviewRun).filter(AIReviewRun.run_id == run_id).first()
    if not run:
        raise HTTPException(404, "Run not found")
    return _to_run_response(run, with_transcript=True)


@router.get("/scheduler/status", response_model=SchedulerStatus, dependencies=[Depends(verify_admin)])
def scheduler_status():
    """Proves the agent is running autonomously on a schedule, not just on-demand."""
    job = scheduler.get_job("autonomous_agent_review") if scheduler.running else None
    next_run = job.next_run_time if job else None
    return SchedulerStatus(
        enabled=settings.AI_REVIEW_INTERVAL_HOURS > 0 and scheduler.running,
        interval_hours=settings.AI_REVIEW_INTERVAL_HOURS,
        next_run_at=next_run,
    )
