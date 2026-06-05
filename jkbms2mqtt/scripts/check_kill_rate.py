#!/usr/bin/env python3
"""Gate a mutmut 3.x run by parsing ``mutmut results --all true`` output.

mutmut 3.x prints one line per mutant::

    <mutant_name>: <status>

where ``mutant_name`` is module-qualified, e.g.
``jkbms2mqtt.protocol.jk_modbus.x_decode__mutmut_3``. We bucket those lines with
``--select`` (a substring that must appear in the name, e.g. ``.protocol.`` or
``.entities.``), tally statuses, and compute::

    kill_rate = caught / (caught + missed)

and exit non-zero if it falls below ``--min``.

Status buckets (see ``mutmut.__main__.status_by_exit_code``):

* **caught**  — ``killed``, ``timeout``, ``caught by type check``. The mutation
  was detected (a test failed, the test hung, or mypy rejected it).
* **missed**  — ``survived``, ``no tests``, ``suspicious``, ``segfault``. The
  mutation slipped through, was never exercised, or gave an ambiguous result we
  refuse to count as a kill.
* **ignored** — ``skipped`` only. Excluded from the denominator entirely.

``not checked`` / ``check was interrupted by user`` mean the run did not finish;
we treat any of them as a hard error so a partial run can never look passing.

Used by ``.github/workflows/mutate.yml``. Replaces the JUnit-XML parser the 2.x
pipeline used (mutmut 3.x has no ``junitxml`` command).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

CAUGHT_STATUSES = frozenset({"killed", "timeout", "caught by type check"})
MISSED_STATUSES = frozenset({"survived", "no tests", "suspicious", "segfault"})
IGNORED_STATUSES = frozenset({"skipped"})
# Statuses that mean the run never completed — never silently pass on these.
INCOMPLETE_STATUSES = frozenset({"not checked", "check was interrupted by user"})


class Counts(NamedTuple):
    caught: int
    missed: int
    ignored: int

    @property
    def total(self) -> int:
        return self.caught + self.missed


def parse_results(text: str, *, select: str) -> tuple[Counts, list[str]]:
    """Tally ``mutmut results --all true`` lines whose name contains ``select``.

    Returns the counts plus the list of missed-mutant names (for reporting).
    Raises ``ValueError`` if any selected mutant is in an incomplete state.
    """
    caught = missed = ignored = 0
    missed_names: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or ": " not in line:
            continue
        name, _, status = line.rpartition(": ")
        name = name.strip()
        status = status.strip()
        if select not in name:
            continue
        if status in INCOMPLETE_STATUSES:
            raise ValueError(
                f"mutant {name!r} is {status!r} — the mutmut run did not finish"
            )
        if status in CAUGHT_STATUSES:
            caught += 1
        elif status in MISSED_STATUSES:
            missed += 1
            missed_names.append(name)
        elif status in IGNORED_STATUSES:
            ignored += 1
        else:  # unknown status — be conservative and count it as missed
            missed += 1
            missed_names.append(f"{name} ({status})")
    return Counts(caught=caught, missed=missed, ignored=ignored), missed_names


def kill_rate(counts: Counts) -> float:
    """Return caught / total in [0, 1]; 1.0 when nothing was mutated."""
    if counts.total == 0:
        return 1.0
    return counts.caught / counts.total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "results_file",
        type=Path,
        help="output of `mutmut results --all true`",
    )
    parser.add_argument(
        "--min",
        type=float,
        required=True,
        help="Minimum required kill rate in [0, 1].",
    )
    parser.add_argument(
        "--select",
        required=True,
        help="Substring a mutant name must contain to be counted "
        "(e.g. '.protocol.' or '.entities.').",
    )
    parser.add_argument(
        "--label",
        default="mutmut",
        help="Label printed in the summary line.",
    )
    parser.add_argument(
        "--show-survivors",
        action="store_true",
        help="List the missed mutants (helps when expanding tests).",
    )
    args = parser.parse_args()

    if not 0.0 <= args.min <= 1.0:
        print(f"--min must be in [0, 1], got {args.min}", file=sys.stderr)
        return 2

    text = args.results_file.read_text()
    try:
        counts, missed_names = parse_results(text, select=args.select)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    if counts.total == 0:
        print(
            f"FAIL: no mutants matched --select {args.select!r} on {args.label} "
            "— check paths_to_mutate / the select filter",
            file=sys.stderr,
        )
        return 2

    ratio = kill_rate(counts)
    print(
        f"{args.label} mutants: total={counts.total} "
        f"caught={counts.caught} missed={counts.missed} "
        f"ignored={counts.ignored} kill_rate={ratio:.1%}"
    )
    if args.show_survivors and missed_names:
        print(f"  survivors ({len(missed_names)}):")
        for name in missed_names:
            print(f"    {name}")
    if ratio < args.min:
        print(
            f"FAIL: kill rate {ratio:.1%} < required {args.min:.0%} on {args.label}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
