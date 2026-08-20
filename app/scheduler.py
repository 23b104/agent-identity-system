"""
This is what makes the agent's operation autonomous rather than purely
request-triggered: a background job fires the full tool-calling review loop
on its own cadence, with no human in the loop for the trigger itself. A
human can still trigger an out-of-band run via POST /review/ai-review, but
the system does not depend on that — left alone, it reviews itself.
"""
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.database import SessionLocal
from app.ai_reviewer import run_agentic_review

log = logging.getLogger("agent_identity")
scheduler = BackgroundScheduler()


def _scheduled_review_job():
    if not settings.GROQ_API_KEY:
        log.info("scheduled_ai_review_skipped reason=no_groq_api_key")
        return
    db = SessionLocal()
    try:
        run = run_agentic_review(db, triggered_by="scheduler")
        log.info("scheduled_ai_review_completed run_id=%s", run.run_id)
    except Exception:
        log.exception("scheduled_ai_review_failed")
    finally:
        db.close()


def start_scheduler():
    if settings.AI_REVIEW_INTERVAL_HOURS <= 0:
        log.info("scheduler_disabled reason=AI_REVIEW_INTERVAL_HOURS<=0")
        return
    scheduler.add_job(
        _scheduled_review_job,
        trigger=IntervalTrigger(hours=settings.AI_REVIEW_INTERVAL_HOURS),
        id="autonomous_agent_review",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    log.info("scheduler_started interval_hours=%s", settings.AI_REVIEW_INTERVAL_HOURS)


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
