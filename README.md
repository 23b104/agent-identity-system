# Agent Identity Management System

Provisions and governs **machine identities for AI agents** with the same rigour
a company applies to human user accounts: scoped, time-bounded credentials,
mandatory rotation, quarterly access reviews, stale-agent detection, and
automatic revocation on expiry — plus an **autonomous LLM reviewer** that
reasons about the agent directory and proposes/executes governance actions
through the same permission-checked path a human admin uses.

Built for PS-2.1 (Agent Identity Card).

---

## Why this is agentic, not just CRUD + one LLM call

Most of the system (registration, rotation, scope enforcement, the deterministic
30-day stale rule, auto-revoke) is intentionally **rule-based** — identity and
access control should be predictable and auditable, not left to a model's
mood. The agentic layer sits on top of that, not instead of it, and it has
all four properties that separate an agent from a classifier wrapped in an
endpoint:

**1. Perceives — it decides what to look at, we don't hand it a fixed snapshot.**
The model is given a set of tools, not a pre-baked dossier. It starts by
calling `list_active_agents()` itself, then decides which agents warrant a
closer look via `get_agent_detail()` (full purpose, scope, credential count,
audit history) before forming an opinion. This is a real function-calling
loop against Groq's OpenAI-compatible API (`llama-3.3-70b-versatile`), running
for up to 20 tool-call iterations per review — not one prompt-in, JSON-out call.

**2. Reasons — genuine judgment, not a threshold.** Between tool calls the
model writes out its thinking (captured verbatim in the run transcript)
before deciding `suspend` / `flag` / `mark_reviewed` per agent. It weighs
combinations a static rule can't — "45+ days idle *and* holds `admin` scope"
is a materially different risk than either signal alone, and a scope/purpose
mismatch ("purpose says 'read invoices', scope includes `delete:ledger`")
is something no `if` statement written in advance would catch.

**3. Acts — with real, immediate consequences, through the guarded path.**
The model's *only* way to change anything is by calling `suspend_agent`,
`flag_agent`, or `mark_reviewed` as tools — there's no code path where the
model's text output is trusted directly. Every `suspend_agent` tool call
executes through `crud.suspend_agent()`, the exact same function a human
admin's `POST /agents/{id}/suspend` call uses: it revokes the agent's active
credentials in the database and writes to the audit log tagged
`actor="ai-reviewer:llama-3.3-70b-versatile"`, so an AI-initiated suspension
is always distinguishable from a human one. This was verified directly
against the database in testing — a suspended agent's credentials go to
zero unrevoked rows, not just a status flag.

**4. Remembers — decisions persist and inform future decisions.** Every run
is stored in full (`AIReviewRun`: who/what triggered it, every tool call,
every reasoning snippet, every decision) and exposed via
`GET /review/ai-review/runs` and `GET /review/ai-review/{run_id}`. The model
has a `get_past_ai_decisions(agent_id)` tool that queries this history, so a
later run can reason "I flagged this agent last cycle and nothing's changed
since — that's a stronger case for suspension now than a first-time flag
would be," rather than re-deriving its opinion from zero every time.

**5. Autonomous trigger — it runs itself.** `app/scheduler.py` uses
APScheduler to fire the full review loop on an interval
(`AI_REVIEW_INTERVAL_HOURS`, default every 6 hours) with no human needed to
start it — `triggered_by="scheduler"` in the run record vs.
`triggered_by="admin:manual"` for the on-demand endpoint. Check
`GET /review/scheduler/status` to see the real next-run timestamp.

**Fails closed, always.** If `GROQ_API_KEY` is unset, Groq is unreachable, or
the loop errors out, the run is recorded with `status="failed"` and the real
error — never silently skipped, never a fabricated report. The scheduler
checks for a key before each scheduled run and logs a skip rather than
crashing if one isn't configured.

> **Testing note on this submission:** the sandbox this was built in blocks
> outbound requests to `api.groq.com` at the network level, so the live LLM
> reasoning itself couldn't be exercised from inside that environment. Every
> other part of the loop — tool dispatch, real credential revocation on
> suspend, transcript persistence, and cross-run memory retrieval — was
> verified against the real database using `tests/test_agentic_loop.py`,
> which drives the exact same `run_agentic_review()` function through a
> scripted multi-turn tool-calling conversation standing in for Groq's
> response. Once deployed (or run locally with network access) with a real
> `GROQ_API_KEY`, the identical code path runs against live inference —
> nothing changes except which client answers the `chat.completions.create`
> call.

---

## What's implemented (mapped to the problem statement)

| Requirement | Where |
|---|---|
| Agent registration flow (name, purpose, team, scopes → scoped credential + identity record) | `POST /agents/register` |
| Identity record: agent ID, team, created, expiry, scopes, status | `app/models.py::Agent` |
| Credential rotation (old revoked, new issued) | `POST /agents/{id}/rotate` |
| Quarterly review: stale flagging (30+ days no call) + report | `POST /review/quarterly` |
| Auto-revoke on expiry | Enforced live in `ScopeChecker` (every call) **and** swept in the quarterly job |
| Scope enforcement (read-only can't write) | `app/auth.py::ScopeChecker`, demoed via `/tools/read`, `/tools/write`, `/tools/admin` |
| Autonomous, tool-calling AI reviewer with memory + self-scheduling | `POST /review/ai-review` (manual trigger), `app/scheduler.py` (auto trigger), `app/ai_reviewer.py`, `GET /review/ai-review/runs` + `/{run_id}` (memory/audit) |
| Audit trail | `app/models.py::AuditEvent` — every create/rotate/suspend/auto-revoke/AI action logged |
| Logging, error handling, health check | `app/main.py` (structured request logging, global exception handler, `GET /health`) |
| Real LLM provider | Groq (free, fast) — see below |
| Cloud deployment (no AWS) | Render Blueprint (`render.yaml`) + `Dockerfile`, works on Railway/Fly too |

---

## 1. Local setup (5 minutes)

**Prerequisites:** Python 3.11+ — [python.org/downloads](https://www.python.org/downloads/)

```bash
# from the project root
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# open .env and set ADMIN_API_KEY / JWT_SECRET to anything for local testing
```

Get a **free** Groq API key (powers the autonomous reviewer) at
**https://console.groq.com/keys** — sign up, create a key, paste it into
`.env` as `GROQ_API_KEY`. No credit card required, generous free tier.

Run it:

```bash
export $(cat .env | xargs)   # or use python-dotenv / your shell's env loading
uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000/docs** — full interactive Swagger UI is
auto-generated by FastAPI. Every endpoint below can be exercised there without
writing any curl.

---

## 2. Prove it works (success criteria walkthrough)

With the server running, in another terminal:

```bash
export ADMIN_KEY=dev-admin-key   # match whatever's in your .env
BASE=http://localhost:8000
```

**Register 3 agents with different scopes:**
```bash
curl -s -X POST $BASE/agents/register -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" -d '{
  "name": "invoice-reader-bot", "purpose": "Reads invoice data for reporting",
  "owning_team": "finance-ops", "requested_scopes": ["read"], "ttl_days": 90}'

curl -s -X POST $BASE/agents/register -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" -d '{
  "name": "reconciliation-bot", "purpose": "Reconciles ledger entries",
  "owning_team": "finance-ops", "requested_scopes": ["read","write"], "ttl_days": 90}'

curl -s -X POST $BASE/agents/register -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" -d '{
  "name": "infra-provisioner-bot", "purpose": "Provisions cloud resources",
  "owning_team": "platform-eng", "requested_scopes": ["read","write","admin"], "ttl_days": 30}'
```
Each response contains `identity` (the record) and `credential` (a JWT — shown
once, exactly like a real secret).

**Scope enforcement** — take the `read`-only agent's credential and try both:
```bash
TOK=<paste the read-only agent's credential>
curl -s $BASE/tools/read  -H "Authorization: Bearer $TOK"   # -> 200
curl -s -X POST $BASE/tools/write -H "Authorization: Bearer $TOK"   # -> 403, scope missing
```

**Rotation:**
```bash
curl -s -X POST $BASE/agents/<agent_id>/rotate -H "X-Admin-Key: $ADMIN_KEY"
# old credential now returns 401 on any protected call; new one works
```

**Stale + auto-revoke** — since a real 30-day wait isn't practical in a demo,
either (a) register an agent with `"ttl_days": 0` to see immediate auto-revoke
at call time, or (b) for the full quarterly-review flow, back-date a row for
a controlled test:
```bash
sqlite3 agent_identity.db "UPDATE agents SET last_active_at = datetime('now','-45 days') WHERE agent_id='<id>';"
curl -s -X POST $BASE/review/quarterly -H "X-Admin-Key: $ADMIN_KEY"
# -> flags it in stale_agents with days_inactive: 45
```

**Autonomous AI review — trigger it manually right now:**
```bash
curl -s -X POST "$BASE/review/ai-review" -H "X-Admin-Key: $ADMIN_KEY" | python3 -m json.tool
```
Returns the run's per-agent `decisions` (`suspend`/`flag`/`no_action`) with
the model's written `reasoning` and `applied: true/false`. For the full
tool-call-by-tool-call transcript of what the agent actually looked at:
```bash
curl -s "$BASE/review/ai-review/<run_id>" -H "X-Admin-Key: $ADMIN_KEY" | python3 -m json.tool
```

**See it running autonomously, with no trigger from you:**
```bash
curl -s "$BASE/review/scheduler/status" -H "X-Admin-Key: $ADMIN_KEY"
# {"enabled": true, "interval_hours": 6.0, "next_run_at": "..."}

curl -s "$BASE/review/ai-review/runs" -H "X-Admin-Key: $ADMIN_KEY" | python3 -m json.tool
# entries with triggered_by: "scheduler" appear here on their own over time
```

**Prove the loop mechanics without a live model call** (useful if you're
offline or don't have a Groq key yet — see the testing note above for why
this exists):
```bash
python3 tests/test_agentic_loop.py
```

Or run the original rule-based success-criteria suite:
```bash
python3 tests/test_flow.py http://localhost:8000 dev-admin-key
```

---

## 3. Deploy to the cloud (no AWS)

**Render** (recommended — free tier, zero config beyond the blueprint,
no credit card):

1. Push this project to a GitHub repo.
2. Go to **https://dashboard.render.com/blueprints** → "New Blueprint Instance" → connect the repo. Render reads `render.yaml` and provisions the web service *and* a free Postgres database automatically.
3. In the Render dashboard, set the `GROQ_API_KEY` env var (get one free at https://console.groq.com/keys — it's marked `sync: false` in the blueprint so it won't be committed to git).
4. Deploy. You'll get a public HTTPS URL like `https://agent-identity-system.onrender.com`.
5. Health check: `GET https://<your-app>.onrender.com/health`.

This satisfies the rubric's "deployed and governs real AI workloads in a cloud
environment" bar without touching AWS: real Postgres persistence, a public
HTTPS API, and a genuine external LLM call (Groq) on every AI review.

**Alternatives** (same `Dockerfile` works unmodified):
- **Railway** — https://railway.app — `railway up` from the CLI, or connect the GitHub repo in the dashboard. Free trial credit, Postgres add-on is one click.
- **Fly.io** — https://fly.io — `fly launch` detects the Dockerfile automatically; `fly postgres create` for a managed DB.

For any of these, the only required env vars are `ADMIN_API_KEY`, `JWT_SECRET`,
`DATABASE_URL` (Postgres connection string — SQLite is fine for a demo but
won't survive container restarts on most platforms), and `GROQ_API_KEY`.

---

## 4. Architecture notes

- **Why JWT for credentials, not opaque tokens:** scopes and expiry are
  embedded in the token itself (`scopes`, `exp` claims), so any service that
  trusts the shared `JWT_SECRET` can verify a credential without a database
  round-trip — the DB lookup by `jti` is only needed to check *revocation*,
  which can't be encoded in a stateless token. This mirrors how real
  workload-identity systems (SPIFFE/SPIRE, cloud IAM STS tokens) separate
  "is this token cryptographically valid" from "has it been revoked since
  issuance."
- **Why two auth layers:** `X-Admin-Key` gates the control plane (who can
  register/suspend/review agents) — this is a stand-in for real SSO/OIDC in
  a production deployment (see Bonus below). The `Authorization: Bearer <JWT>`
  layer is the *agent's own* credential, checked on every tool call — this is
  the data plane, and it's what the success criteria's "read-only agent
  cannot perform write operations" is testing.
- **Audit trail is append-only** (`AuditEvent`) and every mutation — human or
  AI-initiated — writes to it with an `actor` field, so "who/what suspended
  this agent and why" is always answerable, which is the actual governance
  requirement behind PS-2.1's framing (API keys today have no such trail).
- **Postgres in production, SQLite for local dev** — controlled entirely by
  `DATABASE_URL`; no code changes needed (see `app/database.py`).

---

## 5. Bonus: real IAM provider (Okta/Auth0) integration path

The problem statement's bonus asks for real OIDC tokens. This system is
structured so that's an additive change, not a rewrite:

1. Register this API as an OIDC **client** in Auth0 (free tier:
   https://auth0.com/signup) or Okta (free developer tier:
   https://developer.okta.com/signup/), and register each *agent* as a
   **machine-to-machine application** in that tenant (Auth0: Applications →
   Machine to Machine; Okta: Applications → API Services).
2. Swap `create_agent_credential()` in `app/auth.py` from local JWT signing to
   a call to the provider's token endpoint
   (`POST https://<tenant>.auth0.com/oauth/token` with
   `grant_type=client_credentials`), requesting scopes as Auth0 API
   permissions instead of a custom claim.
3. Swap `ScopeChecker` from local `jose.jwt.decode` to verifying against the
   provider's JWKS endpoint (`https://<tenant>.auth0.com/.well-known/jwks.json`)
   — `python-jose` already supports JWKS verification, so this is a ~20-line
   change, not a new dependency.
4. The identity record, audit trail, stale detection, and AI reviewer are
   unaffected — they operate on `Agent` rows regardless of where the
   underlying credential was minted.

This isn't implemented in the submitted code (it requires a live paid-or-free
tenant to demo, which isn't reproducible for a grader without creating their
own account), but the integration seam is deliberately isolated to
`create_agent_credential()` and `ScopeChecker` for exactly this reason.

---

## 6. Project layout

```
app/
  main.py         FastAPI app, middleware, logging, health check
  config.py       All settings, env-var driven
  database.py     SQLAlchemy engine/session (SQLite locally, Postgres in prod)
  models.py       Agent, Credential, AuditEvent ORM models
  schemas.py      Pydantic request/response models
  auth.py         JWT issuance + ScopeChecker (the enforcement point)
  crud.py         register / rotate / suspend business logic
  review.py       Quarterly review: stale detection + auto-revoke sweep
  ai_reviewer.py  Autonomous LLM reviewer (Groq)
  routers/
    agents.py     /agents/* — registration, listing, rotation, suspension
    demo_tools.py /tools/*  — scope-protected endpoints used to prove enforcement
    review.py     /review/* — quarterly + AI review
tests/
  test_flow.py    Automated smoke test covering the success criteria
Dockerfile
render.yaml       Render Blueprint (free tier, no AWS)
.env.example
```
