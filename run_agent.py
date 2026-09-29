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
    if len(sys.argv) == 1:
        bulk_candidates = [
            ROOT / "signalpost-company-universe-2025.jsonl.gz",
            ROOT / "brreg-enheter.csv",
            ROOT / "signalpost-universe.jsonl.gz",
            ROOT.parent / "signalpost-company-universe-2025.jsonl.gz",
        ]
        org_candidates = [
            ROOT / "batch-100.jsonl",
            ROOT / "entry-companies.jsonl",
            ROOT / "smoke-companies.jsonl",
        ]
        chosen_bulk = next((str(p) for p in bulk_candidates if p.exists()), "signalpost-company-universe-2025.jsonl.gz")
        chosen_orgs = next((str(p) for p in org_candidates if p.exists()), "batch-100.jsonl")
        sys.argv.extend([
            "--organisations", chosen_orgs,
            "--bulk", chosen_bulk,
            "--output-dir", "out",
        ])
    main()
