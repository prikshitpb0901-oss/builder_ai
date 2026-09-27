#!/usr/bin/env python3
"""Main competition entry point for Signalpost / Builderr evaluator.

Supports standard competition invocation:
    python run_agent.py --organisations batch.txt --bulk brreg-enheter.csv --output-dir out/
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from scripts.run_competition_batch import main

if __name__ == "__main__":
    main()
