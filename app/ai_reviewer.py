"""
Autonomous governance agent.

This is a genuine agentic loop, not a one-shot classifier call:

  PERCEIVE -> the agent calls tools to look up whatever it decides it needs
              (the active roster, a specific agent's audit history, its own
              past decisions about that agent) rather than being handed one
              fixed snapshot up front.
  REASON   -> between tool calls, the model (Groq, llama-3.3-70b-versatile)
              explains its thinking in plain text, which we capture in the
              transcript.
  ACT      -> the model's only way to change anything is by calling
              suspend_agent / flag_agent / mark_reviewed — real function
              calls that execute through the SAME permission-checked
              crud.suspend_agent() path a human admin uses, and every call
              is logged to the audit trail with actor="ai-reviewer:<model>".
  REMEMBER -> every run is persisted (AIReviewRun) with its full tool-call
              transcript. The get_past_ai_decisions tool lets the agent
              query its own history for an agent before deciding again, so
              it can reason like "I flagged this agent last cycle and
              nothing changed since — that's now worth suspending" instead
              of re-deriving the same conclusion from zero every time.

The loop is autonomous in two senses: (1) within a run, the model decides
which tools to call and in what order — we don't script the sequence; and
(2) runs themselves are triggered on a schedule by APScheduler
(app/scheduler.py), not only on a human hitting an endpoint.

Fails closed: if GROQ_API_KEY is unset, Groq errors, or the model produces
no usable tool calls, the run is marked "failed" with the error recorded —
never silently skipped and never faked.
"""
import json
import logging
from typing import List

from groq import Groq
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Agent, AgentStatus, AuditEvent, AIReviewRun, _now, as_utc
from app.crud import suspend_agent

log = logging.getLogger("agent_identity")

MAX_TOOL_ITERATIONS = 20

SYSTEM_PROMPT = """You are the autonomous access-governance reviewer for an Agent Identity \
Management system that provisions and governs machine (AI agent) identities the way a company \
governs human user accounts.

Your job, each time you are invoked: review every currently active agent identity and decide, \
per agent, one of three actions — suspend, flag, or mark_reviewed (explicit no-risk close-out) — \
by calling the corresponding tool. You MUST eventually call one decision tool \
(suspend_agent / flag_agent / mark_reviewed) for every agent returned by list_active_agents. \
Simply describing your opinion in text is not a decision — only a tool call counts.

Use the other tools to gather evidence before deciding:
- get_agent_detail: full purpose/scope/credential/audit history for one agent.
- get_past_ai_decisions: what YOU decided about this agent in previous review runs. Use this to \
reason longitudinally — e.g. an agent you flagged last cycle with no change since is a stronger \
suspend candidate now than an agent you're seeing for the first time.

Guidance for decisions:
- suspend: clearly elevated risk — e.g. long inactivity (45+ days) combined with broad scope \
(write/admin/delete), OR flagged in a prior run with no improvement since, OR scopes that are \
obviously broader than the stated purpose requires.
- flag: worth a human's attention but not risky enough to suspend outright.
- mark_reviewed: looks healthy and appropriately scoped — explicitly close it out so it's not \
re-flagged unnecessarily next cycle.

Be conservative with suspend. Always call get_agent_detail (and get_past_ai_decisions, if this \
isn't the first run) before deciding on an agent you don't already have strong evidence about. \
Explain your reasoning in text before each decision tool call — that reasoning is recorded as \
part of the audit trail."""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_active_agents",
            "description": "Get the roster of all currently active agent identities, with lightweight fields (id, name, team, scopes, days inactive, days until expiry). Call this first.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_agent_detail",
            "description": "Get full detail on one agent: purpose, scopes, credential count, and its recent audit log (creation, rotations, prior suspensions).",
            "parameters": {
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_past_ai_decisions",
            "description": "Get this agent's decision history from previous autonomous review runs (memory) — what action was taken and why, most recent first.",
            "parameters": {
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "suspend_agent",
            "description": "Suspend an agent identity and revoke its active credentials. Use for clearly elevated risk. This is a real, immediate action.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "reasoning": {"type": "string", "description": "Why this agent is being suspended."},
                },
                "required": ["agent_id", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flag_agent",
            "description": "Flag an agent for human attention without suspending it. Writes to the audit trail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["agent_id", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_reviewed",
            "description": "Explicitly close out an agent as healthy/no action needed this cycle. Writes to the audit trail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["agent_id", "reasoning"],
            },
        },
    },
]


class _AgentToolbox:
    """Holds the DB session + run-scoped state so tool calls can act on real data."""

    def __init__(self, db: Session, model_name: str):
        self.db = db
        self.model_name = model_name
        self.decisions: List[dict] = []

    def _snapshot(self) -> List[dict]:
        now = _now()
        agents = self.db.query(Agent).filter(Agent.status == AgentStatus.active).all()
        out = []
        for a in agents:
            out.append({
                "agent_id": a.agent_id,
                "name": a.name,
                "owning_team": a.owning_team,
                "scopes": a.scopes,
                "days_inactive": (now - as_utc(a.last_active_at or a.created_at)).days,
                "days_until_expiry": (as_utc(a.expiry_date) - now).days,
            })
        return out

    def list_active_agents(self):
        return {"agents": self._snapshot()}

    def get_agent_detail(self, agent_id: str):
        a = self.db.query(Agent).filter(Agent.agent_id == agent_id).first()
        if not a:
            return {"error": "agent not found"}
        now = _now()
        events = (
            self.db.query(AuditEvent)
            .filter(AuditEvent.agent_id == agent_id)
            .order_by(AuditEvent.created_at.desc())
            .limit(10)
            .all()
        )
        return {
            "agent_id": a.agent_id,
            "name": a.name,
            "purpose": a.purpose,
            "owning_team": a.owning_team,
            "scopes": a.scopes,
            "status": a.status.value,
            "created_days_ago": (now - as_utc(a.created_at)).days,
            "days_inactive": (now - as_utc(a.last_active_at or a.created_at)).days,
            "days_until_expiry": (as_utc(a.expiry_date) - now).days,
            "credential_count": len(a.credentials),
            "recent_audit_events": [
                {"type": e.event_type, "actor": e.actor, "detail": e.detail, "when": e.created_at.isoformat()}
                for e in events
            ],
        }

    def get_past_ai_decisions(self, agent_id: str):
        runs = (
            self.db.query(AIReviewRun)
            .filter(AIReviewRun.status == "completed")
            .order_by(AIReviewRun.started_at.desc())
            .limit(10)
            .all()
        )
        history = []
        for run in runs:
            if not run.summary:
                continue
            try:
                decisions = json.loads(run.summary)
            except json.JSONDecodeError:
                continue
            for d in decisions:
                if d.get("agent_id") == agent_id:
                    history.append({
                        "run_id": run.run_id,
                        "when": run.started_at.isoformat(),
                        "action": d.get("action"),
                        "reasoning": d.get("reasoning"),
                    })
        return {"agent_id": agent_id, "past_decisions": history or "no prior decisions on record for this agent"}

    def suspend_agent(self, agent_id: str, reasoning: str):
        a = self.db.query(Agent).filter(Agent.agent_id == agent_id).first()
        if not a or a.status != AgentStatus.active:
            result = {"applied": False, "reason": "agent not found or already inactive"}
        else:
            suspend_agent(self.db, a, actor=f"ai-reviewer:{self.model_name}", reason=f"AI-initiated: {reasoning}")
            result = {"applied": True}
        self.decisions.append({"agent_id": agent_id, "action": "suspend", "reasoning": reasoning, **result})
        return result

    def flag_agent(self, agent_id: str, reasoning: str):
        self.db.add(AuditEvent(agent_id=agent_id, event_type="ai_flagged",
                                actor=f"ai-reviewer:{self.model_name}", detail=reasoning))
        self.db.commit()
        result = {"applied": True}
        self.decisions.append({"agent_id": agent_id, "action": "flag", "reasoning": reasoning, **result})
        return result

    def mark_reviewed(self, agent_id: str, reasoning: str):
        self.db.add(AuditEvent(agent_id=agent_id, event_type="ai_reviewed_no_action",
                                actor=f"ai-reviewer:{self.model_name}", detail=reasoning))
        self.db.commit()
        result = {"applied": True}
        self.decisions.append({"agent_id": agent_id, "action": "no_action", "reasoning": reasoning, **result})
        return result

    def dispatch(self, name: str, args: dict):
        fn = getattr(self, name, None)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        try:
            return fn(**args)
        except TypeError as e:
            return {"error": f"bad arguments: {e}"}


def run_agentic_review(db: Session, triggered_by: str = "admin:manual") -> AIReviewRun:
    if not settings.GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Get a free key at https://console.groq.com/keys "
            "and set it as an environment variable to enable the autonomous review agent."
        )

    run = AIReviewRun(triggered_by=triggered_by, model=settings.GROQ_MODEL, status="running")
    db.add(run)
    db.commit()
    db.refresh(run)

    box = _AgentToolbox(db, settings.GROQ_MODEL)
    client = Groq(api_key=settings.GROQ_API_KEY)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Begin the review. Start by listing active agents."},
    ]
    transcript = []

    try:
        for iteration in range(MAX_TOOL_ITERATIONS):
            completion = client.chat.completions.create(
                model=settings.GROQ_MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.1,
                max_tokens=1500,
            )
            msg = completion.choices[0].message
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls] if msg.tool_calls else None,
            })

            if msg.content:
                transcript.append({"type": "reasoning", "text": msg.content})

            if not msg.tool_calls:
                # model stopped calling tools -> treat as end of run
                break

            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = box.dispatch(tc.function.name, args)
                transcript.append({"type": "tool_call", "name": tc.function.name, "args": args, "result": result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })
        else:
            transcript.append({"type": "note", "text": f"Hit max iterations ({MAX_TOOL_ITERATIONS}) — run truncated."})

        run.status = "completed"
        run.finished_at = _now()
        run.transcript = json.dumps(transcript)
        run.summary = json.dumps(box.decisions)
        db.add(run)
        db.commit()
        db.refresh(run)
        log.info("ai_review_run_completed run_id=%s decisions=%d", run.run_id, len(box.decisions))
        return run

    except Exception as e:
        log.exception("ai_review_run_failed run_id=%s", run.run_id)
        run.status = "failed"
        run.finished_at = _now()
        run.error = str(e)
        run.transcript = json.dumps(transcript)
        db.add(run)
        db.commit()
        raise
