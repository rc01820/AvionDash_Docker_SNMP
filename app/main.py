"""AvionDash — FastAPI Application Tier"""

import time, random, logging, os, builtins, gc, json
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from database import engine, SessionLocal, Base
from app_metrics import metrics_state

os.makedirs("/var/log/aviondash", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/var/log/aviondash/app.log"),
    ],
)
logger = logging.getLogger("aviondash")

# ── Fault state ───────────────────────────────────────────────────────────────
FAULT_STATE = {k: False for k in [
    # Original 14
    "slow_queries", "high_error_rate", "memory_leak", "cpu_spike",
    "db_pool_exhaustion", "n_plus_one", "random_500s", "latency_spike",
    "container_oom_simulation", "network_partition", "disk_fill",
    "health_check_fail", "cascading_failure", "log_flood",
    # New 8
    "http_500_storm", "auth_failure_burst", "payload_corruption",
    "timeout_cascade", "flight_status_chaos", "container_cpu_throttle",
    "snmp_trap_test",
]}
builtins.FAULT_STATE = FAULT_STATE


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up — creating tables")
    from models.users    import User     # noqa
    from models.airports import Airport  # noqa
    from models.aircraft import Aircraft # noqa
    from models.flights  import Flight   # noqa
    Base.metadata.create_all(bind=engine)
    logger.info("Tables ready — seeding demo users")
    from init_db import ensure_users
    ensure_users()
    logger.info("Startup complete")
    try:
        from snmp_trap import trap_app_up
        trap_app_up(uptime_ticks=0)
    except Exception:
        pass
    yield
    logger.info("Shutting down")


app = FastAPI(
    title="AvionDash API", version="1.0.0", lifespan=lifespan,
    docs_url="/api/docs", redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


# ── Middleware ─────────────────────────────────────────────────────────────────
@app.middleware("http")
async def request_middleware(request: Request, call_next):
    start  = time.time()
    fs     = builtins.FAULT_STATE
    path   = request.url.path
    skip   = path in ("/health", "/health/db", "/api/snmp/metrics")

    if not skip:
        # ── Latency spike ────────────────────────────────────────────────────
        if fs.get("latency_spike"):
            d = random.uniform(2.0, 6.0)
            logger.warning(f"[FAULT] latency_spike {d:.1f}s on {path}")
            time.sleep(d)

        # ── Timeout cascade (30s — causes Nginx 504) ─────────────────────────
        if fs.get("timeout_cascade") and "/api/" in path:
            d = random.uniform(28.0, 32.0)
            logger.warning(f"[FAULT] timeout_cascade {d:.0f}s on {path}")
            time.sleep(d)

        # ── HTTP 500 storm (90% hit rate) ────────────────────────────────────
        if fs.get("http_500_storm") and random.random() < 0.90:
            logger.error(f"[FAULT] http_500_storm on {path}")
            metrics_state.record_request(500, (time.time()-start)*1000)
            return JSONResponse(500, {"detail": "Internal Server Error (500 storm fault)"})

        # ── Random 500 errors (35% hit rate) ─────────────────────────────────
        if fs.get("random_500s") and random.random() < 0.35:
            logger.error(f"[FAULT] random_500s on {path}")
            metrics_state.record_request(500, (time.time()-start)*1000)
            return JSONResponse(500, {"detail": "Internal Server Error (fault)"})

        # ── High error rate — 503 (60% hit rate) ─────────────────────────────
        if fs.get("high_error_rate") and random.random() < 0.60:
            logger.error(f"[FAULT] high_error_rate on {path}")
            metrics_state.record_request(503, (time.time()-start)*1000)
            return JSONResponse(503, {"detail": "Service Unavailable (fault)"})

        # ── Auth failure burst — block all logins ─────────────────────────────
        if fs.get("auth_failure_burst") and path == "/api/auth/token":
            logger.warning("[FAULT] auth_failure_burst: blocking login")
            metrics_state.record_login(success=False)
            metrics_state.record_request(401, (time.time()-start)*1000)
            return JSONResponse(401, {
                "detail": "Authentication service unavailable (fault injected)"
            })

        # ── Log flood ─────────────────────────────────────────────────────────
        if fs.get("log_flood"):
            for i in range(50):
                logger.warning(f"[FAULT][LOG_FLOOD] {i} {path}")

        # ── CPU spike ─────────────────────────────────────────────────────────
        if fs.get("cpu_spike"):
            end = time.time() + 0.3
            while time.time() < end:
                _ = random.random() ** 0.5

        # ── Memory leak ───────────────────────────────────────────────────────
        if fs.get("memory_leak"):
            if not hasattr(app.state, "leak"):
                app.state.leak = []
            app.state.leak.append(bytearray(512 * 1024))

    response = await call_next(request)

    # ── Payload corruption — mangle JSON on data endpoints ───────────────────
    if (fs.get("payload_corruption")
            and any(p in path for p in ["/api/flights", "/api/aircraft"])
            and random.random() < 0.20
            and not skip):
        logger.warning(f"[FAULT] payload_corruption on {path}")
        # Inject garbage into the body
        metrics_state.record_request(200, (time.time()-start)*1000)
        return Response(
            content=b'{"data": [CORRUPTED_JSON_FAULT' + b'\x00\xff\xfe',
            status_code=200,
            media_type="application/json",
        )

    # ── Record metrics ────────────────────────────────────────────────────────
    latency_ms  = (time.time() - start) * 1000
    content_len = int(response.headers.get("content-length", 0))
    metrics_state.record_request(response.status_code, latency_ms, content_len)

    # ── Auto-fire SNMP traps on threshold breaches ────────────────────────────
    if not skip:
        _check_traps()

    return response


def _check_traps():
    try:
        from snmp_trap import (
            trap_high_error_rate, trap_latency_degraded,
            trap_high_memory, trap_high_cpu,
        )
        err = metrics_state.error_rate_permille()
        if err > 300:
            trap_high_error_rate(err, metrics_state.requests_errors)
        p95 = metrics_state.latency_percentile(95)
        if p95 > 2000:
            trap_latency_degraded(p95, metrics_state.latency_percentile(99))
        mem = metrics_state.process_memory_kb()
        if mem > 400_000:
            trap_high_memory("aviondash-app", mem, 0)
        cpu = metrics_state.process_cpu_percent()
        if cpu > 80:
            trap_high_cpu("aviondash-app", cpu)
    except Exception:
        pass


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    if builtins.FAULT_STATE.get("health_check_fail"):
        raise HTTPException(503, "Health check failing (fault injected)")
    return {"status": "ok", "service": "aviondash-app"}


@app.get("/health/db")
async def health_db():
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        raise HTTPException(503, f"DB check failed: {e}")


@app.get("/")
async def root():
    return {"service": "AvionDash API", "version": "1.0.0"}


# ── Routers ───────────────────────────────────────────────────────────────────
from api import auth, flights, aircraft, airports, chaos, dashboard, snmp  # noqa
app.include_router(auth.router,      prefix="/api/auth")
app.include_router(flights.router,   prefix="/api/flights")
app.include_router(aircraft.router,  prefix="/api/aircraft")
app.include_router(airports.router,  prefix="/api/airports")
app.include_router(chaos.router,     prefix="/api/chaos")
app.include_router(dashboard.router, prefix="/api/dashboard")
app.include_router(snmp.router,      prefix="/api/snmp")
