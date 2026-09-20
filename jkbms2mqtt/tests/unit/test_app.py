"""Tests for the orchestrator's non-glue helpers.

The ``run()`` coroutine is excluded from coverage (top-level glue); we test
``configure_logging`` and the documented contracts users actually depend on.
"""

from __future__ import annotations

import logging

import pytest

from jkbms2mqtt.app import configure_logging, is_ha_birth
from jkbms2mqtt.config import LogLevel, Settings, Transport


class TestIsHaBirth:
    """Home Assistant restarting is the cue to re-send everything retained."""

    STATUS = "homeassistant/status"

    @pytest.mark.parametrize("payload", ["online", "ONLINE", " online ", "Online"])
    def test_birth_is_recognised(self, payload: str) -> None:
        assert is_ha_birth(self.STATUS, payload, self.STATUS) is True

    def test_will_is_not_a_birth(self) -> None:
        """HA going offline must not trigger a republish storm."""
        assert is_ha_birth(self.STATUS, "offline", self.STATUS) is False

    def test_other_topics_are_ignored(self) -> None:
        assert is_ha_birth("BMS_1/control/x/set", "online", self.STATUS) is False

    def test_empty_status_topic_disables_it(self) -> None:
        assert is_ha_birth(self.STATUS, "online", "") is False

    def test_a_custom_status_topic_is_honoured(self) -> None:
        """HA's birth topic is configured separately from the discovery prefix."""
        assert is_ha_birth("ha/state", "online", "ha/state") is True
        assert is_ha_birth(self.STATUS, "online", "ha/state") is False


def test_configure_logging_sets_root_level() -> None:
    s = Settings(
        transport=Transport.TCP_GATEWAY,
        gateway_host="x.x.x.x",
        gateway_port=502,
        log_level=LogLevel.DEBUG,
    )
    configure_logging(s)
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_info_level() -> None:
    s = Settings(
        transport=Transport.TCP_GATEWAY,
        gateway_host="x.x.x.x",
        gateway_port=502,
        log_level=LogLevel.INFO,
    )
    configure_logging(s)
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_recording_enables_pymodbus_debug() -> None:
    s = Settings(
        transport=Transport.TCP_GATEWAY,
        gateway_host="x.x.x.x",
        gateway_port=502,
        log_level=LogLevel.INFO,
        recording_enabled=True,
    )
    configure_logging(s)
    assert logging.getLogger("pymodbus").level == logging.DEBUG


def test_configure_logging_recording_disabled_leaves_pymodbus_alone() -> None:
    pymodbus_logger = logging.getLogger("pymodbus")
    pymodbus_logger.setLevel(logging.NOTSET)
    s = Settings(
        transport=Transport.TCP_GATEWAY,
        gateway_host="x.x.x.x",
        gateway_port=502,
        log_level=LogLevel.WARNING,
        recording_enabled=False,
    )
    configure_logging(s)
    # We only INCREASE pymodbus' logger level when recording is on; we don't
    # touch it otherwise.
    assert pymodbus_logger.level == logging.NOTSET


def test_configure_logging_uses_force_true() -> None:
    """Adding a pre-existing root handler must not block our config.

    This is the actual bug we're guarding against (legacy code: logging stayed
    at WARNING because aiomqtt had already attached a handler).
    """
    root = logging.getLogger()
    dummy = logging.StreamHandler()
    root.addHandler(dummy)
    try:
        s = Settings(
            transport=Transport.TCP_GATEWAY,
            gateway_host="x.x.x.x",
            gateway_port=502,
            log_level=LogLevel.DEBUG,
        )
        configure_logging(s)
        assert root.level == logging.DEBUG
        # ``basicConfig(force=True)`` removes any prior handlers.
        assert dummy not in root.handlers
    finally:
        if dummy in root.handlers:
            root.removeHandler(dummy)
