# AvionDash SNMP Setup Guide

Complete reference for the AvionDash SNMPv3 implementation under **OID 1.3.6.1.4.1.21308** (Neomon Enterprise).

---

> **Port mapping:** Host `16100/udp` → container `161/udp` (queries). Host `16200/udp` → container `162/udp` (traps). Internal Docker services communicate on standard ports directly.

> **snmpwalk tip:** Always specify a starting OID. `snmpwalk ... localhost:16100` with no OID times out walking the full tree. Use `1.3.6.1.4.1.21308.1` as the starting point.

---

## Architecture

```
NMS / Monitoring Tool
  │  SNMPv3 GET/WALK on host port 16100
  ▼
aviondash-snmp container (Net-SNMP snmpd, port 161)
  │  pass_persist handler polls:
  ▼
http://app:8000/api/snmp/metrics  (FastAPI, internal)
  │  live JSON for all MIB groups
  ▼
MySQL 8 (database tier)
```

**SNMP Traps flow:**
```
aviondash-app container
  │  Events: fault toggle, threshold breach, business event
  │  pysnmp fires SNMPv3 INFORM to snmp-agent:162
  ▼
Your NMS trap receiver / snmptrapd
```

---

## Enterprise OID Tree

**Base:** `1.3.6.1.4.1.21308` (Neomon) → `1.3.6.1.4.1.21308.1` (aviondashMIB)

```
aviondashMIB (.1.3.6.1.4.1.21308.1)
├── avdSystem       .1   System identity & uptime
├── avdApplication  .2   FastAPI metrics (requests, errors, latency, memory, CPU)
├── avdWeb          .3   Nginx metrics (connections, status codes, bytes)
├── avdDatabase     .4   MySQL metrics (connections, queries, slow queries)
├── avdContainers   .5   Docker container table (cpu, memory, restarts)
├── avdOperations   .6   Aviation data (flights, aircraft, airports)
├── avdChaos        .7   22-fault state table
└── avdTraps       .10   17 notification definitions
```

---

## SNMPv3 Users

| User | Role | Auth | Privacy | Default Password |
|------|------|------|---------|-----------------|
| `avdread` | Read-only | SHA | AES | `avdReadAuth123` / `avdReadPriv123` |
| `avdadmin` | Read-write + traps | SHA | AES | `avdAdminAuth123` / `avdAdminPriv123` |

Change defaults in `.env` before any non-local deployment.

---

## Step 1 — Deploy

```bash
cp .env.example .env
docker compose build --no-cache snmp-agent
docker compose up -d
docker compose logs snmp-agent | grep -E "PASSED|FAILED|Starting"
```

---

## Step 2 — Install the MIB

### Linux / macOS

```bash
# Install Net-SNMP tools
sudo apt-get install snmp             # Debian/Ubuntu
sudo dnf install net-snmp-utils       # RHEL/Rocky

# Install our enterprise MIB
mkdir -p ~/.snmp/mibs
cp snmp/mibs/AVIONDASH-MIB.txt ~/.snmp/mibs/

# Configure client MIB loading
cat >> ~/.snmp/snmp.conf << 'EOF'
mibdirs +$HOME/.snmp/mibs
mibs +AVIONDASH-MIB
EOF

# Verify
snmptranslate -IR avdSysHealthState
# Expected: AVIONDASH-MIB::avdSysHealthState
```

### Windows (Net-SNMP)

Copy `snmp/mibs/AVIONDASH-MIB.txt` to `C:\usr\share\snmp\mibs\`

---

## Step 3 — Query the MIB

All examples use `localhost:16100` (host port mapped to container 161).

### System health

```bash
# Health state: 1=healthy, 2=degraded, 3=critical
snmpget -v3 -l authPriv \
  -u avdread -a SHA -A avdReadAuth123 -x AES -X avdReadPriv123 \
  localhost:16100 AVIONDASH-MIB::avdSysHealthState.0

# Health message (human-readable)
snmpget -v3 -l authPriv \
  -u avdread -a SHA -A avdReadAuth123 -x AES -X avdReadPriv123 \
  localhost:16100 AVIONDASH-MIB::avdSysHealthMessage.0
```

### Application metrics

```bash
snmpget -v3 -l authPriv \
  -u avdread -a SHA -A avdReadAuth123 -x AES -X avdReadPriv123 \
  localhost:16100 \
  AVIONDASH-MIB::avdAppRequestsTotal.0 \
  AVIONDASH-MIB::avdAppErrorRate.0 \
  AVIONDASH-MIB::avdAppLatencyP50.0 \
  AVIONDASH-MIB::avdAppLatencyP95.0 \
  AVIONDASH-MIB::avdAppLatencyP99.0 \
  AVIONDASH-MIB::avdAppMemoryUsedKB.0 \
  AVIONDASH-MIB::avdAppCpuPercent.0
```

### Web tier (Nginx)

```bash
snmpget -v3 -l authPriv \
  -u avdread -a SHA -A avdReadAuth123 -x AES -X avdReadPriv123 \
  localhost:16100 \
  AVIONDASH-MIB::avdWebStatus.0 \
  AVIONDASH-MIB::avdWebRequestsTotal.0 \
  AVIONDASH-MIB::avdWebStatus5xx.0
```

### Database tier

```bash
snmpget -v3 -l authPriv \
  -u avdread -a SHA -A avdReadAuth123 -x AES -X avdReadPriv123 \
  localhost:16100 \
  AVIONDASH-MIB::avdDbStatus.0 \
  AVIONDASH-MIB::avdDbConnectionsActive.0 \
  AVIONDASH-MIB::avdDbSlowQueries.0 \
  AVIONDASH-MIB::avdDbAvgQueryTimeMs.0
```

### Container table

```bash
# Walk all three containers (index 1=db, 2=app, 3=web)
snmpwalk -v3 -l authPriv \
  -u avdread -a SHA -A avdReadAuth123 -x AES -X avdReadPriv123 \
  localhost:16100 AVIONDASH-MIB::avdContainerTable

# Get app container CPU specifically
snmpget -v3 -l authPriv \
  -u avdread -a SHA -A avdReadAuth123 -x AES -X avdReadPriv123 \
  localhost:16100 \
  AVIONDASH-MIB::avdContainerCpuPercent.2 \
  AVIONDASH-MIB::avdContainerMemUsedKB.2 \
  AVIONDASH-MIB::avdContainerRestarts.2
```

### Aviation operations

```bash
snmpget -v3 -l authPriv \
  -u avdread -a SHA -A avdReadAuth123 -x AES -X avdReadPriv123 \
  localhost:16100 \
  AVIONDASH-MIB::avdOpsTotalFlights.0 \
  AVIONDASH-MIB::avdOpsActiveFlights.0 \
  AVIONDASH-MIB::avdOpsDelayedFlights.0 \
  AVIONDASH-MIB::avdOpsCancelledFlights.0 \
  AVIONDASH-MIB::avdOpsOnTimeRate.0 \
  AVIONDASH-MIB::avdOpsFleetUtilisation.0
```

### Chaos engine state (all 22 faults)

```bash
# Count of active faults
snmpget -v3 -l authPriv \
  -u avdread -a SHA -A avdReadAuth123 -x AES -X avdReadPriv123 \
  localhost:16100 AVIONDASH-MIB::avdChaosActiveFaults.0

# Walk full fault table
snmpwalk -v3 -l authPriv \
  -u avdread -a SHA -A avdReadAuth123 -x AES -X avdReadPriv123 \
  localhost:16100 AVIONDASH-MIB::avdChaosTable
```

### Full MIB walk

```bash
snmpwalk -v3 -l authPriv \
  -u avdread -a SHA -A avdReadAuth123 -x AES -X avdReadPriv123 \
  localhost:16100 1.3.6.1.4.1.21308.1
```

---

## Step 4 — SNMP Traps

### 17 Trap definitions

| OID Suffix | Trap Name | Fired By |
|-----------|-----------|---------|
| .10.0.1 | avdTrapAppDown | App health check fails |
| .10.0.2 | avdTrapAppUp | App recovers |
| .10.0.3 | avdTrapContainerRestart | Any container restarts |
| .10.0.4 | avdTrapContainerUnhealthy | health_check_fail fault |
| .10.0.10 | avdTrapHighErrorRate | high_error_rate, random_500s, **http_500_storm**, **payload_corruption** |
| .10.0.11 | avdTrapLatencyDegraded | latency_spike, **timeout_cascade** |
| .10.0.12 | avdTrapHighMemoryUsage | memory_leak, container_oom_simulation |
| .10.0.13 | avdTrapHighCpuUsage | cpu_spike, **container_cpu_throttle** |
| .10.0.20 | avdTrapDbDown | network_partition |
| .10.0.21 | avdTrapDbSlowQueries | slow_queries, n_plus_one |
| .10.0.22 | avdTrapDbPoolExhausted | db_pool_exhaustion |
| .10.0.30 | avdTrapFlightCancelled | **flight_status_chaos** |
| .10.0.31 | avdTrapAircraftGrounded | future use |
| .10.0.40 | avdTrapFaultActivated | **every fault activation** |
| .10.0.41 | avdTrapFaultDeactivated | **every fault deactivation** |
| .10.0.42 | avdTrapCascadingFailure | cascading_failure fault |
| .10.0.50 | avdTrapAuthFailureBurst | **auth_failure_burst** fault |

*Bold = new faults added in this release.*

### Configure trap destination

In `.env`:
```env
SNMP_TRAP_HOST=your-nms-host.example.com
SNMP_TRAP_PORT=162
SNMP_TRAP_USER=avdadmin
SNMP_TRAP_AUTH_PASS=avdAdminAuth123
SNMP_TRAP_PRIV_PASS=avdAdminPriv123
SNMP_TRAPS_ENABLED=true
```

### Receive traps locally (testing)

```bash
sudo snmptrapd -f -Lo -v3 \
  -u avdadmin -a SHA -A avdAdminAuth123 \
  -x AES -X avdAdminPriv123 \
  0.0.0.0:16200
```

### Fire all traps at once (SNMP Trap Test fault)

Enable `snmp_trap_test` in the Chaos Control UI. All 17 trap types fire in sequence with a 300ms delay between each. Use this to validate your NMS is correctly receiving and decoding all AvionDash traps before a live demo.

---

## Step 5 — Monitoring Patterns

### Polling intervals

| Group | Interval | Rationale |
|-------|---------|-----------|
| avdSysHealthState | 30s | Core health — fast alerting |
| avdApplication | 30s | Request metrics change quickly |
| avdChaosActiveFaults | 15s | Fault state should be near-real-time |
| avdDatabase | 30s | DB issues need fast detection |
| avdContainerTable | 60s | Docker stats moderate churn |
| avdOperations | 120s | Flight data changes slowly |

### Alert thresholds

| OID | Metric | Warning | Critical |
|-----|--------|---------|---------|
| avdAppErrorRate | Error rate (permille) | > 100 | > 300 |
| avdAppLatencyP95 | P95 latency (ms) | > 1000 | > 3000 |
| avdAppCpuPercent | App CPU % | > 60 | > 80 |
| avdAppMemoryUsedKB | App memory KB | > 300000 | > 400000 |
| avdAppLoginFailure | Login failures | delta > 5 | delta > 20 |
| avdDbStatus | DB status | — | ≠ 1 |
| avdDbAvgQueryTimeMs | Query time (ms) | > 500 | > 2000 |
| avdContainerCpuPercent | Container CPU | > 60 | > 80 |
| avdContainerRestarts | Restarts | delta ≥ 1 | delta ≥ 3 |
| avdChaosActiveFaults | Active faults | ≥ 1 | ≥ 3 |
| avdOpsCancelledFlights | Cancelled flights | delta > 2 | delta > 5 |

### Computed metrics

```
Error rate %  = avdAppErrorRate / 1000 * 100
Fleet util %  = avdOpsFleetUtilisation / 1000 * 100
DB pool util  = avdDbConnectionsActive / avdDbConnectionsMax * 100
On-time rate  = avdOpsOnTimeRate / 1000 * 100
Chaos coverage = avdChaosActiveFaults / 22 * 100
```

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `Timeout` | Stale Docker image | `docker compose build --no-cache snmp-agent && docker compose up -d snmp-agent` |
| `No response from 16100` | Container not running | `docker compose ps` — is aviondash-snmp up? |
| `Authentication failure` | Wrong passphrase | Check `.env` vars match `snmpget` flags |
| `Unknown Object Identifier` | MIB not loaded | Copy `AVIONDASH-MIB.txt` to `~/.snmp/mibs/` |
| `NONE` on all AVD OIDs | pass_persist can't reach app | `docker compose logs snmp-agent` — check connection errors |
| Traps not arriving | Wrong SNMP_TRAP_HOST | Set to your NMS IP, then `docker compose up -d app` |
| `Bad operator` warning | SNMPv2-PDU loaded | Rebuild with `--no-cache` to get updated snmp.conf |
| `avdOpsCancelledFlights` spiked | flight_status_chaos active | Disable fault + reset all faults |
