#!/bin/bash
# AvionDash — Force rebuild SNMP agent with no cache
set -e
echo "Stopping all containers..."
docker compose down

echo "Removing old SNMP agent image..."
docker rmi avion-snmp-snmp-agent 2>/dev/null || true
docker rmi aviondash-snmp 2>/dev/null || true
# Also try the directory-prefixed name Docker Compose uses
DIRNAME=$(basename "$(pwd)")
docker rmi "${DIRNAME}-snmp-agent" 2>/dev/null || true

echo "Rebuilding snmp-agent with --no-cache..."
docker compose build --no-cache snmp-agent

echo "Starting all services..."
docker compose up -d

echo ""
echo "Waiting 15s for startup..."
sleep 15

echo ""
echo "=== snmp-agent logs ==="
docker compose logs snmp-agent --tail 30

echo ""
echo "=== Self-test from host ==="
snmpget -v3 -l authPriv \
  -u avdread -a SHA -A avdReadAuth123 \
  -x AES -X avdReadPriv123 \
  -t 5 -r 2 \
  localhost:16100 1.3.6.1.2.1.1.5.0 \
  && echo "SUCCESS: SNMP agent responding" \
  || echo "FAILED: run 'docker compose logs snmp-agent' to debug"
