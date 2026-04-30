# AvionDash — Fault Scenarios

Complete reference for all **22 chaos fault injections** across three tiers. Each fault is designed to generate specific, observable signals in Datadog and/or fire SNMP traps to your NMS.

---

## How the Chaos Engine Works

Faults are stored in a shared in-memory dictionary (`FAULT_STATE`) inside the FastAPI process. An HTTP middleware inspects state on every request and applies behaviour before the route handler runs. Container-tier faults also spawn background threads. **Every fault activation fires an SNMP trap** (`avdTrapFaultActivated`) plus a tier-specific trap if defined.

### Enable via UI
Navigate to **Chaos Control** (admin role required). Toggle any fault card. The SNMP trap name is shown on each card in purple.

### Enable via API
```bash
TOKEN=$(curl -s -X POST http://localhost/api/auth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=aviondash123" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# Activate a fault
curl -X POST http://localhost/api/chaos/slow_queries/toggle \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# Reset all
curl -X POST http://localhost/api/chaos/reset-all \
  -H "Authorization: Bearer $TOKEN"
```

---

## APPLICATION TIER FAULTS

---

### 1. Slow DB Queries
**Key:** `slow_queries` | **Severity:** WARNING | **SNMP Trap:** `avdTrapDbSlowQueries`

Injects 3–8s `time.sleep()` before DB operations on `/api/flights/`.

**Signals:** APM `db.query.duration` anomaly, P99 latency spike, slow spans in flame graph.

**Monitor:**
```
avg(last_5m):avg:trace.fastapi.request.duration{resource_name:GET /api/flights/} > 3000
```

---

### 2. High Error Rate (503)
**Key:** `high_error_rate` | **Severity:** CRITICAL | **SNMP Trap:** `avdTrapHighErrorRate`

Returns HTTP 503 on 60% of all non-health requests.

**Signals:** `service.error.rate` → 60%, service map turns red, RUM fetch errors.

**Monitor:**
```
sum(last_5m):sum:trace.fastapi.request.errors{service:aviondash-app}.as_rate() > 0.5
```

---

### 3. Random 500 Errors
**Key:** `random_500s` | **Severity:** WARNING | **SNMP Trap:** `avdTrapHighErrorRate`

Returns HTTP 500 on a random 35% of requests. Intermittent — harder to reproduce manually.

**Signals:** SLO error budget burn-rate alert fires before threshold alert would.

---

### 4. Latency Spike
**Key:** `latency_spike` | **Severity:** WARNING | **SNMP Trap:** `avdTrapLatencyDegraded`

Adds 2–6s synchronous sleep on every request including the login endpoint.

**Monitor:**
```
anomalies(avg:trace.fastapi.request.duration{service:aviondash-app}, 'agile', 3)
```

---

### 5. Memory Leak
**Key:** `memory_leak` | **Severity:** CRITICAL | **SNMP Trap:** `avdTrapHighMemoryUsage`

Allocates 512 KB per request and holds it in `app.state.leak` without releasing.

**Monitor:**
```
forecast(max:container.memory.usage{container_name:aviondash-app}, 'linear', 1) > 500000000
```

---

### 6. CPU Spike
**Key:** `cpu_spike` | **Severity:** WARNING | **SNMP Trap:** `avdTrapHighCpuUsage`

Burns 300ms CPU per request via busy-wait loop.

**Monitor:**
```
avg(last_5m):avg:container.cpu.usage{container_name:aviondash-app} > 80
```

---

### 7. N+1 Query Pattern
**Key:** `n_plus_one` | **Severity:** WARNING | **SNMP Trap:** `avdTrapDbSlowQueries`

Issues one extra `SELECT COUNT(*)` per flight row. A request returning 25 flights generates 26 queries.

**Best for:** Live APM demo — flame graph shows dozens of stacked DB spans.

---

### 8. DB Pool Exhaustion
**Key:** `db_pool_exhaustion` | **Severity:** CRITICAL | **SNMP Trap:** `avdTrapDbPoolExhausted`

Holds DB connections open for 5–12s per request, exhausting the 30-connection pool.

**Signals:** `QueuePool limit of size 10 overflow 20 reached` errors, high latency variance.

---

### 9. Log Flood
**Key:** `log_flood` | **Severity:** WARNING | **SNMP Trap:** None

Emits 50 WARNING log lines per request (~500 lines/sec under load).

**Monitor:**
```
logs("service:aviondash-app status:warn").rollup("count").last("5m") > 500
```

---

### 10. HTTP 500 Storm ⚡ NEW
**Key:** `http_500_storm` | **Severity:** CRITICAL | **SNMP Trap:** `avdTrapHighErrorRate`

Returns HTTP 500 on **90%** of requests — more aggressive than `random_500s`. Simulates a crash loop. Fires `avdTrapHighErrorRate` immediately on activation.

**Signals generated:**
- Error rate jumps to ~90% instantly — visible in APM within 30 seconds
- SLO error budget exhausted in minutes
- `avdTrapHighErrorRate` SNMP trap with `avdAppErrorRate=900` (permille)
- Service map shows aviondash-app entirely red

**Best for:** Demonstrating P1 incident response. Combine with `cascading_failure` for maximum drama.

**Monitor:**
```
sum(last_5m):sum:trace.fastapi.request.errors{service:aviondash-app}.as_rate() > 0.80
```

---

### 11. Auth Failure Burst ⚡ NEW
**Key:** `auth_failure_burst` | **Severity:** WARNING | **SNMP Trap:** `avdTrapAuthFailureBurst`

Rejects **all login attempts** with HTTP 401, simulating a broken auth provider or misconfigured SSO. Fires `avdTrapAuthFailureBurst` on activation.

**Signals generated:**
- Every login attempt returns 401 immediately
- `avdAppLoginFailure` counter increments rapidly
- `avdTrapAuthFailureBurst` SNMP trap fires with current failure count
- Security monitors alert on burst of 401s

**Demo script:** Enable the fault, then try logging in from the UI. The SNMP trap fires within 1 second of activation.

---

### 12. Payload Corruption ⚡ NEW
**Key:** `payload_corruption` | **Severity:** WARNING | **SNMP Trap:** `avdTrapHighErrorRate`

Randomly replaces 20% of `/api/flights/` and `/api/aircraft/` response bodies with malformed JSON (`CORRUPTED_JSON_FAULT` + binary garbage). The HTTP status remains 200 — the error only appears client-side when the browser tries to `JSON.parse()` the response.

**Signals generated:**
- Flights and Aircraft tables show parse errors in the UI
- RUM: `SyntaxError: Unexpected token` increases
- Browser console shows JSON parse failures
- APM: 200 responses with client errors (hard to detect without RUM)

**Best for:** Demonstrating why RUM is needed alongside backend APM.

---

### 13. Timeout Cascade ⚡ NEW
**Key:** `timeout_cascade` | **Severity:** CRITICAL | **SNMP Trap:** `avdTrapLatencyDegraded`

Injects 28–32s sleeps on all `/api/*` requests — long enough to exceed Nginx's `proxy_read_timeout 120s` in a sustained way and cause Nginx to return HTTP 504 Gateway Timeout to clients. Models a real cascade where a slow DB causes request queuing which causes timeouts which causes more retries.

**Signals generated:**
- Nginx returns 504 to browsers after proxy timeout
- `avdWebStatus5xx` counter climbs
- APM traces show requests hitting the timeout boundary
- `avdTrapLatencyDegraded` SNMP trap fires with P95 > 28,000ms

**Recovery:** Disable fault. In-flight sleeping requests finish naturally; no restart needed.

---

### 14. Flight Status Spike ⚡ NEW
**Key:** `flight_status_chaos` | **Severity:** WARNING | **SNMP Trap:** `avdTrapFlightCancelled`

Executes direct SQL `UPDATE` statements to mass-cancel 3 flights and delay 5 more, causing the operations dashboard to show a sudden spike in cancellations. Fires `avdTrapFlightCancelled` SNMP trap immediately.

**Signals generated:**
- Dashboard `Cancelled` KPI jumps by 3
- Dashboard `Delayed` KPI jumps by 5
- `avdOpsCancelledFlights` in SNMP MIB reflects new value
- `avdTrapFlightCancelled` fires with varbinds: cancelled count + total flights

**Recovery:** Disable the fault. The reset restores affected flights to scheduled status (up to 8 rows).

**Best for:** Demonstrating business-level SNMP monitoring — the trap fires not from an infrastructure event but from an application-domain state change.

---

## CONTAINER / INFRASTRUCTURE TIER FAULTS

---

### 15. Health Check Failure
**Key:** `health_check_fail` | **Severity:** CRITICAL | **SNMP Trap:** `avdTrapContainerUnhealthy`

Returns HTTP 503 from `/health`. Docker marks container unhealthy after 3 failed checks and restarts it. Fires `avdTrapContainerUnhealthy` on activation.

**Note:** Container restart clears all in-memory fault state. Other active faults will be reset.

---

### 16. Container OOM Simulation
**Key:** `container_oom_simulation` | **Severity:** CRITICAL | **SNMP Trap:** `avdTrapHighMemoryUsage`

Background thread allocates 5MB chunks every second (~300 MB/minute).

**Growth rate:** 5 MB/s → container OOM-kill if `mem_limit` is set in compose.

---

### 17. Network Partition
**Key:** `network_partition` | **Severity:** CRITICAL | **SNMP Trap:** `avdTrapDbDown`

Disposes the SQLAlchemy connection pool. All DB-dependent endpoints return 500. Fires `avdTrapDbDown` on activation.

**Recovery:** Disable fault. SQLAlchemy reconnects automatically on next request.

---

### 18. Disk Fill
**Key:** `disk_fill` | **Severity:** WARNING | **SNMP Trap:** None

Background thread writes 100KB chunks to `/var/log/aviondash/disk_fill.log` at ~500 KB/s. File is deleted on disable.

---

### 19. Container CPU Throttle ⚡ NEW
**Key:** `container_cpu_throttle` | **Severity:** WARNING | **SNMP Trap:** `avdTrapHighCpuUsage`

Spawns **4 CPU-burning threads** that run at full speed continuously, saturating available CPU cores. Unlike `cpu_spike` (which burns per-request), this is constant background pressure regardless of traffic. Fires `avdTrapHighCpuUsage` on activation.

**Signals generated:**
- `container.cpu.usage` climbs to 80–100% within seconds
- Response latency increases as CPU can't service requests
- Process agent shows Python process at top of CPU rankings
- `avdTrapHighCpuUsage` SNMP trap fires immediately

**Best for:** Demonstrating the difference between request-correlated CPU (cpu_spike) vs background CPU pressure (noisy neighbour).

---

## SNMP & OBSERVABILITY TIER FAULTS

---

### 20. SNMP Trap Test ⚡ NEW
**Key:** `snmp_trap_test` | **Severity:** WARNING | **SNMP Trap:** ALL (17 traps fired sequentially)

Fires **all 17 SNMP trap types** in sequence with realistic varbind values. Does NOT affect application behaviour or metrics — it is a pure observability test. Each trap is separated by 300ms to allow NMS processing.

**Traps fired in sequence:**
1. `avdTrapAppDown` — simulates application outage
2. `avdTrapAppUp` — simulates recovery
3. `avdTrapContainerRestart` — container restarted
4. `avdTrapContainerUnhealthy` — health check failing
5. `avdTrapHighErrorRate` — error rate 75%
6. `avdTrapLatencyDegraded` — P95 3500ms, P99 6000ms
7. `avdTrapHighMemoryUsage` — approaching memory limit
8. `avdTrapHighCpuUsage` — CPU at 92%
9. `avdTrapDbDown` — database unreachable
10. `avdTrapDbSlowQueries` — avg query 4500ms
11. `avdTrapDbPoolExhausted` — all 30 connections in use
12. `avdTrapFlightCancelled` — 5 flights cancelled
13. `avdTrapAircraftGrounded` — 2 aircraft grounded
14. `avdTrapFaultActivated` — fault engine event
15. `avdTrapFaultDeactivated` — fault engine event
16. `avdTrapCascadingFailure` — 5 simultaneous faults
17. `avdTrapAuthFailureBurst` — security event

**How to receive traps:**
```bash
# Run snmptrapd on your workstation (port 16200 from host)
sudo snmptrapd -f -Lo -v3 \
  -u avdadmin -a SHA -A avdAdminAuth123 \
  -x AES -X avdAdminPriv123 \
  0.0.0.0:162
```

**Best for:** NMS integration testing. Enable this fault once after deploying to verify every trap type is correctly received and decoded before running a live demo.

---

### 21. Cascading Failure ⚡
**Key:** `cascading_failure` | **Severity:** CRITICAL | **SNMP Trap:** `avdTrapCascadingFailure`

Meta-fault that simultaneously activates 5 faults: `slow_queries` + `high_error_rate` + `latency_spike` + `log_flood` + `http_500_storm`. Fires `avdTrapCascadingFailure` SNMP trap with active fault count and health state.

**All signals generated simultaneously:**
- Error rate climbs to 60–90%
- P99 latency > 6 seconds
- DB query times > 5 seconds
- Log volume 50x normal
- `avdTrapCascadingFailure` SNMP trap
- Composite Datadog monitor fires

---

## Recommended Demo Sequences

### Sequence A: SNMP Validation (5 min — run first)
1. Enable `snmp_trap_test`
2. Verify all 17 traps appear in your NMS
3. Disable — no app impact

### Sequence B: HTTP Error Escalation (10 min)
1. Enable `random_500s` → show 35% error rate, SLO burn
2. Enable `http_500_storm` → escalate to 90% error rate, P1 alert
3. Reset → show recovery in APM

### Sequence C: Business Impact (10 min)
1. Enable `flight_status_chaos` → show SNMP `avdTrapFlightCancelled`
2. Enable `auth_failure_burst` → show `avdTrapAuthFailureBurst`
3. Show dashboard KPI changes alongside SNMP MIB walk

### Sequence D: Full Outage (20 min)
1. Open: APM Service Map + Log Explorer + SNMP MIB walk in terminal
2. Enable `cascading_failure`
3. Walk through Datadog incident timeline + SNMP trap stream
4. Reset All → show recovery

### Sequence E: Infrastructure Pressure (15 min)
1. Enable `container_cpu_throttle` → sustained CPU pressure
2. Enable `memory_leak` → growing memory
3. Enable `container_oom_simulation` → accelerate memory
4. Watch `avdTrapHighCpuUsage` + `avdTrapHighMemoryUsage` traps
5. Reset All

### Sequence F: Data Integrity (10 min)
1. Enable `payload_corruption` → silent data corruption
2. Enable `timeout_cascade` → Nginx 504 errors
3. Show how RUM catches what APM misses (corruption) and what Nginx sees (timeouts)
