"""
Stand-in "tools" that a real agent would call — a read endpoint and a write
endpoint — protected by the ScopeChecker dependency. These exist purely to
prove scope enforcement end-to-end: present a credential without the
'write' scope to POST /tools/write and it must be rejected with 403.
"""
from fastapi import APIRouter, Depends
from app.auth import ScopeChecker
from app.models import Agent

router = APIRouter(prefix="/tools", tags=["demo tools (scope-protected)"])


@router.get("/read")
def read_tool(agent: Agent = Depends(ScopeChecker("read"))):
    return {"ok": True, "agent_id": agent.agent_id, "action": "read", "data": "some read-only resource"}


@router.post("/write")
def write_tool(agent: Agent = Depends(ScopeChecker("write"))):
    return {"ok": True, "agent_id": agent.agent_id, "action": "write", "result": "resource mutated"}


@router.post("/admin")
def admin_tool(agent: Agent = Depends(ScopeChecker("admin"))):
    return {"ok": True, "agent_id": agent.agent_id, "action": "admin", "result": "privileged action executed"}
