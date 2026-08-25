"""Reliability primitives for model and network calls.

Wraps the raw model client with bounded retries, a fallback chain to a weaker
endpoint when the primary fails, per-call timeouts, and a per-endpoint circuit
breaker so one failing endpoint stops being called rather than draining the
budget through repeated timeouts.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import config


class CircuitOpen(Exception):
    pass


@dataclass
class Breaker:
    threshold: int = 5
    cooldown_seconds: int = 300
    failures: int = 0
    opened_at: float = 0.0

    def allow(self) -> bool:
        if self.failures < self.threshold:
            return True
        if time.time() - self.opened_at >= self.cooldown_seconds:
            self.failures = 0
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.time()


_BREAKERS: dict[str, Breaker] = {}


def _breaker(endpoint: str) -> Breaker:
    return _BREAKERS.setdefault(endpoint, Breaker())


FALLBACK_CHAIN = {
    config.JUDGE_MODEL: config.ESCALATE_MODEL,
    config.ESCALATE_MODEL: config.EXTRACT_MODEL,
    config.SUPERVISOR_MODEL: config.ESCALATE_MODEL,
    config.EXTRACT_MODEL: config.TRIAGE_MODEL,
}


def with_retry(fn, *args, attempts: int = 3, base_delay: float = 2.0, **kwargs):
    last = None
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as error:
            last = error
            if attempt < attempts - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise last


def resilient_model_call(call_fn, endpoint: str, system: str, user: str,
                         max_tokens: int = 1500) -> tuple[str, str]:
    """Call a model endpoint with retry, circuit breaking, and one fallback.

    call_fn is the underlying client (context.call_model). Returns the response
    text and the endpoint that actually served it, so the caller can record which
    model produced a finding.
    """
    chain = [endpoint]
    fallback = FALLBACK_CHAIN.get(endpoint)
    if fallback and fallback != endpoint:
        chain.append(fallback)

    last_error = None
    for target in chain:
        breaker = _breaker(target)
        if not breaker.allow():
            last_error = CircuitOpen(f"circuit open for {target}")
            continue
        try:
            result = with_retry(call_fn, target, system, user, max_tokens)
            breaker.record_success()
            return result, target
        except Exception as error:
            breaker.record_failure()
            last_error = error
    raise last_error if last_error else RuntimeError("model call failed")


def guarded_call(endpoint, system, user, agent, stage, meter, max_tokens=1500):
    """Resilient, metered model call. Returns (text, model_used).

    Applies the fallback chain and circuit breaker, times the call, and records
    token and cost usage on the meter under the given agent and stage.
    """
    from .context import call_model
    from .observability import approx_tokens, timed

    with timed() as box:
        text, used = resilient_model_call(
            call_model, endpoint, system, user, max_tokens
        )
    if meter is not None:
        meter.record(
            agent, stage, used,
            approx_tokens(system) + approx_tokens(user),
            approx_tokens(text), box["ms"],
        )
    return text, used
