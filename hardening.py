"""
Production hardening utilities for S-Tool Projector.

Provides rate limiting, retry logic, health checks, alerting,
and circuit breaker patterns for robust production operation.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Sequence

import requests as _requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hardening")


# ── Rate Limiters ──


class TokenBucketLimiter:
    """Thread-safe token bucket rate limiter."""

    def __init__(self, rate_per_sec: float, burst: int):
        self._rate = rate_per_sec
        self._burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def acquire(self, timeout: float = 5.0) -> bool:
        """Try to acquire a token. Returns True if acquired within timeout."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.05, deadline - time.monotonic()))

    @property
    def remaining(self) -> int:
        with self._lock:
            self._refill()
            return int(self._tokens)


class DailyQuotaLimiter:
    """Rate limiter that resets at midnight UTC each day."""

    def __init__(self, max_per_day: int):
        self._max = max_per_day
        self._count = 0
        self._today: str = ""
        self._lock = threading.Lock()

    def _maybe_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._today:
            self._today = today
            self._count = 0

    def acquire(self, timeout: float = 5.0) -> bool:
        """Acquire a slot. Timeout is accepted for interface parity but unused."""
        with self._lock:
            self._maybe_reset()
            if self._count < self._max:
                self._count += 1
                return True
            return False

    @property
    def remaining(self) -> int:
        with self._lock:
            self._maybe_reset()
            return max(0, self._max - self._count)


# ── Retry with Backoff ──


def retry_with_backoff(
    func: Callable | None = None,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple = (Exception,),
):
    """Decorator: retry with exponential backoff and jitter.

    Can be used as @retry_with_backoff or @retry_with_backoff(max_retries=2).
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_retries:
                        raise
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    jitter = random.uniform(0, delay * 0.5)
                    sleep_time = delay + jitter
                    log.warning(
                        "Retry %d/%d for %s after %.1fs: %s",
                        attempt + 1, max_retries, fn.__name__, sleep_time, exc,
                    )
                    time.sleep(sleep_time)
            raise last_exc  # unreachable, but keeps type checkers happy
        return wrapper

    if func is not None:
        # Called as @retry_with_backoff without parens
        return decorator(func)
    return decorator


# ── Health Checker ──


class HealthChecker:
    """Tracks health status of system components."""

    COMPONENTS = ("worker", "api", "sentiment")
    ERROR_WINDOW_SECS = 3600  # 1 hour
    MAX_ERRORS_PER_HOUR = 10
    MAX_DOWN_SECS = 7200  # 2 hours

    def __init__(self):
        self._lock = threading.Lock()
        self._last_success: dict[str, float] = {}
        self._errors: dict[str, deque] = {c: deque() for c in self.COMPONENTS}
        self._provider_status: dict[str, bool] = {}

    def record_success(self, component: str):
        """Record a successful operation for a component."""
        with self._lock:
            self._last_success[component] = time.time()

    def record_error(self, component: str):
        """Record an error for a component (rolling 1-hour window)."""
        with self._lock:
            if component not in self._errors:
                self._errors[component] = deque()
            self._errors[component].append(time.time())

    def set_provider_status(self, provider: str, available: bool):
        """Set availability of a data provider (yfinance, stocktwits, etc.)."""
        with self._lock:
            self._provider_status[provider] = available

    def _prune_errors(self, component: str) -> int:
        """Remove errors older than the window; return current count."""
        cutoff = time.time() - self.ERROR_WINDOW_SECS
        q = self._errors.get(component, deque())
        while q and q[0] < cutoff:
            q.popleft()
        return len(q)

    def check_health(self) -> dict[str, Any]:
        """Return health status for all components and providers."""
        now = time.time()
        result: dict[str, Any] = {"components": {}, "providers": {}, "timestamp": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            for comp in self.COMPONENTS:
                error_count = self._prune_errors(comp)
                last_ok = self._last_success.get(comp)
                down_secs = (now - last_ok) if last_ok else None
                status = "healthy"
                if error_count > self.MAX_ERRORS_PER_HOUR:
                    status = "degraded"
                if last_ok and down_secs and down_secs > self.MAX_DOWN_SECS:
                    status = "down"
                if last_ok is None and error_count > 0:
                    status = "unknown"
                result["components"][comp] = {
                    "status": status,
                    "errors_last_hour": error_count,
                    "last_success": datetime.fromtimestamp(last_ok, tz=timezone.utc).isoformat() if last_ok else None,
                }
            for prov, available in self._provider_status.items():
                result["providers"][prov] = {"available": available}
        return result

    def is_healthy(self) -> bool:
        """True if no component has >10 errors/hour or been down >2 hours."""
        now = time.time()
        with self._lock:
            for comp in self.COMPONENTS:
                if self._prune_errors(comp) > self.MAX_ERRORS_PER_HOUR:
                    return False
                last_ok = self._last_success.get(comp)
                if last_ok and (now - last_ok) > self.MAX_DOWN_SECS:
                    return False
        return True


# ── Alert Manager ──


class AlertManager:
    """Simple alert system with deduplication and optional webhook."""

    DEDUP_WINDOW_SECS = 900  # 15 minutes
    ALERT_LOG = "alerts.log"

    def __init__(self, webhook_url: str | None = None):
        self._webhook_url = webhook_url or os.environ.get("ALERT_WEBHOOK_URL")
        self._lock = threading.Lock()
        self._recent: dict[str, float] = {}  # alert_key -> last_sent_time
        self._alert_logger = logging.getLogger("alerts")
        # Add file handler for alerts.log
        if not any(isinstance(h, logging.FileHandler) and h.baseFilename.endswith(self.ALERT_LOG) for h in self._alert_logger.handlers):
            fh = logging.FileHandler(self.ALERT_LOG)
            fh.setLevel(logging.CRITICAL)
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._alert_logger.addHandler(fh)

    def _is_duplicate(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            last = self._recent.get(key)
            if last and (now - last) < self.DEDUP_WINDOW_SECS:
                return True
            self._recent[key] = now
            return False

    def send_alert(self, title: str, message: str, severity: str = "critical"):
        """Send an alert if not recently sent (15-min dedup window)."""
        key = f"{severity}:{title}"
        if self._is_duplicate(key):
            log.debug("Alert suppressed (duplicate): %s", title)
            return

        full_msg = f"[{severity.upper()}] {title}: {message}"
        self._alert_logger.critical(full_msg)
        log.warning("ALERT: %s", full_msg)

        if self._webhook_url:
            self._send_webhook(title, message, severity)

    def _send_webhook(self, title: str, message: str, severity: str):
        """Post alert to Slack/Discord webhook."""
        payload = {
            "text": f"*[{severity.upper()}] {title}*\n{message}",
            "content": f"**[{severity.upper()}] {title}**\n{message}",
        }
        try:
            resp = _requests.post(self._webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            log.error("Webhook delivery failed: %s", exc)

    def check_and_alert(self, health: dict[str, Any], worker_stats: dict | None = None):
        """Evaluate health data and fire alerts for threshold breaches.

        Alert triggers:
          - Worker failure rate >20%
          - API error rate >5%
          - Provider down
        """
        # Provider down alerts
        for prov, info in health.get("providers", {}).items():
            if not info.get("available", True):
                self.send_alert(
                    f"Provider down: {prov}",
                    f"Data provider '{prov}' is unavailable.",
                    severity="critical",
                )

        # Component error alerts
        for comp, info in health.get("components", {}).items():
            if info.get("status") in ("degraded", "down"):
                self.send_alert(
                    f"Component {info['status']}: {comp}",
                    f"{comp} has {info['errors_last_hour']} errors in the last hour.",
                    severity="critical" if info["status"] == "down" else "warning",
                )

        # Worker failure rate
        if worker_stats:
            total = worker_stats.get("total", 0)
            failed = worker_stats.get("failed", 0)
            if total > 0 and (failed / total) > 0.20:
                self.send_alert(
                    "Worker failure rate high",
                    f"{failed}/{total} projections failed ({failed/total:.0%}).",
                    severity="critical",
                )

            # API error rate (if tracked)
            api_total = worker_stats.get("api_requests", 0)
            api_errors = worker_stats.get("api_errors", 0)
            if api_total > 0 and (api_errors / api_total) > 0.05:
                self.send_alert(
                    "API error rate high",
                    f"{api_errors}/{api_total} API errors ({api_errors/api_total:.0%}).",
                    severity="warning",
                )


# ── Circuit Breaker ──


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Circuit breaker to auto-disable flaky provider calls.

    States:
      CLOSED  — normal operation, calls pass through
      OPEN    — too many failures, calls are skipped (raises CircuitOpenError)
      HALF_OPEN — after reset_timeout, allow one test call
    """

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 300.0):
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                if self._last_failure_time and (time.monotonic() - self._last_failure_time) >= self._reset_timeout:
                    self._state = CircuitState.HALF_OPEN
            return self._state

    def call(self, func: Callable, *args, **kwargs):
        """Execute func through the circuit breaker."""
        current_state = self.state

        if current_state == CircuitState.OPEN:
            raise CircuitOpenError(f"Circuit is OPEN — call to {func.__name__} skipped")

        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            self._record_failure()
            raise
        else:
            self._record_success()
            return result

    def _record_failure(self):
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self._failure_threshold:
                prev = self._state
                self._state = CircuitState.OPEN
                if prev != CircuitState.OPEN:
                    log.warning(
                        "Circuit breaker OPEN after %d failures (reset in %.0fs)",
                        self._failure_count, self._reset_timeout,
                    )

    def _record_success(self):
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                log.info("Circuit breaker CLOSED — test call succeeded")
            self._failure_count = 0
            self._state = CircuitState.CLOSED

    def reset(self):
        """Manually reset the circuit breaker."""
        with self._lock:
            self._failure_count = 0
            self._state = CircuitState.CLOSED
            self._last_failure_time = None


class CircuitOpenError(Exception):
    """Raised when a call is blocked by an open circuit breaker."""
    pass


# ── Module-level singleton for shared health tracking ──

health_checker = HealthChecker()
