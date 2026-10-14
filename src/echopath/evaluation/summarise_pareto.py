"""Score the Pareto runs in one currency.

The harness reports vision tokens and Jev dollars in separate columns, which
reads as though the vision fallback were free. It is the most expensive part of
every run. Pricing both against the same rate is the only way the comparison
means anything.

    python -m echopath.evaluation.summarise_pareto [results.jsonl]
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from echopath.project_paths import project_root

# Input rate for the vision fallback, per million tokens. Screenshots dominate
# the request, so output is a rounding error and is ignored.
VISION_PER_MTOK = 3.00
CONDITIONS = ("echopath", "jev-only", "echopath+jev")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="?", type=Path, help="results JSONL (default: <project>/local_data/pareto/results.jsonl)")
    args = parser.parse_args(argv)
    path = args.results.expanduser().resolve() if args.results else project_root() / "local_data" / "pareto" / "results.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows:
        print(f"no results in {path}")
        return 1

    families = sorted({r["family"] for r in rows})
    by = collections.defaultdict(list)
    for r in rows:
        by[r["condition"]].append(r)

    head = f"{'condition':14}" + "".join(f"{f:>12}" for f in families)
    print(head + f"{'overall':>10}{'vis tokens':>12}{'total $':>10}"
                 f"{'$/success':>11}{'latency':>10}")
    print("-" * len(head + " " * 53))

    for condition in CONDITIONS:
        runs = by.get(condition, [])
        if not runs:
            continue
        cells = ""
        for family in families:
            sub = [r for r in runs if r["family"] == family]
            cells += f"{sum(r['completed'] for r in sub)}/{len(sub):<9}".rjust(12)
        ok = sum(r["completed"] for r in runs)
        tokens = sum(r["tokens"] for r in runs)
        total = sum(r["cost"] for r in runs) + tokens / 1e6 * VISION_PER_MTOK
        per = total / ok if ok else float("nan")
        latency = sum(r["latency_ms"] for r in runs) // len(runs)
        print(f"{condition:14}{cells}{f'{ok}/{len(runs)}':>10}{tokens:>12,}"
              f"{total:>10.4f}{per:>11.4f}{latency:>9}ms")

    print()
    print("where each mechanism was used, and how sure it was")
    for condition in CONDITIONS:
        runs = by.get(condition, [])
        if not runs:
            continue
        crop_hit = [r for r in runs if r["used_crop"]]
        crop_miss = [r["crop_score"] for r in runs
                     if r["crop_score"] is not None and not r["used_crop"]]
        jev = [r["jev_confidence"] for r in runs if r["used_jev"]
               and r["jev_confidence"] is not None]
        parts = [f"crop hit {len(crop_hit)}/{len(runs)}"]
        if crop_miss:
            parts.append(f"missed at {min(crop_miss):.2f}-{max(crop_miss):.2f}")
        if jev:
            parts.append(f"jev {len(jev)} calls, confidence "
                         f"{min(jev):.2f}-{max(jev):.2f}")
        parts.append(f"vision {sum(r['used_vision'] for r in runs)}")
        print(f"  {condition:14} " + "; ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
