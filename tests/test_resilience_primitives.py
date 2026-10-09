"""Backoff, circuit breaker y rate limiter (Módulo 1)."""

from __future__ import annotations

import random

import pytest

from conftest import FakeClock
from vinted_arb.ingestion.backoff import BackoffPolicy
from vinted_arb.ingestion.circuit_breaker import BreakerState, CircuitBreaker
from vinted_arb.ingestion.rate_limiter import AsyncRateLimiter


class TestBackoff:
    def test_exponential_caps(self) -> None:
        policy = BackoffPolicy(base_delay_s=2, max_delay_s=20, multiplier=2, jitter="none")
        assert [policy.delay_for(n) for n in range(1, 6)] == [2, 4, 8, 16, 20]

    def test_equal_jitter_guarantees_minimum_wait(self) -> None:
        policy = BackoffPolicy(base_delay_s=4, max_delay_s=64, jitter="equal")
        rng = random.Random(42)
        for n in range(1, 6):
            cap = policy.cap_for(n)
            for _ in range(50):
                delay = policy.delay_for(n, rng)
                assert cap / 2 <= delay <= cap

    def test_full_jitter_range(self) -> None:
        policy = BackoffPolicy(base_delay_s=4, max_delay_s=64, jitter="full")
        rng = random.Random(1)
        delays = [policy.delay_for(3, rng) for _ in range(200)]
        assert all(0 <= d <= 16 for d in delays)
        assert min(delays) < 4  # el full jitter sí puede reintentar casi al instante

    def test_retry_after_is_honoured_and_capped(self) -> None:
        policy = BackoffPolicy(base_delay_s=1, max_delay_s=8, jitter="none", max_retry_after_s=120)
        assert policy.delay_with_hint(1, 30) == 30
        assert policy.delay_with_hint(1, 10_000) == 120
        assert policy.delay_with_hint(3, None) == 4
        assert policy.delay_with_hint(4, 2) == 8  # el backoff propio manda si es mayor

    def test_huge_retry_numbers_do_not_overflow(self) -> None:
        policy = BackoffPolicy(base_delay_s=1, max_delay_s=60, jitter="none")
        assert policy.delay_for(10_000) == 60

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"base_delay_s": 0},
            {"base_delay_s": 10, "max_delay_s": 5},
            {"multiplier": 0.5},
            {"max_attempts": 0},
        ],
    )
    def test_invalid_policies(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError):
            BackoffPolicy(**kwargs)  # type: ignore[arg-type]

    def test_retry_number_starts_at_one(self) -> None:
        with pytest.raises(ValueError):
            BackoffPolicy().delay_for(0)


class TestCircuitBreaker:
    def make(self, clock: FakeClock, **kwargs: object) -> CircuitBreaker:
        params: dict[str, object] = {
            "failure_threshold": 3,
            "base_cooldown_s": 100,
            "max_cooldown_s": 350,
            "clock": clock,
        }
        params.update(kwargs)
        return CircuitBreaker("t", **params)  # type: ignore[arg-type]

    def test_opens_after_threshold_of_soft_failures(self, clock: FakeClock) -> None:
        breaker = self.make(clock)
        breaker.record_failure("500")
        breaker.record_failure("500")
        assert breaker.state is BreakerState.CLOSED
        breaker.record_failure("500")
        assert breaker.state is BreakerState.OPEN
        assert not breaker.allow_request()
        assert breaker.retry_in_s() == pytest.approx(100)

    def test_success_resets_failure_count(self, clock: FakeClock) -> None:
        breaker = self.make(clock)
        breaker.record_failure("500")
        breaker.record_failure("500")
        breaker.record_success()
        breaker.record_failure("500")
        assert breaker.state is BreakerState.CLOSED

    def test_hard_failure_opens_immediately(self, clock: FakeClock) -> None:
        breaker = self.make(clock)
        breaker.trip("captcha")
        assert breaker.state is BreakerState.OPEN
        assert breaker.last_reason == "captcha"

    def test_half_open_allows_a_single_probe(self, clock: FakeClock) -> None:
        breaker = self.make(clock)
        breaker.trip("captcha")
        clock.advance(99)
        assert not breaker.allow_request()
        clock.advance(1)
        assert breaker.allow_request()
        assert breaker.state is BreakerState.HALF_OPEN
        assert not breaker.allow_request()  # segunda petición concurrente denegada

    def test_unreported_probe_expires(self, clock: FakeClock) -> None:
        breaker = self.make(clock, probe_timeout_s=60)
        breaker.trip("captcha")
        clock.advance(100)
        assert breaker.allow_request()  # sonda que nunca informa (excepción inesperada)
        clock.advance(59)
        assert not breaker.allow_request()
        clock.advance(1)
        assert breaker.allow_request()  # nueva sonda: el breaker no se queda atascado

    def test_probe_success_closes_and_resets_escalation(self, clock: FakeClock) -> None:
        breaker = self.make(clock)
        breaker.trip("captcha")
        clock.advance(100)
        assert breaker.allow_request()
        breaker.record_success()
        assert breaker.state is BreakerState.CLOSED
        assert breaker.consecutive_trips == 0
        breaker.trip("captcha")
        assert breaker.retry_in_s() == pytest.approx(100)

    def test_probe_failure_escalates_cooldown_up_to_max(self, clock: FakeClock) -> None:
        breaker = self.make(clock)
        breaker.trip("captcha")
        expected = [200, 350, 350]  # 100 → 200 → 400 (tope 350) → 350
        for cooldown in expected:
            clock.advance(breaker.retry_in_s())
            assert breaker.allow_request()
            breaker.record_failure("sigue bloqueado")
            assert breaker.state is BreakerState.OPEN
            assert breaker.retry_in_s() == pytest.approx(cooldown)

    def test_snapshot_roundtrip_survives_restart(self, clock: FakeClock) -> None:
        breaker = self.make(clock)
        breaker.trip("captcha")
        snapshot = breaker.snapshot()

        restored = self.make(clock)
        restored.restore(snapshot)
        assert restored.state is BreakerState.OPEN
        assert restored.retry_in_s() == pytest.approx(100)
        assert restored.last_reason == "captcha"

    def test_half_open_snapshot_restores_as_open(self, clock: FakeClock) -> None:
        breaker = self.make(clock)
        breaker.restore({"state": "half_open", "open_until": clock() - 1})
        assert breaker.state is BreakerState.OPEN
        assert breaker.allow_request()  # enfriamiento ya vencido → sonda

    def test_corrupt_snapshot_is_ignored(self, clock: FakeClock) -> None:
        breaker = self.make(clock)
        breaker.restore({"state": "nonsense"})
        assert breaker.state is BreakerState.CLOSED

    def test_listener_receives_transitions_and_cannot_break_breaker(self, clock: FakeClock) -> None:
        seen: list[tuple[str, str]] = []

        def listener(old: BreakerState, new: BreakerState, reason: str) -> None:
            seen.append((old.value, new.value))
            raise RuntimeError("listener roto")

        breaker = self.make(clock, on_state_change=listener)
        breaker.trip("captcha")
        clock.advance(100)
        breaker.allow_request()
        breaker.record_success()
        assert seen == [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]


class TestRateLimiter:
    async def test_burst_then_steady_rate(self, clock: FakeClock) -> None:
        limiter = AsyncRateLimiter(60, burst=2, jitter_ratio=0, clock=clock, sleep=clock.sleep)
        waits = [await limiter.acquire() for _ in range(4)]
        assert waits[:2] == [0, 0]
        assert waits[2] == pytest.approx(1.0)
        assert waits[3] == pytest.approx(1.0)

    async def test_tokens_refill_over_time(self, clock: FakeClock) -> None:
        limiter = AsyncRateLimiter(6, burst=1, jitter_ratio=0, clock=clock, sleep=clock.sleep)
        assert await limiter.acquire() == 0
        clock.advance(10)
        assert await limiter.acquire() == 0

    async def test_jitter_is_bounded_and_breaks_periodicity(self, clock: FakeClock) -> None:
        limiter = AsyncRateLimiter(
            60, burst=1, jitter_ratio=0.5, clock=clock, sleep=clock.sleep, rng=random.Random(3)
        )
        waits = [await limiter.acquire() for _ in range(20)]
        gaps = waits[1:]
        assert all(1.0 <= w <= 1.5 + 1e-9 for w in gaps)
        assert len({round(w, 6) for w in gaps}) > 1

    @pytest.mark.parametrize("kwargs", [{"rate_per_minute": 0}, {"burst": 0}, {"jitter_ratio": -1}])
    def test_invalid_parameters(self, kwargs: dict[str, float]) -> None:
        params: dict[str, float] = {"rate_per_minute": 6}
        params.update(kwargs)
        with pytest.raises(ValueError):
            AsyncRateLimiter(**params)  # type: ignore[arg-type]
