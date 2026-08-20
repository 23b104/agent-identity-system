import logging
import time
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from contextlib import asynccontextmanager

from app.database import Base, engine
from app.config import settings
from app.routers import agents, demo_tools, review
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("agent_identity")

Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()  # autonomous agent begins reviewing itself on its own cadence
    log.info("startup_complete ai_review_interval_hours=%s", settings.AI_REVIEW_INTERVAL_HOURS)
    yield
    stop_scheduler()


app = FastAPI(
    title=settings.APP_NAME,
    description="Provisions and governs machine identities for AI agents with "
                "the same rigour as human IAM: scoped time-bounded credentials, "
                "rotation, quarterly access review, stale detection, and "
                "auto-revoke on expiry — plus an autonomous, tool-calling LLM "
                "review agent that runs itself on a schedule.",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/demo", StaticFiles(directory="static", html=True), name="demo")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to specific origins in production
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled_error path=%s method=%s", request.url.path, request.method)
        raise
    duration_ms = round((time.time() - start) * 1000, 2)
    log.info("request path=%s method=%s status=%d duration_ms=%s",
              request.url.path, request.method, response.status_code, duration_ms)
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("unhandled_exception")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok", "env": settings.ENV, "service": settings.APP_NAME}


app.include_router(agents.router)
app.include_router(demo_tools.router)
app.include_router(review.router)
