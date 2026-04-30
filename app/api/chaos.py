"""Chaos Control API — fault injection endpoints."""

import builtins, gc, logging, os, random, threading, time
from datetime import datetime, timezone
from typing import Dict
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from api.auth import get_current_user, require_admin

logger = logging.getLogger("aviondash.chaos")
router = APIRouter()


class FaultToggle(BaseModel):
    enabled: bool


# ── Full fault catalog (14 original + 8 new = 22 total) ──────────────────────
CATALOG = {
    # ── Application tier ─────────────────────────────────────────────────────
    "slow_queries": {
        "label": "Slow DB Queries", "tier": "application", "severity": "warning",
        "description": "Injects 3–8s sleep before DB operations on /flights.",
        "datadog_signal": "db.query.duration anomaly → APM slow span",
        "snmp_trap": "avdTrapDbSlowQueries",
    },
    "high_error_rate": {
        "label": "High Error Rate (503)", "tier": "application", "severity": "critical",
        "description": "Returns HTTP 503 on 60% of requests, simulating upstream failure.",
        "datadog_signal": "service.error.rate spike → composite monitor",
        "snmp_trap": "avdTrapHighErrorRate",
    },
    "random_500s": {
        "label": "Random 500 Errors", "tier": "application", "severity": "warning",
        "description": "Returns HTTP 500 on 35% of requests, burning SLO error budget.",
        "datadog_signal": "http.server_error count → SLO burn-rate alert",
        "snmp_trap": "avdTrapHighErrorRate",
    },
    "latency_spike": {
        "label": "Latency Spike", "tier": "application", "severity": "warning",
        "description": "Adds 2–6s synchronous sleep on every request.",
        "datadog_signal": "trace.fastapi.request.duration p99 anomaly",
        "snmp_trap": "avdTrapLatencyDegraded",
    },
    "memory_leak": {
        "label": "Memory Leak", "tier": "application", "severity": "critical",
        "description": "Allocates 512 KB per request without freeing it.",
        "datadog_signal": "container.memory.usage → forecast monitor",
        "snmp_trap": "avdTrapHighMemoryUsage",
    },
    "cpu_spike": {
        "label": "CPU Spike", "tier": "application", "severity": "warning",
        "description": "Burns 300ms CPU per request via busy-wait.",
        "datadog_signal": "container.cpu.usage → threshold alert",
        "snmp_trap": "avdTrapHighCpuUsage",
    },
    "n_plus_one": {
        "label": "N+1 Query Pattern", "tier": "application", "severity": "warning",
        "description": "Issues extra SELECT per flight row — classic ORM anti-pattern.",
        "datadog_signal": "db.query.count spike → APM trace analytics",
        "snmp_trap": "avdTrapDbSlowQueries",
    },
    "db_pool_exhaustion": {
        "label": "DB Pool Exhaustion", "tier": "application", "severity": "critical",
        "description": "Holds DB connections for 5–12s, exhausting the pool.",
        "datadog_signal": "db.pool.connections.waiting → composite monitor",
        "snmp_trap": "avdTrapDbPoolExhausted",
    },
    "log_flood": {
        "label": "Log Flood", "tier": "application", "severity": "warning",
        "description": "Emits 50 WARNING lines per request.",
        "datadog_signal": "logs.count anomaly → log volume alert",
        "snmp_trap": None,
    },

    # ── NEW: Application tier ─────────────────────────────────────────────────
    "http_500_storm": {
        "label": "HTTP 500 Storm", "tier": "application", "severity": "critical",
        "description": "Returns HTTP 500 on 90% of requests for 60s bursts, "
                       "simulating an application crash loop. Fires avdTrapHighErrorRate "
                       "SNMP trap immediately on activation.",
        "datadog_signal": "http.server_error rate > 80% → P1 alert + SLO breach",
        "snmp_trap": "avdTrapHighErrorRate",
    },
    "auth_failure_burst": {
        "label": "Auth Failure Burst", "tier": "application", "severity": "warning",
        "description": "Rejects all login attempts with 401 Unauthorized, simulating "
                       "a misconfigured auth provider. Fires avdTrapAuthFailureBurst "
                       "SNMP trap on activation.",
        "datadog_signal": "login failure rate spike → security monitor",
        "snmp_trap": "avdTrapAuthFailureBurst",
    },
    "payload_corruption": {
        "label": "Payload Corruption", "tier": "application", "severity": "warning",
        "description": "Randomly mangles JSON response bodies on 20% of /flights "
                       "and /aircraft responses, causing client parse errors.",
        "datadog_signal": "client-side JSON parse errors → RUM error tracking",
        "snmp_trap": "avdTrapHighErrorRate",
    },
    "timeout_cascade": {
        "label": "Timeout Cascade", "tier": "application", "severity": "critical",
        "description": "Injects 30s sleeps on DB calls, causing upstream timeouts "
                       "that cascade to the Nginx tier. Fires latency + DB SNMP traps.",
        "datadog_signal": "upstream timeout → Nginx 504 → composite monitor",
        "snmp_trap": "avdTrapLatencyDegraded",
    },
    "flight_status_chaos": {
        "label": "Flight Status Spike", "tier": "application", "severity": "warning",
        "description": "Mass-updates flights to cancelled/delayed status in DB, "
                       "triggering business-level SNMP traps and operations monitors.",
        "datadog_signal": "avdOpsCancelledFlights spike → avdTrapFlightCancelled",
        "snmp_trap": "avdTrapFlightCancelled",
    },

    # ── Container tier ────────────────────────────────────────────────────────
    "health_check_fail": {
        "label": "Health Check Failure", "tier": "container", "severity": "critical",
        "description": "Returns 503 from /health — triggers Docker restart + Synthetics.",
        "datadog_signal": "synthetics.check.status FAIL → downtime alert",
        "snmp_trap": "avdTrapContainerUnhealthy",
    },
    "container_oom_simulation": {
        "label": "Container OOM Simulation", "tier": "container", "severity": "critical",
        "description": "Background thread allocates 5MB/s until stopped or OOM-killed.",
        "datadog_signal": "container.memory.usage → OOM forecast monitor",
        "snmp_trap": "avdTrapHighMemoryUsage",
    },
    "network_partition": {
        "label": "Network Partition", "tier": "container", "severity": "critical",
        "description": "Disposes DB connection pool, simulating app/db network split. "
                       "Fires avdTrapDbDown SNMP trap.",
        "datadog_signal": "DB connection errors → APM error propagation",
        "snmp_trap": "avdTrapDbDown",
    },
    "disk_fill": {
        "label": "Disk Fill", "tier": "container", "severity": "warning",
        "description": "Background thread writes 100KB chunks to log volume at 500KB/s.",
        "datadog_signal": "disk.in_use → threshold monitor",
        "snmp_trap": None,
    },
    "container_cpu_throttle": {
        "label": "Container CPU Throttle", "tier": "container", "severity": "warning",
        "description": "Spawns 4 CPU-burning threads that run continuously, "
                       "simulating noisy-neighbour CPU pressure. Fires avdTrapHighCpuUsage "
                       "SNMP trap on activation.",
        "datadog_signal": "container.cpu.usage sustained > 90% → throttle alert",
        "snmp_trap": "avdTrapHighCpuUsage",
    },
    "snmp_trap_test": {
        "label": "SNMP Trap Test (All Traps)", "tier": "snmp", "severity": "warning",
        "description": "Fires all 17 SNMP trap types in sequence with realistic varbinds. "
                       "Use to verify your NMS trap receiver is correctly receiving and "
                       "decoding AvionDash traps. Does NOT affect application behaviour.",
        "datadog_signal": "All avdTrap* notifications → NMS trap receiver",
        "snmp_trap": "ALL",
    },
    "cascading_failure": {
        "label": "Cascading Failure ⚡", "tier": "container", "severity": "critical",
        "description": "Activates slow_queries + high_error_rate + latency_spike + "
                       "log_flood + http_500_storm simultaneously. Fires "
                       "avdTrapCascadingFailure SNMP trap.",
        "datadog_signal": "composite monitor: 5 signals → P1 alert",
        "snmp_trap": "avdTrapCascadingFailure",
    },
}

# ── State ─────────────────────────────────────────────────────────────────────
_oom_running       = False
_disk_running      = False
_cpu_throttle_running = False
_cpu_threads: list = []


# ── Background workers ────────────────────────────────────────────────────────
def _oom_worker():
    blocks = []
    while _oom_running:
        blocks.append(bytearray(5 * 1024 * 1024))
        logger.warning(f"[FAULT][OOM] {len(blocks) * 5} MB allocated")
        time.sleep(1)
    del blocks; gc.collect()


def _disk_worker():
    path  = "/var/log/aviondash/disk_fill.log"
    chunk = "X" * 102400
    while _disk_running:
        try:
            open(path, "a").write(chunk + "\n")
        except Exception as e:
            logger.error(f"[FAULT][DISK] {e}"); break
        time.sleep(0.2)


def _cpu_burn_worker():
    """Saturate one CPU core continuously."""
    while _cpu_throttle_running:
        _ = sum(i * i for i in range(10000))


# ── Trap helper ───────────────────────────────────────────────────────────────
def _fire_trap(trap_key: str, info: dict, enabled: bool):
    """Fire the appropriate SNMP trap for a fault activation/deactivation."""
    try:
        import snmp_trap as st
        from app_metrics import metrics_state
        sev = 2 if info.get("severity") == "critical" else 1
        label = info.get("label", trap_key)

        if enabled:
            st.trap_fault_activated(trap_key, label, sev)

            # Additional specific traps based on snmp_trap field
            t = info.get("snmp_trap")
            if t == "avdTrapHighErrorRate":
                st.trap_high_error_rate(
                    metrics_state.error_rate_permille(),
                    metrics_state.requests_errors,
                    f"Fault activated: {label}"
                )
            elif t == "avdTrapLatencyDegraded":
                st.trap_latency_degraded(
                    metrics_state.latency_percentile(95),
                    metrics_state.latency_percentile(99),
                    f"Fault activated: {label}"
                )
            elif t == "avdTrapDbSlowQueries":
                st.trap_db_slow_queries(
                    metrics_state.db_avg_query_ms(),
                    metrics_state.db_slow_queries
                )
            elif t == "avdTrapDbDown":
                st.trap_db_down(2, f"Fault activated: {label}")
            elif t == "avdTrapDbPoolExhausted":
                st.trap_db_pool_exhausted(30, 30, 5)
            elif t == "avdTrapHighMemoryUsage":
                st.trap_high_memory("aviondash-app",
                                    metrics_state.process_memory_kb(), 0)
            elif t == "avdTrapHighCpuUsage":
                st.trap_high_cpu("aviondash-app",
                                 metrics_state.process_cpu_percent())
            elif t == "avdTrapAuthFailureBurst":
                st.trap_auth_failure_burst(metrics_state.login_failure)
            elif t == "avdTrapFlightCancelled":
                st.trap_flight_cancelled(3, 25)
            elif t == "avdTrapContainerUnhealthy":
                st.trap_container_unhealthy("aviondash-app", 2)
            elif t == "avdTrapCascadingFailure":
                active = sum(1 for v in builtins.FAULT_STATE.values() if v)
                st.trap_cascading_failure(active, 3, f"Cascading failure activated")
            elif t == "ALL":
                _fire_all_traps(st, metrics_state)
        else:
            st.trap_fault_deactivated(trap_key, label)

    except Exception as e:
        logger.warning(f"[FAULT] trap send failed: {e}")


def _fire_all_traps(st, ms):
    """Fire every trap type in sequence — used by the snmp_trap_test fault."""
    import time
    logger.info("[FAULT][SNMP_TEST] Firing all trap types...")
    st.trap_app_down("SNMP trap test — app_down")
    time.sleep(0.3)
    st.trap_app_up(int((time.time() - ms.start_time) * 100))
    time.sleep(0.3)
    st.trap_container_restart("aviondash-app", 3, 1)
    time.sleep(0.3)
    st.trap_container_unhealthy("aviondash-app", 2)
    time.sleep(0.3)
    st.trap_high_error_rate(750, ms.requests_errors, "SNMP trap test")
    time.sleep(0.3)
    st.trap_latency_degraded(3500, 6000, "SNMP trap test")
    time.sleep(0.3)
    st.trap_high_memory("aviondash-app", ms.process_memory_kb(), 512000)
    time.sleep(0.3)
    st.trap_high_cpu("aviondash-app", 92)
    time.sleep(0.3)
    st.trap_db_down(2, "SNMP trap test — simulated DB outage")
    time.sleep(0.3)
    st.trap_db_slow_queries(4500, ms.db_slow_queries + 10)
    time.sleep(0.3)
    st.trap_db_pool_exhausted(30, 30, 12)
    time.sleep(0.3)
    st.trap_flight_cancelled(5, 25)
    time.sleep(0.3)
    st.trap_aircraft_grounded(2, 15)
    time.sleep(0.3)
    st.trap_fault_activated("snmp_trap_test", "SNMP Trap Test", 1)
    time.sleep(0.3)
    st.trap_fault_deactivated("snmp_trap_test", "SNMP Trap Test")
    time.sleep(0.3)
    st.trap_cascading_failure(5, 3, "SNMP trap test — cascading failure")
    time.sleep(0.3)
    st.trap_auth_failure_burst(ms.login_failure + 5)
    logger.info("[FAULT][SNMP_TEST] All 17 traps fired.")


# ── Flight status chaos helper ────────────────────────────────────────────────
def _apply_flight_status_chaos(enable: bool):
    """Mass-update flight statuses to generate operations-level SNMP traps."""
    try:
        from database import SessionLocal
        from models.flights import Flight
        from sqlalchemy import text
        db = SessionLocal()
        if enable:
            # Cancel 3 flights, delay 5 flights
            db.execute(text(
                "UPDATE flights SET status='cancelled' "
                "WHERE status='scheduled' LIMIT 3"
            ))
            db.execute(text(
                "UPDATE flights SET status='delayed', delay_minutes=90 "
                "WHERE status='scheduled' LIMIT 5"
            ))
            db.commit()
            logger.warning("[FAULT][FLIGHT_STATUS] 3 cancelled, 5 delayed")
        else:
            # Restore reasonable statuses
            db.execute(text(
                "UPDATE flights SET status='scheduled', delay_minutes=0 "
                "WHERE status IN ('cancelled','delayed') "
                "AND notes IS NULL LIMIT 8"
            ))
            db.commit()
            logger.info("[FAULT][FLIGHT_STATUS] Restored flight statuses")
        db.close()
    except Exception as e:
        logger.error(f"[FAULT][FLIGHT_STATUS] DB update failed: {e}")


# ── Routes ─────────────────────────────────────────────────────────────────────
@router.get("/status")
async def fault_status(_=Depends(get_current_user)):
    return {"faults": dict(builtins.FAULT_STATE)}


@router.get("/catalog")
async def catalog(_=Depends(get_current_user)):
    return {"faults": CATALOG}


@router.post("/{fault_name}/toggle")
async def toggle(fault_name: str, body: FaultToggle, _=Depends(require_admin)):
    global _oom_running, _disk_running, _cpu_throttle_running, _cpu_threads

    if fault_name not in builtins.FAULT_STATE:
        raise HTTPException(404, f"Unknown fault: {fault_name}")

    en   = body.enabled
    info = CATALOG.get(fault_name, {})

    # Track fault change timestamp for SNMP metrics
    try:
        from app_metrics import metrics_state
        metrics_state.record_fault_change(fault_name)
    except Exception:
        pass

    # ── Meta-faults ───────────────────────────────────────────────────────────
    if fault_name == "cascading_failure":
        cascade = ["slow_queries", "high_error_rate", "latency_spike",
                   "log_flood", "http_500_storm"]
        for f in cascade:
            builtins.FAULT_STATE[f] = en
        builtins.FAULT_STATE["cascading_failure"] = en
        logger.warning(f"[FAULT] cascading_failure {'ON' if en else 'OFF'}")
        threading.Thread(target=_fire_trap, args=(fault_name, info, en),
                         daemon=True).start()
        return {"fault": fault_name, "enabled": en, "also_toggled": cascade}

    # ── Background workers ────────────────────────────────────────────────────
    if fault_name == "container_oom_simulation":
        if en and not _oom_running:
            _oom_running = True
            threading.Thread(target=_oom_worker, daemon=True).start()
        elif not en:
            _oom_running = False

    if fault_name == "disk_fill":
        if en and not _disk_running:
            _disk_running = True
            threading.Thread(target=_disk_worker, daemon=True).start()
        elif not en:
            _disk_running = False
            try:
                os.remove("/var/log/aviondash/disk_fill.log")
            except FileNotFoundError:
                pass

    if fault_name == "container_cpu_throttle":
        if en and not _cpu_throttle_running:
            _cpu_throttle_running = True
            _cpu_threads = []
            for _ in range(4):
                t = threading.Thread(target=_cpu_burn_worker, daemon=True)
                t.start()
                _cpu_threads.append(t)
            logger.warning("[FAULT][CPU_THROTTLE] 4 burn threads started")
        elif not en:
            _cpu_throttle_running = False
            _cpu_threads = []
            logger.info("[FAULT][CPU_THROTTLE] Burn threads signalled to stop")

    # ── DB operations ─────────────────────────────────────────────────────────
    if fault_name == "network_partition":
        if en:
            from database import engine
            engine.dispose()
            logger.critical("[FAULT] network_partition: DB pool disposed")

    if fault_name == "flight_status_chaos":
        threading.Thread(target=_apply_flight_status_chaos, args=(en,),
                         daemon=True).start()

    # ── SNMP trap test — fire async, doesn't block response ──────────────────
    if fault_name == "snmp_trap_test" and en:
        try:
            import snmp_trap as st
            from app_metrics import metrics_state as ms
            threading.Thread(target=_fire_all_traps, args=(st, ms),
                             daemon=True).start()
        except Exception as e:
            logger.warning(f"[FAULT][SNMP_TEST] trap init failed: {e}")

    # ── Set state & fire activation trap ─────────────────────────────────────
    builtins.FAULT_STATE[fault_name] = en
    logger.warning(f"[FAULT] {fault_name} {'ON' if en else 'OFF'}")
    threading.Thread(target=_fire_trap, args=(fault_name, info, en),
                     daemon=True).start()

    return {"fault": fault_name, "enabled": en}


@router.post("/reset-all")
async def reset_all(_=Depends(require_admin)):
    global _oom_running, _disk_running, _cpu_throttle_running, _cpu_threads
    _oom_running = _disk_running = _cpu_throttle_running = False
    _cpu_threads = []
    for k in builtins.FAULT_STATE:
        builtins.FAULT_STATE[k] = False
    try:
        os.remove("/var/log/aviondash/disk_fill.log")
    except FileNotFoundError:
        pass
    gc.collect()
    # Restore any flight status changes
    threading.Thread(target=_apply_flight_status_chaos, args=(False,),
                     daemon=True).start()
    return {"message": "All faults cleared"}
