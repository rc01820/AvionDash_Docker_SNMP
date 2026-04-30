# AvionDash Docker — Datadog Setup Guide

Complete reference for instrumenting AvionDash in Datadog. Covers APM, Log Management, Infrastructure Metrics, Monitors (including the 8 new faults), Synthetics, Dashboards, and SLOs.

---

## Prerequisites

- Datadog account (free trial works)
- AvionDash running: `docker compose up -d --build`
- Datadog API key from **Organization Settings → API Keys**

---

## Step 1 — Enable the Datadog Agent

### 1.1 Add your API key to `.env`
```env
DD_API_KEY=your_api_key_here
DD_SITE=datadoghq.com
```

### 1.2 Uncomment the agent block in `docker-compose.yml`
Find the commented `datadog-agent` block and uncomment it.

### 1.3 Enable APM tracing in the app
```yaml
DD_TRACE_ENABLED: "true"
```

### 1.4 Restart
```bash
docker compose up -d
docker compose exec datadog-agent agent status
```

---

## Step 2 — APM

Auto-instrumented via `ddtrace`:
- FastAPI request/response spans
- SQLAlchemy query spans (with SQL text)
- All requests tagged: `service:aviondash-app-docker`, `env:demo`, `version:1.0.0`

### Generate traffic
```bash
TOKEN=$(curl -s -X POST http://localhost/api/auth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=aviondash123" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

for ep in /api/dashboard/summary /api/flights/ /api/aircraft/ /api/airports/; do
  curl -s -H "Authorization: Bearer $TOKEN" "http://localhost$ep" > /dev/null
done
```

---

## Step 3 — Log Pipelines

**Pipeline 1 — Nginx** (filter: `source:nginx`)
- JSON Parser on `message`
- Status Remapper: `status` → `http.status_code`
- Duration Remapper: `request_time` → `duration`

**Pipeline 2 — FastAPI** (filter: `service:aviondash-app-docker`)
- Grok: `%{TIMESTAMP_ISO8601:timestamp} %{LOGLEVEL:level} \[%{DATA:logger}\] %{GREEDYDATA:message}`
- Status Remapper: `level` → log status
- Trace ID Remapper: link logs to APM

### Key log queries
```
# All fault activations
service:aviondash-app-docker "[FAULT]"

# 500 storm activity
service:aviondash-app-docker "[FAULT] http_500_storm"

# Auth failure burst
service:aviondash-app-docker "[FAULT] auth_failure_burst"

# Payload corruption events
service:aviondash-app-docker "[FAULT] payload_corruption"

# Timeout cascade
service:aviondash-app-docker "[FAULT] timeout_cascade"

# All 5xx from Nginx
source:nginx @http.status_code:[500 TO 599]
```

---

## Step 4 — Monitors (All 22 Faults)

### Application Tier Monitors

---

**Monitor 1 — High API Error Rate** *(slow_queries, high_error_rate, random_500s, http_500_storm)*
```
Type:  Metric Alert
Query: sum(last_5m):sum:trace.fastapi.request.errors{service:aviondash-app-docker}.as_rate() > 0.30
Alert: > 30%  |  Warning: > 10%
Tags:  service:aviondash-app-docker, env:demo, tier:application
Message:
  🚨 API error rate is {{value}}%.
  Active fault candidates: high_error_rate, random_500s, http_500_storm
  Check: http://localhost/#chaos
```

---

**Monitor 2 — HTTP 500 Storm** *(http_500_storm — new)*
```
Type:  Metric Alert
Query: sum(last_2m):sum:trace.fastapi.request.errors{service:aviondash-app-docker}.as_rate() > 0.80
Alert: > 80% error rate
Tags:  service:aviondash-app-docker, env:demo, tier:application
Message:
  🔥 CRITICAL: Error rate is {{value}}% — HTTP 500 storm likely active.
  Disable at: http://localhost/#chaos
```

---

**Monitor 3 — P99 Latency Anomaly** *(latency_spike, timeout_cascade)*
```
Type:  Anomaly Alert
Query: anomalies(avg:trace.fastapi.request.duration{service:aviondash-app-docker}, 'agile', 3)
Tags:  service:aviondash-app-docker, env:demo
```

---

**Monitor 4 — Timeout Cascade (504)** *(timeout_cascade — new)*
```
Type:  Log Alert
Query: logs("source:nginx @http.status_code:504").rollup("count").last("5m") > 3
Tags:  service:aviondash-web-docker, env:demo
Message:
  ⏱️ Nginx returning 504 Gateway Timeout — timeout_cascade fault may be active.
```

---

**Monitor 5 — Slow DB Queries** *(slow_queries, n_plus_one, timeout_cascade)*
```
Type:  Metric Alert
Query: avg(last_5m):avg:db.query.duration{service:aviondash-app-docker} > 3000
Alert: > 3000ms  |  Warning: > 1000ms
```

---

**Monitor 6 — DB Pool Exhaustion** *(db_pool_exhaustion)*
```
Type:  Composite
Formula: monitor_3 AND monitor_5
(Latency anomaly AND slow DB = pool likely exhausted)
```

---

**Monitor 7 — Auth Failure Burst** *(auth_failure_burst — new)*
```
Type:  Log Alert
Query: logs("service:aviondash-app-docker \"Failed login\" OR \"Authentication service unavailable\"").rollup("count").last("5m") > 10
Tags:  service:aviondash-app-docker, env:demo, tier:security
Message:
  🔐 High login failure rate: {{value}} failures in 5 minutes.
  auth_failure_burst fault may be active, or genuine brute-force attempt.
```

---

**Monitor 8 — Payload Corruption** *(payload_corruption — new)*
```
Type:  Log Alert
Query: logs("service:aviondash-app-docker \"[FAULT] payload_corruption\"").rollup("count").last("5m") > 0
Tags:  service:aviondash-app-docker, env:demo
Message:
  💥 Payload corruption fault active — JSON responses are being corrupted.
  Clients will see parse errors. Check RUM for SyntaxError events.
```

---

**Monitor 9 — Log Volume Anomaly** *(log_flood)*
```
Type:  Log Alert
Query: logs("service:aviondash-app-docker status:warn").rollup("count").last("5m") > 500
```

---

**Monitor 10 — Flight Cancellation Spike** *(flight_status_chaos — new)*
```
Type:  Log Alert
Query: logs("service:aviondash-app-docker \"[FAULT][FLIGHT_STATUS]\"").rollup("count").last("5m") > 0
Tags:  service:aviondash-app-docker, env:demo, tier:business
Message:
  ✈️ Flight status chaos active — mass cancellations/delays injected into DB.
  Dashboard cancelled/delayed KPIs will show anomalous spikes.
```

---

### Container Tier Monitors

---

**Monitor 11 — App Container CPU** *(cpu_spike, container_cpu_throttle)*
```
Type:  Metric Alert
Query: avg(last_5m):avg:container.cpu.usage{container_name:aviondash-app-docker} > 80
Alert: > 80%  |  Warning: > 60%
```

---

**Monitor 12 — Container CPU Throttle** *(container_cpu_throttle — new)*
```
Type:  Metric Alert
Query: avg(last_3m):avg:container.cpu.usage{container_name:aviondash-app-docker} > 90
Alert: > 90% sustained
Message:
  🔥 Sustained CPU > 90% on aviondash-app-docker.
  container_cpu_throttle or cpu_spike fault may be active.
  This is distinct from request-correlated cpu_spike — 4 background threads running.
```

---

**Monitor 13 — Memory High** *(memory_leak, container_oom_simulation)*
```
Type:  Metric Alert
Query: avg(last_5m):avg:container.memory.usage{container_name:aviondash-app-docker} > 400000000
Alert: > 400 MB  |  Warning: > 300 MB
```

---

**Monitor 14 — Memory Leak Forecast** *(memory_leak, container_oom_simulation)*
```
Type:  Forecast Alert
Query: forecast(max:container.memory.usage{container_name:aviondash-app-docker}, 'linear', 1) > 500000000
Window: 1 hour
```

---

**Monitor 15 — Container Restart** *(health_check_fail)*
```
Type:  Event Alert
Query: events("sources:docker tags:container_name:aviondash-app-docker status:error").rollup("count").last("5m") > 0
```

---

**Monitor 16 — DB Container Down** *(network_partition)*
```
Type:  Metric Alert
Query: avg(last_2m):avg:container.running{container_name:aviondash-db-docker} < 1
```

---

**Monitor 17 — Disk Usage** *(disk_fill)*
```
Type:  Metric Alert
Query: max(last_5m):max:disk.in_use{host:aviondash-app-docker} > 0.85
Alert: > 85%  |  Warning: > 75%
```

---

**Monitor 18 — Nginx 5xx Rate** *(http_500_storm, timeout_cascade, high_error_rate)*
```
Type:  Log Alert
Query: logs("source:nginx @http.status_code:[500 TO 599]").rollup("count").last("5m") > 20
```

---

### Composite Monitors

---

**Monitor 19 — Cascading Failure P1** *(cascading_failure)*
```
Type:    Composite
Formula: monitor_1 AND monitor_3 AND monitor_9
         (Error Rate AND Latency AND Log Flood)
Priority: P1 — Critical
Message:
  🚨 P1 — CASCADING FAILURE DETECTED on AvionDash.
  Multiple signals firing simultaneously across all tiers.
  Immediate action: check Chaos Control and correlate APM service map.
```

---

**Monitor 20 — Container Resource Pressure** *(container_cpu_throttle + memory_leak)*
```
Type:    Composite
Formula: monitor_12 AND monitor_13
         (CPU Throttle AND Memory High)
Message:
  Resource pressure across CPU and memory simultaneously.
  Possible faults: container_cpu_throttle + memory_leak or container_oom_simulation.
```

---

**Monitor 21 — Security + Availability** *(auth_failure_burst + http_500_storm)*
```
Type:    Composite
Formula: monitor_7 AND monitor_2
         (Auth Failures AND 500 Storm)
Message:
  🔐🔥 Both auth failures and high error rate firing simultaneously.
  Could indicate credential stuffing attack during an outage.
```

---

## Step 5 — Synthetic Tests

### Test 1 — Login Flow
```
Name:   AvionDash Login End-to-End
Type:   API (multi-step)
Step 1: POST /api/auth/token → assert status=200, extract access_token
Step 2: GET /api/dashboard/summary (with token) → assert status=200, latency<2s
Step 3: GET /api/flights/ (with token) → assert status=200
Schedule: Every 5 minutes
Locations: aws:us-east-1, aws:eu-west-1
Alert: 2 consecutive failures
```

### Test 2 — Health Endpoint
```
URL:    http://your-host/health
Assert: status=200, body contains "ok", latency<500ms
Schedule: Every 1 minute
Alert: 2 failures
```

### Test 3 — API Availability Under Fault
```
Name:   AvionDash API Availability (fault-aware)
Type:   API
Steps:  GET /api/flights/ → assert status NOT 500
        (This test will fail when http_500_storm or high_error_rate is active —
         useful to verify fault → alert → recovery flow)
```

---

## Step 6 — SLOs

### SLO 1 — API Availability
```
Type:   Monitor-based
Monitor: Monitor 1 (High Error Rate)
Target: 99.5% over 30 days  (3.65 hours/month budget)
Burn-rate alerts:
  Fast: 14.4x rate over 1h  (2% of monthly budget in 1 hour)
  Slow: 6x rate over 6h     (5% of monthly budget in 6 hours)
```

### SLO 2 — API Latency
```
Type:   Monitor-based
Monitor: Monitor 3 (P99 Latency Anomaly)
Target: 95% of time within normal latency bounds
Window: 7 days rolling
```

---

## Step 7 — Dashboard Layout

**Row 1 — Request Health**
- Timeseries: Request rate, error rate (all 22 fault types affect this)
- Timeseries: P50/P95/P99 latency
- Query Value: Active chaos faults (from SNMP `avdChaosActiveFaults`)

**Row 2 — Container Resources**
- Timeseries: `container.cpu.usage` by `container_name`
  *(watch this during cpu_spike and container_cpu_throttle)*
- Timeseries: `container.memory.usage` by `container_name`
  *(watch during memory_leak and container_oom_simulation)*

**Row 3 — Database**
- Timeseries: `db.query.duration` *(spikes during slow_queries, timeout_cascade)*
- Timeseries: `db.query.count` *(N+1 pattern visible here)*
- Query Value: DB error count

**Row 4 — Business Operations**
- Query Value: `avdOpsCancelledFlights` from SNMP
  *(spikes during flight_status_chaos)*
- Query Value: `avdAppLoginFailure` from SNMP
  *(spikes during auth_failure_burst)*

**Row 5 — Chaos State**
- Event timeline: `service:aviondash-app-docker "[FAULT]"`
- Log stream: real-time fault activation log

---

## Fault → Signal Quick Reference

| Fault | Primary Datadog Signal | SNMP Trap |
|-------|----------------------|-----------|
| slow_queries | APM `db.query.duration` | avdTrapDbSlowQueries |
| high_error_rate | `service.error.rate` | avdTrapHighErrorRate |
| random_500s | SLO burn rate | avdTrapHighErrorRate |
| latency_spike | APM P99 anomaly | avdTrapLatencyDegraded |
| memory_leak | `container.memory.usage` forecast | avdTrapHighMemoryUsage |
| cpu_spike | `container.cpu.usage` | avdTrapHighCpuUsage |
| n_plus_one | APM flame graph | avdTrapDbSlowQueries |
| db_pool_exhaustion | DB connection wait | avdTrapDbPoolExhausted |
| log_flood | Log volume anomaly | — |
| **http_500_storm** | Error rate > 80% | avdTrapHighErrorRate |
| **auth_failure_burst** | 401 burst in logs | avdTrapAuthFailureBurst |
| **payload_corruption** | RUM parse errors | avdTrapHighErrorRate |
| **timeout_cascade** | Nginx 504 count | avdTrapLatencyDegraded |
| **flight_status_chaos** | Business KPI spike | avdTrapFlightCancelled |
| health_check_fail | Container unhealthy | avdTrapContainerUnhealthy |
| container_oom_simulation | Memory forecast | avdTrapHighMemoryUsage |
| network_partition | DB error propagation | avdTrapDbDown |
| disk_fill | `disk.in_use` | — |
| **container_cpu_throttle** | Sustained CPU > 90% | avdTrapHighCpuUsage |
| **snmp_trap_test** | — (observability only) | ALL 17 traps |
| cascading_failure | Composite P1 | avdTrapCascadingFailure |

*Bold = new faults added in this release.*
