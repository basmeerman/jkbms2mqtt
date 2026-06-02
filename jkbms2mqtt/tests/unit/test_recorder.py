"""Recorder + replay tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from jkbms2mqtt.recorder import (
    JsonlReplayTransport,
    RecordingTransport,
    ReplayExhaustedError,
    ReplayMismatchError,
    write_jsonl,
)


@dataclass
class FakeTransport:
    rx_queue: list[bytes] = field(default_factory=list)
    tx_log: list[bytes] = field(default_factory=list)
    connected: bool = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def aclose(self) -> None:
        self.connected = False

    async def write(self, data: bytes) -> None:
        self.tx_log.append(data)

    async def read_exactly(self, n: int, *, timeout_s: float) -> bytes:
        del timeout_s
        if not self.rx_queue:
            raise TimeoutError("queue empty")
        chunk = self.rx_queue.pop(0)
        assert len(chunk) == n
        return chunk


async def test_recorder_writes_jsonl(tmp_path: Path) -> None:
    inner = FakeTransport(rx_queue=[b"\xaa\xbb"])
    path = tmp_path / "rec.jsonl"
    rec = RecordingTransport(inner=inner, path=path)
    await rec.connect()
    await rec.write(b"\x55")
    out = await rec.read_exactly(2, timeout_s=1.0)
    assert out == b"\xaa\xbb"
    await rec.aclose()

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(l) for l in lines]
    assert parsed[0]["dir"] == "tx"
    assert parsed[0]["hex"] == "55"
    assert parsed[1]["dir"] == "rx"
    assert parsed[1]["hex"] == "aabb"


async def test_recorder_aclose_idempotent(tmp_path: Path) -> None:
    inner = FakeTransport()
    rec = RecordingTransport(inner=inner, path=tmp_path / "x.jsonl")
    await rec.connect()
    await rec.aclose()
    await rec.aclose()
    assert not rec.is_connected


async def test_replay_round_trips_an_exchange(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_jsonl(
        path,
        [
            ("tx", b"\x01\x02"),
            ("rx", b"\xaa\xbb\xcc"),
        ],
    )
    replay = JsonlReplayTransport(path=path)
    await replay.connect()
    assert replay.is_connected
    await replay.write(b"\x01\x02")
    out = await replay.read_exactly(3, timeout_s=0.5)
    assert out == b"\xaa\xbb\xcc"
    await replay.aclose()
    assert not replay.is_connected


async def test_replay_tx_mismatch_raises(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_jsonl(path, [("tx", b"\x01\x02")])
    replay = JsonlReplayTransport(path=path)
    with pytest.raises(ReplayMismatchError, match="tx mismatch"):
        await replay.write(b"\xff\xff")


async def test_replay_wrong_direction_for_tx_raises(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_jsonl(path, [("rx", b"\x01")])
    replay = JsonlReplayTransport(path=path)
    with pytest.raises(ReplayMismatchError, match="expected tx"):
        await replay.write(b"\x01")


async def test_replay_wrong_direction_for_rx_raises(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_jsonl(path, [("tx", b"\x01")])
    replay = JsonlReplayTransport(path=path)
    with pytest.raises(ReplayMismatchError, match="expected rx"):
        await replay.read_exactly(1, timeout_s=0.5)


async def test_replay_exhaustion_raises(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_jsonl(path, [])
    replay = JsonlReplayTransport(path=path)
    with pytest.raises(ReplayExhaustedError):
        await replay.read_exactly(1, timeout_s=0.5)


async def test_replay_can_assemble_a_buffer_from_multiple_records(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_jsonl(
        path,
        [
            ("rx", b"\x01\x02"),
            ("rx", b"\x03\x04\x05"),
        ],
    )
    replay = JsonlReplayTransport(path=path)
    out = await replay.read_exactly(5, timeout_s=0.5)
    assert out == b"\x01\x02\x03\x04\x05"


def test_write_jsonl_rejects_bad_direction(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"rx\|tx"):
        write_jsonl(tmp_path / "x.jsonl", [("middle", b"\x00")])


def test_write_jsonl_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_jsonl(path, [("rx", b"\x01")])
    # Manually append blank lines and reparse
    with path.open("a") as fh:
        fh.write("\n\n")
    replay = JsonlReplayTransport(path=path)
    assert len(replay._records) == 1  # type: ignore[attr-defined]
