#!/bin/bash
set -e

LOG_DIR=/var/log/snmp
VARLIB=/var/lib/snmp
mkdir -p "$LOG_DIR" "$VARLIB"

# ── Credentials ───────────────────────────────────────────────────────────────
RU="${SNMP_READ_USER:-avdread}"
RAP="${SNMP_READ_AUTH_PASS:-avdReadAuth123}"
RPP="${SNMP_READ_PRIV_PASS:-avdReadPriv123}"
AU="${SNMP_ADMIN_USER:-avdadmin}"
AAP="${SNMP_ADMIN_AUTH_PASS:-avdAdminAuth123}"
APP="${SNMP_ADMIN_PRIV_PASS:-avdAdminPriv123}"

# ── Write ONLY createUser lines to the persistence file ───────────────────────
# IMPORTANT: This file must contain ONLY createUser lines.
# snmpd reads it automatically alongside /etc/snmp/snmpd.conf.
# If it contained pass_persist lines they would duplicate our registration.
printf 'createUser %s SHA "%s" AES "%s"\n' "$RU"  "$RAP" "$RPP"  > "$VARLIB/snmpd.conf"
printf 'createUser %s SHA "%s" AES "%s"\n' "$AU"  "$AAP" "$APP"  >> "$VARLIB/snmpd.conf"

echo "[snmp] USM users written to $VARLIB/snmpd.conf"
echo "[snmp] Read-only  user : $RU  (SHA/AES authPriv)"
echo "[snmp] Admin      user : $AU  (SHA/AES authPriv)"
echo "[snmp] Metrics API     : ${AVIONDASH_API_URL:-http://app:8000}/api/snmp/metrics"
echo "[snmp] Host port       : 16100 (queries)  16200 (traps)"

export AVIONDASH_API_URL="${AVIONDASH_API_URL:-http://app:8000}"

# ── Start snmpd as a daemon ───────────────────────────────────────────────────
# snmpd without -f daemonizes (forks to background) and writes its PID.
# We then monitor the PID to keep the container alive and detect crashes.
# The bind address MUST be a positional argument (not agentAddress in conf).
echo "[snmp] Starting snmpd (daemon mode)..."
/usr/sbin/snmpd \
    -Lo \
    -p /var/run/snmpd.pid \
    -c /etc/snmp/snmpd.conf \
    udp:0.0.0.0:161

# Wait for snmpd to fork and bind
sleep 3

SNMPD_PID=$(cat /var/run/snmpd.pid 2>/dev/null || echo "")
if [ -z "$SNMPD_PID" ]; then
    echo "[snmp] ERROR: snmpd did not write /var/run/snmpd.pid — startup failed."
    echo "[snmp] Common causes: duplicate pass_persist registration, bad agentAddress"
    exit 1
fi
echo "[snmp] snmpd running, PID: $SNMPD_PID"

# ── Self-test ──────────────────────────────────────────────────────────────────
echo "[snmp] Running self-test..."
TEST_RESULT=$(snmpget -v3 -l authPriv \
    -u "$RU" -a SHA -A "$RAP" -x AES -X "$RPP" \
    -t 5 -r 2 \
    127.0.0.1:161 1.3.6.1.2.1.1.5.0 2>&1)

if echo "$TEST_RESULT" | grep -q "STRING"; then
    echo "[snmp] Self-test PASSED ✓ — $TEST_RESULT"
else
    echo "[snmp] Self-test result: $TEST_RESULT"
    echo "[snmp] WARNING: self-test did not get expected response"
fi

echo "[snmp] Ready. Host query:"
echo "  snmpwalk -v3 -l authPriv -u $RU -a SHA -A $RAP -x AES -X $RPP localhost:16100 1.3.6.1.4.1.21308.1"

# ── Keep container alive — exit if snmpd dies ─────────────────────────────────
while kill -0 "$SNMPD_PID" 2>/dev/null; do
    sleep 10
done
echo "[snmp] snmpd PID $SNMPD_PID exited — container stopping."
exit 1
