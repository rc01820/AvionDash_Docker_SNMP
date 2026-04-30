"""
In-process metrics state for AvionDash.
Tracks request counts, latency samples, DB stats, and login events.
Used by /api/snmp/metrics to serve live data to the SNMP agent.
"""

import os
import time
import threading
import statistics
from collections import deque
from datetime import datetime, timezone

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False


class MetricsState:
    def __init__(self):
        self._lock = threading.Lock()
        self.start_time = time.time()
        self.start_iso  = datetime.now(timezone.utc).isoformat()

        # HTTP
        self.requests_total   = 0
        self.requests_errors  = 0
        self.status_4xx       = 0
        self.bytes_sent       = 0
        self.active_sessions  = 0

        # Auth
        self.login_success = 0
        self.login_failure = 0

        # Latency ring-buffer (last 500 samples, ms)
        self._latency_samples = deque(maxlen=500)

        # DB
        self.db_queries_total = 0
        self.db_slow_queries  = 0
        self.db_errors        = 0
        self._db_query_times  = deque(maxlen=200)

        # Process (cached)
        self._cpu_cache       = 0.0
        self._cpu_ts          = 0.0
        self._proc            = None

        # Chaos change tracking
        self.fault_last_change: dict = {}

    # ── Recording ───────────────────────────────────────────────────────────
    def record_request(self, status_code: int, latency_ms: float, resp_bytes: int = 0):
        with self._lock:
            self.requests_total += 1
            if status_code >= 500:
                self.requests_errors += 1
            elif status_code >= 400:
                self.status_4xx += 1
            self._latency_samples.append(latency_ms)
            self.bytes_sent += resp_bytes

    def record_login(self, success: bool):
        with self._lock:
            if success:
                self.login_success += 1
            else:
                self.login_failure += 1

    def record_db_query(self, duration_ms: float, error: bool = False):
        with self._lock:
            self.db_queries_total += 1
            self._db_query_times.append(duration_ms)
            if duration_ms > 1000:
                self.db_slow_queries += 1
            if error:
                self.db_errors += 1

    def record_fault_change(self, fault_key: str):
        self.fault_last_change[fault_key] = datetime.now(timezone.utc).isoformat()

    # ── Computed values ──────────────────────────────────────────────────────
    def error_rate_permille(self) -> int:
        with self._lock:
            if self.requests_total == 0:
                return 0
            return int((self.requests_errors / self.requests_total) * 1000)

    def latency_percentile(self, pct: int) -> int:
        with self._lock:
            samples = list(self._latency_samples)
        if not samples:
            return 0
        samples.sort()
        idx = max(0, int(len(samples) * pct / 100) - 1)
        return int(samples[idx])

    def db_avg_query_ms(self) -> int:
        with self._lock:
            times = list(self._db_query_times)
        if not times:
            return 0
        return int(statistics.mean(times))

    def process_memory_kb(self) -> int:
        if not _PSUTIL:
            return 0
        try:
            if self._proc is None:
                self._proc = psutil.Process(os.getpid())
            return self._proc.memory_info().rss // 1024
        except Exception:
            return 0

    def process_cpu_percent(self) -> int:
        if not _PSUTIL:
            return 0
        now = time.time()
        if now - self._cpu_ts < 5.0:          # cache 5s
            return int(self._cpu_cache)
        try:
            if self._proc is None:
                self._proc = psutil.Process(os.getpid())
            self._cpu_cache = self._proc.cpu_percent(interval=None)
            self._cpu_ts    = now
        except Exception:
            pass
        return int(self._cpu_cache)

    def process_thread_count(self) -> int:
        if not _PSUTIL:
            return 1
        try:
            if self._proc is None:
                self._proc = psutil.Process(os.getpid())
            return self._proc.num_threads()
        except Exception:
            return 1

    def web_requests_total(self) -> int:
        # Approximate: same as app requests (Nginx proxies all /api/*)
        return self.requests_total

    def web_active_connections(self) -> int:
        # Heuristic placeholder
        return max(1, min(10, self.requests_total % 8))


# Singleton
metrics_state = MetricsState()
