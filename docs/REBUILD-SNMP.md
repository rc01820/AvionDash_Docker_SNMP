# Force Rebuild SNMP Agent

If the snmp-agent container is using a stale image (common after config changes),
run this to force a complete rebuild:

```bash
# Stop and remove the old container + image
docker compose down
docker rmi avion-snmp-snmp-agent 2>/dev/null || true
docker rmi aviondash-snmp 2>/dev/null || true

# Rebuild with no cache
docker compose build --no-cache snmp-agent

# Start everything
docker compose up -d

# Watch the snmp-agent startup (should show self-test PASSED)
docker compose logs -f snmp-agent
```

## Verify it's working

```bash
# 1. Container should show "Self-test PASSED" in logs
docker compose logs snmp-agent | grep -E "PASSED|FAILED|Starting"

# 2. Check snmpd is listening on port 161 inside container
docker compose exec snmp-agent ss -ulnp

# 3. Test from host (port 16100)
snmpget -v3 -l authPriv -u avdread -a SHA -A avdReadAuth123 \
  -x AES -X avdReadPriv123 \
  localhost:16100 1.3.6.1.2.1.1.5.0

# 4. Walk the AvionDash MIB
snmpwalk -v3 -l authPriv -u avdread -a SHA -A avdReadAuth123 \
  -x AES -X avdReadPriv123 \
  localhost:16100 1.3.6.1.4.1.21308.1
```

## Troubleshooting

If you still get a timeout, run diagnostics inside the container:

```bash
# Check snmpd process
docker compose exec snmp-agent ps aux | grep snmpd

# Check what port snmpd is bound to
docker compose exec snmp-agent ss -ulnp

# Test query from INSIDE container (bypasses Docker port mapping)
docker compose exec snmp-agent snmpget -v3 -l authPriv \
  -u avdread -a SHA -A avdReadAuth123 \
  -x AES -X avdReadPriv123 \
  127.0.0.1:161 1.3.6.1.2.1.1.5.0

# View full snmpd config inside container
docker compose exec snmp-agent cat /etc/snmp/snmpd.conf
docker compose exec snmp-agent cat /var/lib/snmp/snmpd.conf
```
