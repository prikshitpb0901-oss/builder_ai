#!/usr/bin/env python3
"""Main competition entry point for Signalpost / Builderr evaluator.

Supports standard competition invocation:
    python run_agent.py --organisations batch.txt --bulk brreg-enheter.csv --output-dir out/
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Automatically detect and include .venv site-packages if available and not yet on path
for site_pkg in (ROOT / ".venv").glob("**/site-packages"):
    if site_pkg.is_dir() and str(site_pkg) not in sys.path:
        sys.path.insert(0, str(site_pkg))

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from scripts.run_competition_batch import main

if __name__ == "__main__":
    args = sys.argv[1:]

    # 1. Handle positional argument if provided (e.g. `python run_agent.py daily_100.txt`)
    org_flag_present = any(arg in args for arg in ("--organisations", "--orgs", "-i"))
    if not org_flag_present:
        positional = [a for a in args if not a.startswith("-")]
        if positional:
            target_org = positional[0]
            args.remove(target_org)
            args.extend(["--organisations", target_org])
            org_flag_present = True

    # 2. Provide smart defaults for any missing essential arguments
    bulk_candidates = [
        ROOT / "signalpost-company-universe-2025.jsonl.gz",
        ROOT / "brreg-enheter.csv",
        ROOT / "signalpost-universe.jsonl.gz",
        ROOT.parent / "signalpost-company-universe-2025.jsonl.gz",
        Path.cwd() / "signalpost-company-universe-2025.jsonl.gz",
        Path.cwd() / "brreg-enheter.csv",
    ]
    chosen_bulk = next((str(p) for p in bulk_candidates if p.exists()), str(ROOT / "signalpost-company-universe-2025.jsonl.gz"))

    if not org_flag_present:
        org_candidates = [
            ROOT / "smoke-100.jsonl",
            ROOT / "batch-100.jsonl",
            ROOT / "entry-companies.jsonl",
            Path.cwd() / "smoke-100.jsonl",
            Path.cwd() / "batch-100.jsonl",
        ]
        chosen_orgs = next((str(p) for p in org_candidates if p.exists()), str(ROOT / "smoke-100.jsonl"))
        args.extend(["--organisations", chosen_orgs])

    bulk_flag_present = any(arg in args for arg in ("--bulk", "-b"))
    if not bulk_flag_present:
        args.extend(["--bulk", chosen_bulk])

    out_flag_present = any(arg in args for arg in ("--output-dir", "--output_dir", "-d", "--output", "-o"))
    if not out_flag_present:
        args.extend(["--output-dir", "out"])

    sys.argv = [sys.argv[0]] + args
    main()
