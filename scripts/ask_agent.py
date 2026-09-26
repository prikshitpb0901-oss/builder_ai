#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.research import answer_profile  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--org", required=True)
    parser.add_argument("--question", default="What do we know about this company?")
    args = parser.parse_args()
    row = next((json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines() if line.strip() and json.loads(line).get("organisation_number") == args.org), None)
    if row is None:
        raise SystemExit(f"Organisation number {args.org} is not in the frozen sample")
    print(json.dumps(answer_profile(row, args.question), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
