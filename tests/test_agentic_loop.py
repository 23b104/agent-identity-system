"""
Validates the agent LOOP MECHANICS (tool dispatch, real DB actions, memory
persistence across runs) using a scripted fake Groq client that plays the
role of the LLM turn-by-turn — a realistic multi-step tool-calling
conversation, just with canned responses instead of a live model call.

This does NOT test the model's actual judgment (that requires a real
GROQ_API_KEY and live inference — see README). It proves that if the model
calls tools in this shape, the system correctly: perceives the roster,
looks up detail, checks its own memory, takes a real suspend action that
revokes credentials, and persists a transcript that the *next* run's memory
tool can see.

Run: python3 tests/test_agentic_loop.py
"""
import sys
import os
import json
from types import SimpleNamespace
from unittest.mock import patch

# This MUST run before any "from app..." import below, and must stay first —
# it's what makes the local `app` package importable regardless of how or
# from where this script is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_URL"] = "sqlite:///./test_agentic.db"
os.environ["GROQ_API_KEY"] = "fake-key-for-scripted-test"

if os.path.exists("test_agentic.db"):
    os.remove("test_agentic.db")

from app.database import Base, engine, SessionLocal
from app import crud, ai_reviewer
from app.models import Agent, AgentStatus, AIReviewRun

Base.metadata.create_all(bind=engine)


def make_tool_call(call_id, name, args):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        model_dump=lambda: {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}},
    )


class ScriptedGroqClient:
    """Plays a realistic multi-turn tool-calling conversation."""

    def __init__(self, script):
        self.script = script
        self.turn = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        content, tool_calls = self.script[self.turn]
        self.turn += 1
        msg = SimpleNamespace(content=content, tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def run_scripted(script, triggered_by):
    db = SessionLocal()
    with patch("app.ai_reviewer.Groq", return_value=ScriptedGroqClient(script)):
        run = ai_reviewer.run_agentic_review(db, triggered_by=triggered_by)
    db.close()
    return run


print("=== Setting up: register a risky agent + a healthy agent ===")
db = SessionLocal()
risky, _, _ = crud.register_agent(
    db, "legacy-delete-bot", "One-off migration script, should have been decommissioned",
    "platform-eng", ["read", "write", "admin"], ttl_days=90,
)
healthy, _, _ = crud.register_agent(
    db, "daily-sync-bot", "Syncs inventory counts every morning",
    "ops", ["read", "write"], ttl_days=90,
)
risky_id, healthy_id = risky.agent_id, healthy.agent_id
db.close()
print(f"risky agent:   {risky_id}")
print(f"healthy agent: {healthy_id}")

print("\n=== RUN 1: scripted model inspects both agents, suspends the risky one ===")
run1_script = [
    ("I'll start by listing active agents.",
     [make_tool_call("c1", "list_active_agents", {})]),
    ("Let me look closer at the migration bot — broad scopes, name suggests it's overdue.",
     [make_tool_call("c2", "get_agent_detail", {"agent_id": risky_id})]),
    ("No prior history on this one. Given admin+write scope and a self-described 'one-off' "
     "purpose that's clearly not ongoing, this is a real risk. Suspending.",
     [make_tool_call("c3", "suspend_agent", {
         "agent_id": risky_id,
         "reasoning": "Holds admin+write scope but purpose describes a one-off migration task, "
                      "not an ongoing service — scope is broader than the stated need warrants.",
     })]),
    ("Now checking the daily sync bot.",
     [make_tool_call("c4", "get_agent_detail", {"agent_id": healthy_id})]),
    ("This one's purpose matches its scope and it's a legitimate recurring job. Marking reviewed.",
     [make_tool_call("c5", "mark_reviewed", {
         "agent_id": healthy_id,
         "reasoning": "Scope (read,write) matches stated recurring sync purpose. No concerns.",
     })]),
    ("Review complete for this cycle.", None),
]
run1 = run_scripted(run1_script, "admin:manual")
print(f"run status: {run1.status}")
decisions1 = json.loads(run1.summary)
for d in decisions1:
    print(f"  -> {d['agent_id']}: {d['action']} (applied={d['applied']}) — {d['reasoning'][:70]}...")

db = SessionLocal()
risky_after = db.query(Agent).filter(Agent.agent_id == risky_id).first()
healthy_after = db.query(Agent).filter(Agent.agent_id == healthy_id).first()
assert risky_after.status == AgentStatus.suspended, "FAIL: risky agent was not actually suspended in DB"
assert healthy_after.status == AgentStatus.active, "FAIL: healthy agent should still be active"
print(f"\nDB check: risky agent status = {risky_after.status.value} (expected suspended)  PASS")
print(f"DB check: healthy agent status = {healthy_after.status.value} (expected active)  PASS")

from app.models import Credential
active_creds = db.query(Credential).filter(Credential.agent_id == risky_id, Credential.revoked == False).count()
assert active_creds == 0, "FAIL: suspended agent still has an unrevoked credential"
print(f"DB check: risky agent has 0 unrevoked credentials (real credential revocation)  PASS")
db.close()

print("\n=== RUN 2: scripted model checks MEMORY of run 1 before deciding on a re-registered risky agent ===")
run2_script = [
    ("Listing active agents for this cycle.",
     [make_tool_call("c1", "list_active_agents", {})]),
    ("Checking whether I've seen this exact agent before.",
     [make_tool_call("c2", "get_past_ai_decisions", {"agent_id": healthy_id})]),
    ("I reviewed daily-sync-bot last cycle and found no issues. Re-confirming healthy.",
     [make_tool_call("c3", "mark_reviewed", {
         "agent_id": healthy_id,
         "reasoning": "Consistent with prior review — no change in scope or purpose since last cycle.",
     })]),
    ("Done.", None),
]
run2 = run_scripted(run2_script, "scheduler")
print(f"run status: {run2.status}, triggered_by: {run2.triggered_by}")

transcript2 = json.loads(run2.transcript)
memory_call = next(t for t in transcript2 if t.get("name") == "get_past_ai_decisions")
print("\nMemory tool call result (what run 2 actually saw about run 1's decision):")
print(json.dumps(memory_call["result"], indent=2))
assert memory_call["result"]["past_decisions"] != "no prior decisions on record for this agent", \
    "FAIL: memory tool did not find run 1's decision"
assert memory_call["result"]["past_decisions"][0]["action"] == "no_action"
print("\nMemory check: run 2 correctly retrieved run 1's actual decision via get_past_ai_decisions  PASS")

print("\n=== Run history (this is what GET /review/ai-review/runs exposes) ===")
db = SessionLocal()
all_runs = db.query(AIReviewRun).order_by(AIReviewRun.started_at).all()
for r in all_runs:
    print(f"  {r.run_id}  triggered_by={r.triggered_by}  status={r.status}  decisions={len(json.loads(r.summary))}")
db.close()

engine.dispose()  # release SQLite's file handle before deleting it (needed on Windows)
try:
    os.remove("test_agentic.db")
except PermissionError:
    print("(Note: couldn't delete test_agentic.db, file still locked — harmless, ignore)")
print("\n=== ALL AGENTIC LOOP CHECKS PASSED ===")