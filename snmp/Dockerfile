FROM debian:bookworm-slim
# Build arg for cache busting — increment BUILD_VERSION in docker-compose.yml
# to force a full rebuild: docker compose build --no-cache snmp-agent
ARG BUILD_VERSION=1.0

# ── Enable non-free repos (required for snmp-mibs-downloader) ────────────────
RUN echo "deb http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware" \
        > /etc/apt/sources.list && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        snmpd \
        snmp \
        snmp-mibs-downloader \
        curl \
        python3 \
        python3-requests \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# ── Directories ───────────────────────────────────────────────────────────────
RUN mkdir -p /usr/share/snmp/mibs \
             /var/log/snmp \
             /var/lib/snmp \
             /usr/local/lib/snmp && \
    # Pre-create a clean empty snmpd.conf so snmpd does not read stale
    # pass_persist lines from a previous container run via the volume.
    # Entrypoint overwrites this with fresh createUser lines on each start.
    echo "# AvionDash SNMP persistence — written by entrypoint.sh" > /var/lib/snmp/snmpd.conf

# ── Enterprise MIB ───────────────────────────────────────────────────────────
COPY mibs/AVIONDASH-MIB.txt /usr/share/snmp/mibs/AVIONDASH-MIB.txt

# ── Client tools MIB config (/etc/snmp/snmp.conf) ────────────────────────────
# Controls snmpget / snmpwalk MIB name resolution.
# "mibs ALL" is valid in snmp.conf (client config) but NOT in snmpd.conf.
# Load specific MIBs — exclude SNMPv2-PDU (has a bad INTEGER definition
# that prints a harmless but noisy warning on every client invocation).
RUN echo "mibdirs /usr/share/snmp/mibs:/usr/share/snmp/mibs/iana:/usr/share/snmp/mibs/ietf" \
        > /etc/snmp/snmp.conf && \
    echo "mibs SNMPv2-SMI:SNMPv2-TC:SNMPv2-CONF:SNMPv2-MIB:AVIONDASH-MIB" >> /etc/snmp/snmp.conf && \
    echo "showMibErrors 0" >> /etc/snmp/snmp.conf

# ── pass_persist handler ──────────────────────────────────────────────────────
COPY aviondash_pass.py /usr/local/lib/snmp/aviondash_pass.py
RUN chmod +x /usr/local/lib/snmp/aviondash_pass.py

# ── snmpd daemon config ───────────────────────────────────────────────────────
# NOTE: No agentAddress directive — bind address is passed as CLI arg
# in entrypoint.sh to avoid "Error opening endpoint" errors in Docker.
COPY conf/snmpd.conf /etc/snmp/snmpd.conf

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 161/udp 162/udp

ENTRYPOINT ["/entrypoint.sh"]
