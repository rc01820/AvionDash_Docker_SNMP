"""
SNMP support endpoints.

Exposes /api/snmp/metrics — a JSON snapshot consumed by the SNMP agent's
pass_persist helper. No authentication required; the agent is the only
intended caller and runs on the internal Docker network.
"""

import builtins
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import get_db
from models.flights import Flight
from models.aircraft import Aircraft
from models.airports import Airport
from app_metrics import metrics_state

router = APIRouter()


# ── Code mappings (must match MIB INTEGER enum definitions) ─────────────────
SEVERITY_CODE = {"warning": 1, "critical": 2}
TIER_CODE     = {"web": 1, "application": 2, "database": 3, "container": 4}


@router.get("/metrics")
async def snmp_metrics(db: Session = Depends(get_db)):
    """Return all metrics in the format expected by aviondash_pass.py."""

    # ── System ──────────────────────────────────────────────────────────────
    uptime_secs = time.time() - metrics_state.start_time
    fault_count = sum(1 for v in builtins.FAULT_STATE.values() if v)
    if fault_count == 0:
        health_state, health_msg = 1, "All systems nominal"
    elif fault_count <= 2:
        health_state, health_msg = 2, f"{fault_count} fault(s) active"
    else:
        health_state, health_msg = 3, f"{fault_count} faults active — degraded"

    system = {
        "name":          "AvionDash",
        "version":       "1.0.0",
        "environment":   os.getenv("DD_ENV", "demo"),
        "uptime_ticks":  int(uptime_secs * 100),  # hundredths of a second
        "health_state":  health_state,
        "health_message": health_msg,
        "last_restart":  metrics_state.start_iso,
    }

    # ── Application ────────────────────────────────────────────────────────
    application = {
        "requests_total":      metrics_state.requests_total,
        "requests_errors":     metrics_state.requests_errors,
        "error_rate_permille": metrics_state.error_rate_permille(),
        "latency_p50_ms":      metrics_state.latency_percentile(50),
        "latency_p95_ms":      metrics_state.latency_percentile(95),
        "latency_p99_ms":      metrics_state.latency_percentile(99),
        "active_sessions":     metrics_state.active_sessions,
        "login_success":       metrics_state.login_success,
        "login_failure":       metrics_state.login_failure,
        "memory_used_kb":      metrics_state.process_memory_kb(),
        "cpu_percent":         metrics_state.process_cpu_percent(),
        "thread_count":        metrics_state.process_thread_count(),
    }

    # ── Web tier (synthetic — Nginx stub_status would be richer) ───────────
    web = {
        "status":              1,  # up
        "requests_total":      metrics_state.web_requests_total(),
        "active_connections":  metrics_state.web_active_connections(),
        "upstream_latency_ms": metrics_state.latency_percentile(50),
        "status_2xx":          metrics_state.requests_total - metrics_state.requests_errors,
        "status_4xx":          metrics_state.status_4xx,
        "status_5xx":          metrics_state.requests_errors,
        "bytes_sent":          metrics_state.bytes_sent,
    }

    # ── Database ───────────────────────────────────────────────────────────
    db_status = 1
    last_err  = ""
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        db_status = 2
        last_err  = str(e)[:200]

    pool = None
    try:
        from database import engine
        pool = engine.pool
    except Exception:
        pass

    if pool is not None:
        try:
            checked_out = pool.checkedout()
            pool_size   = pool.size() + (pool._max_overflow if hasattr(pool, "_max_overflow") else 20)
        except Exception:
            checked_out = 0
            pool_size   = 30
    else:
        checked_out = 0
        pool_size   = 30

    database = {
        "status":              db_status,
        "connections_active":  checked_out,
        "connections_max":     pool_size,
        "connections_waiting": 0,
        "queries_total":       metrics_state.db_queries_total,
        "slow_queries":        metrics_state.db_slow_queries,
        "avg_query_time_ms":   metrics_state.db_avg_query_ms(),
        "errors":              metrics_state.db_errors,
        "last_error_message":  last_err,
    }

    # ── Operations ─────────────────────────────────────────────────────────
    total_f = db.query(Flight).count()
    active  = db.query(Flight).filter(Flight.status == "en_route").count()
    sched   = db.query(Flight).filter(Flight.status == "scheduled").count()
    board   = db.query(Flight).filter(Flight.status == "boarding").count()
    delayed = db.query(Flight).filter(Flight.delay_minutes > 0).count()
    canc    = db.query(Flight).filter(Flight.status == "cancelled").count()
    landed  = db.query(Flight).filter(Flight.status == "landed").count()
    total_a = db.query(Aircraft).count()
    act_a   = db.query(Aircraft).filter(Aircraft.status == "active").count()
    maint_a = db.query(Aircraft).filter(Aircraft.status == "maintenance").count()
    gnd_a   = db.query(Aircraft).filter(Aircraft.status == "grounded").count()
    total_ap = db.query(Airport).count()

    operations = {
        "total_flights":        total_f,
        "active_flights":       active,
        "scheduled_flights":    sched,
        "boarding_flights":     board,
        "delayed_flights":      delayed,
        "cancelled_flights":    canc,
        "landed_flights":       landed,
        "on_time_permille":     int(((total_f - delayed) / max(total_f, 1)) * 1000),
        "total_aircraft":       total_a,
        "active_aircraft":      act_a,
        "maintenance_aircraft": maint_a,
        "grounded_aircraft":    gnd_a,
        "fleet_utilisation_permille": int((act_a / max(total_a, 1)) * 1000),
        "total_airports":       total_ap,
    }

    # ── Containers (synthetic / static for demo) ──────────────────────────
    containers = [
        {
            "index": 1, "name": "aviondash-db", "tier": "database",
            "image": "mysql:8.0",
            "status_code": 1, "health_code": 1,
            "cpu_percent": 5, "memory_used_kb": 380000, "memory_limit_kb": 0,
            "net_rx_bytes": 0, "net_tx_bytes": 0, "restarts": 0,
            "uptime_ticks": int(uptime_secs * 100),
        },
        {
            "index": 2, "name": "aviondash-app", "tier": "application",
            "image": "aviondash-app:1.0.0",
            "status_code": 1, "health_code": 1,
            "cpu_percent": metrics_state.process_cpu_percent(),
            "memory_used_kb": metrics_state.process_memory_kb(),
            "memory_limit_kb": 0,
            "net_rx_bytes": 0, "net_tx_bytes": 0,
            "restarts": 0,
            "uptime_ticks": int(uptime_secs * 100),
        },
        {
            "index": 3, "name": "aviondash-web", "tier": "web",
            "image": "nginx:1.25-alpine",
            "status_code": 1, "health_code": 1,
            "cpu_percent": 2, "memory_used_kb": 22000, "memory_limit_kb": 0,
            "net_rx_bytes": 0, "net_tx_bytes": 0, "restarts": 0,
            "uptime_ticks": int(uptime_secs * 100),
        },
    ]

    # ── Chaos ──────────────────────────────────────────────────────────────
    from api.chaos import CATALOG  # local import to avoid circulars
    fault_rows = []
    for i, (key, info) in enumerate(CATALOG.items(), start=1):
        fault_rows.append({
            "index": i,
            "key":   key,
            "label": info["label"],
            "tier":  info["tier"],
            "severity_code": SEVERITY_CODE.get(info["severity"], 1),
            "enabled": bool(builtins.FAULT_STATE.get(key, False)),
            "last_change": metrics_state.fault_last_change.get(key, ""),
        })

    chaos = {
        "active_count": fault_count,
        "faults":       fault_rows,
    }

    return {
        "system":       system,
        "application":  application,
        "web":          web,
        "database":     database,
        "operations":   operations,
        "containers":   containers,
        "chaos":        chaos,
    }
