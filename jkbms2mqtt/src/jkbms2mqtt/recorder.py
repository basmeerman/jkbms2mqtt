"""Traffic recorder + JSONL replay.

Every read and write on a recorded transport is appended to a JSONL file:

    {"ts": <unix_micros>, "dir": "tx" | "rx", "hex": "55aaeb90..."}

The same format is what the test fixtures use. Recording is a first-class
production feature (set `recording.enabled: true` in config) — not a test
crutch — which means the format is part of the public contract.

The `JsonlReplayTransport` plays a JSONL back as if it were the BMS, asserting
each TX line matches the bytes the caller wrote and returning the RX bytes on
read. Useful for end-to-end integration tests with no hardware.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from jkbms2mqtt.transport.base import Transport


@dataclass
class RecordingTransport:
    """Wraps an underlying transport and appends every byte to a JSONL file."""

    inner: Transport
    path: Path
    # `_file` is opened lazily on the first write so we don't create the file
    # if the user enables recording but the transport never actually connects.
    _file: object = field(default=None, init=False, repr=False)

    @property
    def is_connected(self) -> bool:
        return self.inner.is_connected

    async def connect(self) -> None:
        await self.inner.connect()

    async def aclose(self) -> None:
        await self.inner.aclose()
        if self._file is not None:
            self._file.close()  # type: ignore[attr-defined]
            self._file = None

    async def write(self, data: bytes) -> None:
        await self.inner.write(data)
        self._append("tx", data)

    async def read_exactly(self, n: int, *, timeout_s: float) -> bytes:
        data = await self.inner.read_exactly(n, timeout_s=timeout_s)
        self._append("rx", data)
        return data

    def _append(self, direction: str, data: bytes) -> None:
        if self._file is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a", encoding="utf-8")
        record = {
            "ts": int(time.time() * 1_000_000),
            "dir": direction,
            "hex": data.hex(),
        }
        self._file.write(json.dumps(record))  # type: ignore[attr-defined]
        self._file.write("\n")  # type: ignore[attr-defined]
        self._file.flush()  # type: ignore[attr-defined]


@dataclass
class JsonlReplayTransport:
    """Replay a recorded JSONL as a fake BMS.

    Reads return the next `rx` record. Writes assert the bytes match the next
    `tx` record exactly (raising `ReplayMismatchError` on divergence). When the
    log is exhausted, `read_exactly` raises `ReplayExhaustedError`.
    """

    path: Path
    _records: list[dict[str, object]] = field(default_factory=list, init=False)
    _cursor: int = field(default=0, init=False)
    _connected: bool = field(default=False, init=False)
    _rx_buffer: bytes = field(default=b"", init=False)

    def __post_init__(self) -> None:
        with self.path.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                self._records.append(json.loads(line))

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def aclose(self) -> None:
        self._connected = False

    async def write(self, data: bytes) -> None:
        record = self._next_tx_record()
        expected = bytes.fromhex(str(record["hex"]))
        if expected != data:
            raise ReplayMismatchError(
                f"tx mismatch at record {self._cursor}: expected {expected.hex()}, "
                f"got {data.hex()}"
            )

    async def read_exactly(self, n: int, *, timeout_s: float) -> bytes:
        del timeout_s  # replay is synchronous; honour the signature
        while len(self._rx_buffer) < n:
            record = self._next_rx_record()
            self._rx_buffer += bytes.fromhex(str(record["hex"]))
        out = self._rx_buffer[:n]
        self._rx_buffer = self._rx_buffer[n:]
        return out

    def _next_tx_record(self) -> dict[str, object]:
        record = self._next_record()
        if record["dir"] != "tx":
            raise ReplayMismatchError(
                f"expected tx record at position {self._cursor - 1}, got {record['dir']}"
            )
        return record

    def _next_rx_record(self) -> dict[str, object]:
        record = self._next_record()
        if record["dir"] != "rx":
            raise ReplayMismatchError(
                f"expected rx record at position {self._cursor - 1}, got {record['dir']}"
            )
        return record

    def _next_record(self) -> dict[str, object]:
        if self._cursor >= len(self._records):
            raise ReplayExhaustedError("end of replay log")
        record = self._records[self._cursor]
        self._cursor += 1
        return record


class ReplayMismatchError(AssertionError):
    """A `tx` byte stream did not match what the replay log expected."""


class ReplayExhaustedError(RuntimeError):
    """The replay log ran out before the caller stopped reading."""


# Tiny convenience for tests — synthesize a JSONL file from a sequence of
# (direction, bytes) tuples without a real BMS.
def write_jsonl(path: Path, exchanges: list[tuple[str, bytes]]) -> None:
    """Write a JSONL replay file from `(direction, bytes)` pairs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for direction, data in exchanges:
            if direction not in ("rx", "tx"):
                raise ValueError(f"direction must be rx|tx, got {direction!r}")
            fh.write(
                json.dumps({"ts": 0, "dir": direction, "hex": data.hex()})
            )
            fh.write("\n")


# Sentinel so callers can `await asyncio.sleep(0)` after replay-driven loops.
async def yield_to_loop() -> None:  # pragma: no cover - trivial coroutine
    await asyncio.sleep(0)
