"""
SNMP Trap sender for AvionDash.

Sends SNMPv3 INFORM / TRAP notifications to configured trap receiver(s).
Uses pysnmp.  Called by the application on specific events (fault changes,
health state transitions, auth failures, etc).

Trap OID base: 1.3.6.1.4.1.21308.1.10.0.<trap_id>

Environment:
  SNMP_TRAP_HOST        Trap receiver IP/hostname   (default: snmp-trap-receiver)
  SNMP_TRAP_PORT        UDP port                    (default: 162)
  SNMP_TRAP_USER        SNMPv3 username             (default: avdadmin)
  SNMP_TRAP_AUTH_PASS   Auth passphrase             (default: avdAdminAuth123)
  SNMP_TRAP_PRIV_PASS   Priv passphrase             (default: avdAdminPriv123)
  SNMP_TRAP_AUTH_PROTO  SHA | MD5                   (default: SHA)
  SNMP_TRAP_PRIV_PROTO  AES | DES                   (default: AES)
  SNMP_TRAPS_ENABLED    true | false                (default: true)
"""

import os
import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger("aviondash.snmp_trap")

# ── Environment config ───────────────────────────────────────────────────────
TRAP_HOST        = os.getenv("SNMP_TRAP_HOST",       "snmp-trap-receiver")
TRAP_PORT        = int(os.getenv("SNMP_TRAP_PORT",   "162"))
TRAP_USER        = os.getenv("SNMP_TRAP_USER",       "avdadmin")
TRAP_AUTH_PASS   = os.getenv("SNMP_TRAP_AUTH_PASS",  "avdAdminAuth123")
TRAP_PRIV_PASS   = os.getenv("SNMP_TRAP_PRIV_PASS",  "avdAdminPriv123")
TRAP_AUTH_PROTO  = os.getenv("SNMP_TRAP_AUTH_PROTO", "SHA")
TRAP_PRIV_PROTO  = os.getenv("SNMP_TRAP_PRIV_PROTO", "AES")
TRAPS_ENABLED    = os.getenv("SNMP_TRAPS_ENABLED",   "true").lower() == "true"

# ── OID constants ─────────────────────────────────────────────────────────────
BASE_NOTIF = (1, 3, 6, 1, 4, 1, 21308, 1, 10, 0)

TRAP_OID = {
    # Container/lifecycle
    "app_down":           BASE_NOTIF + (1,),
    "app_up":             BASE_NOTIF + (2,),
    "container_restart":  BASE_NOTIF + (3,),
    "container_unhealthy":BASE_NOTIF + (4,),
    # Performance
    "high_error_rate":    BASE_NOTIF + (10,),
    "latency_degraded":   BASE_NOTIF + (11,),
    "high_memory":        BASE_NOTIF + (12,),
    "high_cpu":           BASE_NOTIF + (13,),
    # Database
    "db_down":            BASE_NOTIF + (20,),
    "db_slow_queries":    BASE_NOTIF + (21,),
    "db_pool_exhausted":  BASE_NOTIF + (22,),
    # Operations
    "flight_cancelled":   BASE_NOTIF + (30,),
    "aircraft_grounded":  BASE_NOTIF + (31,),
    # Chaos
    "fault_activated":    BASE_NOTIF + (40,),
    "fault_deactivated":  BASE_NOTIF + (41,),
    "cascading_failure":  BASE_NOTIF + (42,),
    # Security
    "auth_failure_burst": BASE_NOTIF + (50,),
}

# MIB object OIDs used as varbinds (matching MIB column OIDs)
AVD = (1, 3, 6, 1, 4, 1, 21308, 1)
SYS_NAME_OID          = AVD + (1, 1, 0)
SYS_HEALTH_MSG_OID    = AVD + (1, 6, 0)
SYS_HEALTH_STATE_OID  = AVD + (1, 5, 0)
APP_ERROR_RATE_OID    = AVD + (2, 3, 0)
APP_REQ_ERRORS_OID    = AVD + (2, 2, 0)
APP_LATENCY_P95_OID   = AVD + (2, 5, 0)
APP_LATENCY_P99_OID   = AVD + (2, 6, 0)
APP_LOGIN_FAIL_OID    = AVD + (2, 9, 0)
APP_SYS_UPTIME_OID    = AVD + (1, 4, 0)
CTR_NAME_OID          = AVD + (5, 1, 1, 2, 0)   # placeholder index 0
CTR_STATUS_OID        = AVD + (5, 1, 1, 5, 0)
CTR_HEALTH_OID        = AVD + (5, 1, 1, 6, 0)
CTR_RESTARTS_OID      = AVD + (5, 1, 1, 12, 0)
CTR_MEM_USED_OID      = AVD + (5, 1, 1, 8, 0)
CTR_MEM_LIMIT_OID     = AVD + (5, 1, 1, 9, 0)
CTR_CPU_OID           = AVD + (5, 1, 1, 7, 0)
DB_STATUS_OID         = AVD + (4, 1, 0)
DB_SLOW_QUERIES_OID   = AVD + (4, 6, 0)
DB_AVG_QT_OID         = AVD + (4, 7, 0)
DB_CONN_ACTIVE_OID    = AVD + (4, 2, 0)
DB_CONN_MAX_OID       = AVD + (4, 3, 0)
DB_CONN_WAIT_OID      = AVD + (4, 4, 0)
DB_LAST_ERR_OID       = AVD + (4, 9, 0)
OPS_CANCELLED_OID     = AVD + (6, 6, 0)
OPS_TOTAL_FL_OID      = AVD + (6, 1, 0)
OPS_GROUNDED_OID      = AVD + (6, 12, 0)
OPS_TOTAL_AC_OID      = AVD + (6, 9, 0)
CHAOS_KEY_OID         = AVD + (7, 2, 1, 2, 0)
CHAOS_LABEL_OID       = AVD + (7, 2, 1, 3, 0)
CHAOS_SEV_OID         = AVD + (7, 2, 1, 5, 0)
CHAOS_ACTIVE_OID      = AVD + (7, 1, 0)


def _send(trap_key: str, varbinds: list):
    """Fire-and-forget trap on a background thread."""
    if not TRAPS_ENABLED:
        return
    t = threading.Thread(target=_do_send, args=(trap_key, varbinds), daemon=True)
    t.start()


def _do_send(trap_key: str, varbinds: list):
    """Blocking send — runs in background thread."""
    try:
        from pysnmp.hlapi import (
            SnmpEngine, UsmUserData, UdpTransportTarget,
            ContextData, NotificationType, ObjectIdentity,
            sendNotification,
            usmHMACSHAAuthProtocol, usmHMACMD5AuthProtocol,
            usmAesCfb128Protocol,   usmDESPrivProtocol,
            Integer32, OctetString, Counter32, Counter64, Gauge32, TimeTicks,
        )
    except ImportError:
        log.warning("pysnmp not installed — trap not sent: %s", trap_key)
        return

    auth_protos = {
        "SHA": usmHMACSHAAuthProtocol,
        "MD5": usmHMACMD5AuthProtocol,
    }
    priv_protos = {
        "AES": usmAesCfb128Protocol,
        "DES": usmDESPrivProtocol,
    }

    try:
        error_indication, error_status, error_index, _ = next(
            sendNotification(
                SnmpEngine(),
                UsmUserData(
                    TRAP_USER,
                    authKey=TRAP_AUTH_PASS,
                    privKey=TRAP_PRIV_PASS,
                    authProtocol=auth_protos.get(TRAP_AUTH_PROTO, usmHMACSHAAuthProtocol),
                    privProtocol=priv_protos.get(TRAP_PRIV_PROTO, usmAesCfb128Protocol),
                ),
                UdpTransportTarget((TRAP_HOST, TRAP_PORT), retries=1, timeout=2),
                ContextData(),
                "trap",
                NotificationType(
                    ObjectIdentity(*TRAP_OID[trap_key])
                ).addVarBinds(*varbinds),
            )
        )
        if error_indication:
            log.error("trap %s: send error: %s", trap_key, error_indication)
        else:
            log.info("trap %s sent to %s:%d", trap_key, TRAP_HOST, TRAP_PORT)
    except Exception as e:
        log.exception("trap %s: unexpected error: %s", trap_key, e)


# ── Public API ───────────────────────────────────────────────────────────────

def trap_app_down(health_msg: str = "Application unreachable"):
    _send("app_down", [
        (SYS_NAME_OID,       "AvionDash"),
        (SYS_HEALTH_MSG_OID, health_msg),
    ])


def trap_app_up(uptime_ticks: int = 0):
    _send("app_up", [
        (SYS_NAME_OID,     "AvionDash"),
        (APP_SYS_UPTIME_OID, uptime_ticks),
    ])


def trap_container_restart(name: str, status_code: int, restart_count: int):
    _send("container_restart", [
        (CTR_NAME_OID,     name),
        (CTR_STATUS_OID,   status_code),
        (CTR_RESTARTS_OID, restart_count),
    ])


def trap_container_unhealthy(name: str, health_code: int):
    _send("container_unhealthy", [
        (CTR_NAME_OID,   name),
        (CTR_HEALTH_OID, health_code),
    ])


def trap_high_error_rate(error_rate_permille: int, errors_total: int, msg: str = ""):
    _send("high_error_rate", [
        (APP_ERROR_RATE_OID, error_rate_permille),
        (APP_REQ_ERRORS_OID, errors_total),
        (SYS_HEALTH_MSG_OID, msg or f"Error rate {error_rate_permille/10:.1f}%"),
    ])


def trap_latency_degraded(p95_ms: int, p99_ms: int, msg: str = ""):
    _send("latency_degraded", [
        (APP_LATENCY_P95_OID, p95_ms),
        (APP_LATENCY_P99_OID, p99_ms),
        (SYS_HEALTH_MSG_OID,  msg or f"P95 latency {p95_ms}ms"),
    ])


def trap_high_memory(container_name: str, used_kb: int, limit_kb: int):
    _send("high_memory", [
        (CTR_NAME_OID,      container_name),
        (CTR_MEM_USED_OID,  used_kb),
        (CTR_MEM_LIMIT_OID, limit_kb),
    ])


def trap_high_cpu(container_name: str, cpu_pct: int):
    _send("high_cpu", [
        (CTR_NAME_OID, container_name),
        (CTR_CPU_OID,  cpu_pct),
    ])


def trap_db_down(status_code: int, error_msg: str):
    _send("db_down", [
        (DB_STATUS_OID,   status_code),
        (DB_LAST_ERR_OID, error_msg[:200]),
    ])


def trap_db_slow_queries(avg_ms: int, slow_count: int):
    _send("db_slow_queries", [
        (DB_AVG_QT_OID,      avg_ms),
        (DB_SLOW_QUERIES_OID, slow_count),
    ])


def trap_db_pool_exhausted(active: int, maximum: int, waiting: int):
    _send("db_pool_exhausted", [
        (DB_CONN_ACTIVE_OID, active),
        (DB_CONN_MAX_OID,    maximum),
        (DB_CONN_WAIT_OID,   waiting),
    ])


def trap_flight_cancelled(cancelled: int, total: int):
    _send("flight_cancelled", [
        (OPS_CANCELLED_OID, cancelled),
        (OPS_TOTAL_FL_OID,  total),
    ])


def trap_aircraft_grounded(grounded: int, total: int):
    _send("aircraft_grounded", [
        (OPS_GROUNDED_OID, grounded),
        (OPS_TOTAL_AC_OID, total),
    ])


def trap_fault_activated(key: str, label: str, severity_code: int):
    _send("fault_activated", [
        (CHAOS_KEY_OID,   key),
        (CHAOS_LABEL_OID, label),
        (CHAOS_SEV_OID,   severity_code),
    ])


def trap_fault_deactivated(key: str, label: str):
    _send("fault_deactivated", [
        (CHAOS_KEY_OID,   key),
        (CHAOS_LABEL_OID, label),
    ])


def trap_cascading_failure(active_count: int, health_state: int, msg: str):
    _send("cascading_failure", [
        (CHAOS_ACTIVE_OID,    active_count),
        (SYS_HEALTH_STATE_OID, health_state),
        (SYS_HEALTH_MSG_OID,   msg),
    ])


def trap_auth_failure_burst(failure_count: int):
    _send("auth_failure_burst", [
        (APP_LOGIN_FAIL_OID, failure_count),
    ])
