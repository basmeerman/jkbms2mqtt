"""Tests for the shared exponential-backoff connect helper."""

from __future__ import annotations

import asyncio

import pytest

from jkbms2mqtt.transport.backoff import connect_with_backoff


class _Flaky:
    def __init__(self, fail_n_times: int) -> None:
        self.attempts = 0
        self.fail_n_times = fail_n_times

    async def connect(self) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_n_times:
            raise OSError("simulated")


class _AlwaysFails:
    def __init__(self) -> None:
        self.attempts = 0

    async def connect(self) -> None:
        self.attempts += 1
        raise OSError("always fails")


async def _no_sleep(_d: float) -> None:
    return None


async def test_succeeds_on_first_attempt() -> None:
    t = _Flaky(fail_n_times=0)
    await connect_with_backoff(t)
    assert t.attempts == 1


async def test_retries_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    t = _Flaky(fail_n_times=3)
    await connect_with_backoff(t)
    assert t.attempts == 4


async def test_raises_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    t = _AlwaysFails()
    with pytest.raises(OSError):
        await connect_with_backoff(t, max_attempts=3)
    assert t.attempts == 3


async def test_backoff_caps_at_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercises the `min(backoff*2, MAX_BACKOFF_S)` cap branch."""
    sleeps: list[float] = []

    async def record_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    t = _AlwaysFails()
    with pytest.raises(OSError):
        await connect_with_backoff(t, max_attempts=8)
    # 7 sleeps (one per failure-then-retry), doubling 1, 2, 4, 8, 16, 30, 30
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


async def test_label_appears_in_log(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    import logging

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    caplog.set_level(logging.WARNING)
    t = _AlwaysFails()
    with pytest.raises(OSError):
        await connect_with_backoff(t, max_attempts=2, label="CustomLabel")
    assert "CustomLabel" in caplog.text
