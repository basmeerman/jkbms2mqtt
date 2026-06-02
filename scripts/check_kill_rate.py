#!/usr/bin/env python3
"""Gate a mutmut run by parsing its JUnit XML output.

mutmut's JUnit convention treats each mutant as a test case. A *killed* mutant
is a passing test; a *survived* mutant is a failure. We compute
``kill_rate = killed / total`` and exit non-zero if it falls below ``--min``.

Used by ``.github/workflows/mutate.yml`` so the substring-counting hack the
workflow shipped with originally (which was case-sensitive and would silently
mis-count) is replaced with a real parser.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple


class Counts(NamedTuple):
    total: int
    killed: int
    survived: int


def count_results(xml_path: Path) -> Counts:
    """Parse a JUnit XML file and return (total, killed, survived) counts.

    Killed = the mutmut testcase passed (test suite caught the mutation).
    Survived = the testcase has a <failure> child (mutation went undetected).
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()
    testcases = root.findall(".//testcase")
    total = len(testcases)
    survived = sum(1 for tc in testcases if tc.find("failure") is not None)
    killed = total - survived
    return Counts(total=total, killed=killed, survived=survived)


def kill_rate(counts: Counts) -> float:
    """Return killed / total in [0, 1]; default 1.0 when total is zero."""
    if counts.total == 0:
        return 1.0
    return counts.killed / counts.total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml_file", type=Path, help="mutmut junitxml output")
    parser.add_argument(
        "--min",
        type=float,
        required=True,
        help="Minimum required kill rate in [0, 1].",
    )
    parser.add_argument(
        "--label",
        default="mutmut",
        help="Label printed in the summary line.",
    )
    args = parser.parse_args()

    if not 0.0 <= args.min <= 1.0:
        print(f"--min must be in [0, 1], got {args.min}", file=sys.stderr)
        return 2

    counts = count_results(args.xml_file)
    ratio = kill_rate(counts)
    print(
        f"{args.label} mutants: total={counts.total} "
        f"killed={counts.killed} survived={counts.survived} "
        f"kill_rate={ratio:.1%}"
    )
    if ratio < args.min:
        print(
            f"FAIL: kill rate {ratio:.1%} < required {args.min:.0%} on {args.label}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
