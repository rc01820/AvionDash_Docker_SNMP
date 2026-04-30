#!/usr/bin/env python3
"""
AvionDash SNMP pass_persist handler.

Speaks the Net-SNMP "pass_persist" protocol over stdin/stdout. snmpd invokes
this once and keeps it running, sending one of:

    PING\n          → respond PONG
    get\nOID\n      → respond  OID\nTYPE\nVALUE   (or NONE)
    getnext\nOID\n  → respond next OID lexically  (or NONE)

Metrics are fetched from the FastAPI app at AVIONDASH_API_URL/api/snmp/metrics.
Results cached for 5 seconds to keep snmpd responsive.
"""

import os
import sys
import time
import json
import signal
import logging
from datetime import datetime

import requests

# ── Configuration ────────────────────────────────────────────────────────────
API_URL    = os.environ.get("AVIONDASH_API_URL", "http://app:8000")
CACHE_TTL  = 5.0  # seconds

# ── Logging ──────────────────────────────────────────────────────────────────
# Create log dir if it exists, otherwise fall back to stderr.
# The handler MUST NOT crash on startup — snmpd will mark it failed.
_log_dir = "/var/log/snmp"
try:
    os.makedirs(_log_dir, exist_ok=True)
    logging.basicConfig(
        filename=os.path.join(_log_dir, "aviondash_pass.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
except Exception:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
log = logging.getLogger("aviondash_pass")

# Don't die on broken pipes (snmpd may close us)
signal.signal(signal.SIGPIPE, signal.SIG_DFL)


# ── Base OID ─────────────────────────────────────────────────────────────────
BASE = ".1.3.6.1.4.1.21308.1"   # neomon.aviondash

# Static (rarely-changing) OID → (type, value) mapping built at runtime
_cache = {"data": None, "ts": 0.0}


def fetch_metrics():
    """Fetch fresh metrics from the FastAPI app, with caching."""
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]
    try:
        r = requests.get(f"{API_URL}/api/snmp/metrics", timeout=2.5)
        r.raise_for_status()
        _cache["data"] = r.json()
        _cache["ts"]   = now
    except Exception as e:
        log.warning(f"metrics fetch failed: {e}")
        if _cache["data"] is None:
            _cache["data"] = {}  # serve empties rather than crashing
    return _cache["data"]


def oid_int(s):
    """Convert dotted-OID string to tuple of ints for sorting."""
    return tuple(int(p) for p in s.lstrip(".").split(".") if p)


def build_table(metrics):
    """Construct the full {oid: (type, value)} mapping."""
    t = {}

    sysd = metrics.get("system", {})
    appd = metrics.get("application", {})
    webd = metrics.get("web", {})
    dbd  = metrics.get("database", {})
    opsd = metrics.get("operations", {})
    chd  = metrics.get("chaos", {})
    cnts = metrics.get("containers", [])

    # 1. avdSystem
    t[f"{BASE}.1.1.0"] = ("STRING",    sysd.get("name", "AvionDash"))
    t[f"{BASE}.1.2.0"] = ("STRING",    sysd.get("version", "1.0.0"))
    t[f"{BASE}.1.3.0"] = ("STRING",    sysd.get("environment", "demo"))
    t[f"{BASE}.1.4.0"] = ("Timeticks", str(sysd.get("uptime_ticks", 0)))
    t[f"{BASE}.1.5.0"] = ("INTEGER",   str(sysd.get("health_state", 1)))
    t[f"{BASE}.1.6.0"] = ("STRING",    sysd.get("health_message", "OK"))
    t[f"{BASE}.1.7.0"] = ("STRING",    sysd.get("last_restart", ""))

    # 2. avdApplication
    t[f"{BASE}.2.1.0"]  = ("Counter64", str(appd.get("requests_total", 0)))
    t[f"{BASE}.2.2.0"]  = ("Counter64", str(appd.get("requests_errors", 0)))
    t[f"{BASE}.2.3.0"]  = ("Gauge32",   str(appd.get("error_rate_permille", 0)))
    t[f"{BASE}.2.4.0"]  = ("Gauge32",   str(appd.get("latency_p50_ms", 0)))
    t[f"{BASE}.2.5.0"]  = ("Gauge32",   str(appd.get("latency_p95_ms", 0)))
    t[f"{BASE}.2.6.0"]  = ("Gauge32",   str(appd.get("latency_p99_ms", 0)))
    t[f"{BASE}.2.7.0"]  = ("Gauge32",   str(appd.get("active_sessions", 0)))
    t[f"{BASE}.2.8.0"]  = ("Counter64", str(appd.get("login_success", 0)))
    t[f"{BASE}.2.9.0"]  = ("Counter64", str(appd.get("login_failure", 0)))
    t[f"{BASE}.2.10.0"] = ("Gauge32",   str(appd.get("memory_used_kb", 0)))
    t[f"{BASE}.2.11.0"] = ("Gauge32",   str(appd.get("cpu_percent", 0)))
    t[f"{BASE}.2.12.0"] = ("Gauge32",   str(appd.get("thread_count", 0)))

    # 3. avdWeb
    t[f"{BASE}.3.1.0"] = ("INTEGER",   str(webd.get("status", 1)))
    t[f"{BASE}.3.2.0"] = ("Counter64", str(webd.get("requests_total", 0)))
    t[f"{BASE}.3.3.0"] = ("Gauge32",   str(webd.get("active_connections", 0)))
    t[f"{BASE}.3.4.0"] = ("Gauge32",   str(webd.get("upstream_latency_ms", 0)))
    t[f"{BASE}.3.5.0"] = ("Counter64", str(webd.get("status_2xx", 0)))
    t[f"{BASE}.3.6.0"] = ("Counter64", str(webd.get("status_4xx", 0)))
    t[f"{BASE}.3.7.0"] = ("Counter64", str(webd.get("status_5xx", 0)))
    t[f"{BASE}.3.8.0"] = ("Counter64", str(webd.get("bytes_sent", 0)))

    # 4. avdDatabase
    t[f"{BASE}.4.1.0"] = ("INTEGER",   str(dbd.get("status", 1)))
    t[f"{BASE}.4.2.0"] = ("Gauge32",   str(dbd.get("connections_active", 0)))
    t[f"{BASE}.4.3.0"] = ("Gauge32",   str(dbd.get("connections_max", 0)))
    t[f"{BASE}.4.4.0"] = ("Gauge32",   str(dbd.get("connections_waiting", 0)))
    t[f"{BASE}.4.5.0"] = ("Counter64", str(dbd.get("queries_total", 0)))
    t[f"{BASE}.4.6.0"] = ("Counter64", str(dbd.get("slow_queries", 0)))
    t[f"{BASE}.4.7.0"] = ("Gauge32",   str(dbd.get("avg_query_time_ms", 0)))
    t[f"{BASE}.4.8.0"] = ("Counter64", str(dbd.get("errors", 0)))
    t[f"{BASE}.4.9.0"] = ("STRING",    dbd.get("last_error_message", ""))

    # 5. avdContainers (TABLE)
    # avdContainerEntry .5.1.1.{col}.{idx}
    for c in cnts:
        i = c.get("index")
        if not i:
            continue
        t[f"{BASE}.5.1.1.2.{i}"]  = ("STRING",    c.get("name", ""))
        t[f"{BASE}.5.1.1.3.{i}"]  = ("STRING",    c.get("tier", ""))
        t[f"{BASE}.5.1.1.4.{i}"]  = ("STRING",    c.get("image", ""))
        t[f"{BASE}.5.1.1.5.{i}"]  = ("INTEGER",   str(c.get("status_code", 5)))
        t[f"{BASE}.5.1.1.6.{i}"]  = ("INTEGER",   str(c.get("health_code", 4)))
        t[f"{BASE}.5.1.1.7.{i}"]  = ("Gauge32",   str(c.get("cpu_percent", 0)))
        t[f"{BASE}.5.1.1.8.{i}"]  = ("Gauge32",   str(c.get("memory_used_kb", 0)))
        t[f"{BASE}.5.1.1.9.{i}"]  = ("Gauge32",   str(c.get("memory_limit_kb", 0)))
        t[f"{BASE}.5.1.1.10.{i}"] = ("Counter64", str(c.get("net_rx_bytes", 0)))
        t[f"{BASE}.5.1.1.11.{i}"] = ("Counter64", str(c.get("net_tx_bytes", 0)))
        t[f"{BASE}.5.1.1.12.{i}"] = ("Counter32", str(c.get("restarts", 0)))
        t[f"{BASE}.5.1.1.13.{i}"] = ("Timeticks", str(c.get("uptime_ticks", 0)))

    # 6. avdOperations
    t[f"{BASE}.6.1.0"]  = ("Gauge32", str(opsd.get("total_flights", 0)))
    t[f"{BASE}.6.2.0"]  = ("Gauge32", str(opsd.get("active_flights", 0)))
    t[f"{BASE}.6.3.0"]  = ("Gauge32", str(opsd.get("scheduled_flights", 0)))
    t[f"{BASE}.6.4.0"]  = ("Gauge32", str(opsd.get("boarding_flights", 0)))
    t[f"{BASE}.6.5.0"]  = ("Gauge32", str(opsd.get("delayed_flights", 0)))
    t[f"{BASE}.6.6.0"]  = ("Gauge32", str(opsd.get("cancelled_flights", 0)))
    t[f"{BASE}.6.7.0"]  = ("Gauge32", str(opsd.get("landed_flights", 0)))
    t[f"{BASE}.6.8.0"]  = ("Gauge32", str(opsd.get("on_time_permille", 0)))
    t[f"{BASE}.6.9.0"]  = ("Gauge32", str(opsd.get("total_aircraft", 0)))
    t[f"{BASE}.6.10.0"] = ("Gauge32", str(opsd.get("active_aircraft", 0)))
    t[f"{BASE}.6.11.0"] = ("Gauge32", str(opsd.get("maintenance_aircraft", 0)))
    t[f"{BASE}.6.12.0"] = ("Gauge32", str(opsd.get("grounded_aircraft", 0)))
    t[f"{BASE}.6.13.0"] = ("Gauge32", str(opsd.get("fleet_utilisation_permille", 0)))
    t[f"{BASE}.6.14.0"] = ("Gauge32", str(opsd.get("total_airports", 0)))

    # 7. avdChaos
    t[f"{BASE}.7.1.0"] = ("Gauge32", str(chd.get("active_count", 0)))
    for f in chd.get("faults", []):
        i = f.get("index")
        if not i:
            continue
        t[f"{BASE}.7.2.1.2.{i}"] = ("STRING",  f.get("key", ""))
        t[f"{BASE}.7.2.1.3.{i}"] = ("STRING",  f.get("label", ""))
        t[f"{BASE}.7.2.1.4.{i}"] = ("STRING",  f.get("tier", ""))
        t[f"{BASE}.7.2.1.5.{i}"] = ("INTEGER", str(f.get("severity_code", 1)))
        t[f"{BASE}.7.2.1.6.{i}"] = ("INTEGER", "1" if f.get("enabled") else "2")
        t[f"{BASE}.7.2.1.7.{i}"] = ("STRING",  f.get("last_change", ""))

    return t


def find_oid(table, oid, exact=True):
    """Look up exact OID or next lexical OID."""
    if exact:
        return oid if oid in table else None
    # Sort keys numerically by component
    sorted_keys = sorted(table.keys(), key=oid_int)
    target = oid_int(oid)
    for key in sorted_keys:
        if oid_int(key) > target:
            return key
    return None


def respond(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main():
    log.info("aviondash_pass started, API_URL=%s", API_URL)
    while True:
        try:
            cmd = sys.stdin.readline()
        except KeyboardInterrupt:
            break
        if not cmd:
            break
        cmd = cmd.strip()

        if cmd == "PING":
            respond("PONG")
            continue

        if cmd in ("get", "getnext"):
            try:
                oid = sys.stdin.readline().strip()
            except Exception:
                respond("NONE")
                continue
            try:
                table = build_table(fetch_metrics())
            except Exception as e:
                log.exception("build_table failed")
                respond("NONE")
                continue

            key = find_oid(table, oid, exact=(cmd == "get"))
            if not key:
                respond("NONE")
                continue
            typ, val = table[key]
            respond(key)
            respond(typ)
            respond(val)
            continue

        if cmd == "":
            continue

        # set or anything else: not supported
        respond("NONE")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("aviondash_pass crashed")
        raise
