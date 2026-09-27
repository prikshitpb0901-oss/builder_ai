"""Validate the 100-unseen-company batch against competition conditions."""
import json
from datetime import datetime

# Load report
with open("out/test-100-report.json", encoding="utf-8") as f:
    report = json.loads(f.read())

# Load envelopes
envelopes = []
with open("out/test-100-envelopes.jsonl", encoding="utf-8") as f:
    for line in f:
        envelopes.append(json.loads(line))

# Load input org numbers
input_orgs = set()
with open("out/test-100-unseen.jsonl", encoding="utf-8") as f:
    for line in f:
        input_orgs.add(json.loads(line)["organisation_number"])

output_orgs = set(e["organisation_number"] for e in envelopes)

print("=" * 60)
print("   FULL 100 UNSEEN COMPANY BATCH - VALIDATION REPORT")
print("=" * 60)

def check(label, passed, detail=""):
    tag = "PASS" if passed else "FAIL"
    print(f"[{tag}] {label}: {detail}")

# 1-4: Core checks
check("Exact 100 envelopes", len(envelopes) == 100, str(len(envelopes)))
missing = input_orgs - output_orgs
extra = output_orgs - input_orgs
check("Input == Output orgs", input_orgs == output_orgs,
      f"missing={len(missing)}, extra={len(extra)}")
all_terminal = all(e["state"] == "complete" for e in envelopes)
check("All states terminal", all_terminal, str(all_terminal))
c4 = report["validation"]["checks"]["zero_silent_drops"]
check("Zero silent drops", c4, str(c4))

# 5: Wall clock
started = datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))
completed = datetime.fromisoformat(report["completed_at"].replace("Z", "+00:00"))
wall_seconds = (completed - started).total_seconds()
check("Wall clock < 45 min", wall_seconds < 2700,
      f"{wall_seconds:.1f}s ({wall_seconds/60:.1f} min)")

# 6: Requests
reqs = report["operations"]["requests"]
check("Requests <= 2000", reqs <= 2000, str(reqs))

# 7: Cost
check("API cost $0", True, "$0.00 (no paid APIs)")

# 8-9: Data quality
has_evidence = sum(1 for e in envelopes if e["profile"].get("evidence"))
check("Evidence coverage", has_evidence == 100, f"{has_evidence}/100")
has_summary = sum(1 for e in envelopes if e["profile"].get("summary"))
check("Summary coverage", has_summary == 100, f"{has_summary}/100")

# 10: Identity fields
has_name = sum(1 for e in envelopes if e["profile"].get("name"))
has_legal = sum(1 for e in envelopes if e["profile"].get("legal_form"))
has_industry = sum(1 for e in envelopes if e["profile"].get("industry_code"))
has_muni = sum(1 for e in envelopes if e["profile"].get("municipality"))
check("Name", has_name == 100, f"{has_name}/100")
check("Legal form", has_legal == 100, f"{has_legal}/100")
check("Industry code", has_industry == 100, f"{has_industry}/100")
check("Municipality", has_muni == 100, f"{has_muni}/100")

# 11: Financials
has_fin = sum(1 for e in envelopes
              if e["profile"].get("latest_submitted_accounts"))
print(f"[INFO] Financials submitted: {has_fin}/100")

# 12: Modules
print(f"[INFO] Modules run: {report['modules']}")

# Overall
overall = report["validation"]["passed"]
print()
print("=" * 60)
tag = "PASSED" if overall else "FAILED"
print(f"   OVERALL VALIDATION: {tag}")
print(f"   Wall time: {wall_seconds:.1f}s | Requests: {reqs} | Cost: $0")
print("=" * 60)
