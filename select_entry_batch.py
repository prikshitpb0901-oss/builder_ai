#!/usr/bin/env python3
"""Select a reproducible entry batch from the public Signalpost universe."""

from __future__ import annotations

import argparse
import gzip
import json
import random
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", required=True, help="Public .jsonl or .jsonl.gz universe")
    parser.add_argument("--output", required=True, help="Output JSONL manifest")
    parser.add_argument("--count", type=int, default=1000, help="At least 1000 for a valid entry")
    parser.add_argument("--seed", type=int, default=20260823)
    args = parser.parse_args()
    if args.count < 1000:
        raise SystemExit("Signalpost entries must cover at least 1,000 companies")

    path = Path(args.universe)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    if args.count > len(rows):
        raise SystemExit(f"Requested {args.count}; universe contains {len(rows)}")

    chosen = random.Random(args.seed).sample(rows, args.count)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for row in chosen:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"Wrote {len(chosen):,} companies to {output}")


if __name__ == "__main__":
    main()
