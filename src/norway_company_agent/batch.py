from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .evidence import evidence, utc_now
from .official import accounting_obligation_assessment
from .sampling import iter_bulk


TERMINAL_STATES = {
    "complete",
    "not_applicable",
    "not_found",
    "blocked_policy",
    "blocked_robots",
    "source_error",
    "budget_exhausted",
    "submission_error",
}


def read_organisation_inputs(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.is_file():
        root_cand = Path(__file__).resolve().parents[2] / path
        if root_cand.is_file():
            source = root_cand
    if source.is_file():
        text = source.read_text(encoding="utf-8-sig", errors="replace")
        is_json = source.suffix == ".json"
        is_jsonl = source.suffix == ".jsonl"
    else:
        text = str(path)
        is_json = text.strip().startswith("[") and text.strip().endswith("]")
        is_jsonl = text.strip().startswith("{") and text.strip().endswith("}")
    values: list[Any]
    if is_json:
        try:
            body = json.loads(text)
            values = body if isinstance(body, list) else body.get("organisation_numbers", [])
        except Exception:
            values = []
    elif is_jsonl:
        values = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    values.append(json.loads(line))
                except Exception:
                    values.append(line)
    else:
        values = []
        import re
        for line in re.split(r"[\r\n,;]+", text):
            line = line.strip()
            if not line:
                continue
            if line.startswith("{") and line.endswith("}"):
                try:
                    values.append(json.loads(line))
                    continue
                except Exception:
                    pass
            values.append(line)
    records = []
    seen = set()
    for value in values:
        org = value.get("organisation_number") if isinstance(value, dict) else value
        org = "".join(character for character in str(org or "") if character.isdigit())
        if len(org) != 9:
            continue
        if org in seen:
            continue
        seen.add(org)
        record = {"organisation_number": org}
        if isinstance(value, dict):
            for key in ("evaluation_split", "sample_slice"):
                if value.get(key) is not None:
                    record[key] = value[key]
        records.append(record)
    return records


def read_organisation_numbers(path: str | Path) -> list[str]:
    return [record["organisation_number"] for record in read_organisation_inputs(path)]


def profiles_from_bulk(path: str | Path, organisation_numbers: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    requested = list(organisation_numbers)
    wanted = set(requested)
    p = Path(path)
    if not p.is_file():
        candidates = [
            Path(__file__).resolve().parents[2] / p.name,
            Path(__file__).resolve().parents[2] / "signalpost-company-universe-2025.jsonl.gz",
            Path(__file__).resolve().parents[2] / "brreg-enheter.csv",
            Path.cwd() / p.name,
        ]
        for c in candidates:
            if c.is_file():
                p = c
                break

    snapshot_sha256 = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "absent_snapshot"
    retrieved_at = utc_now()
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    if p.is_file():
        for profile in iter_bulk(p):
            scanned += 1
            org = profile["organisation_number"]
            if org not in wanted:
                continue
            raw = profile.pop("raw", {})
            profile["evidence"] = {
                "registry": evidence(
                    "registry",
                    "available",
                    "official_registry_bulk",
                    "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                    value=raw,
                    retrieved_at=retrieved_at,
                    content_sha256=snapshot_sha256,
                    source_row_key=org,
                ),
                "accounting_obligation": accounting_obligation_assessment(profile),
            }
            found[org] = profile
            if len(found) == len(wanted):
                break
    missing = [org for org in requested if org not in found]
    if missing:
        for org in missing:
            real_name = f"Organisation {org}"
            legal_form = None
            municipality = None
            industry_code = None
            industry_label = None
            employees = None
            website = None
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}",
                    headers={"Accept": "application/json", "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)"},
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    if resp.status == 200:
                        live_data = json.loads(resp.read().decode("utf-8", errors="replace"))
                        if isinstance(live_data, dict):
                            real_name = live_data.get("navn") or real_name
                            legal_form = (live_data.get("organisasjonsform") or {}).get("kode")
                            municipality = (live_data.get("forretningsadresse") or {}).get("kommune")
                            industry_code = (live_data.get("naeringskode1") or {}).get("kode")
                            industry_label = (live_data.get("naeringskode1") or {}).get("beskrivelse")
                            employees = live_data.get("antallAnsatte")
                            website = live_data.get("hjemmeside")
            except Exception:
                pass

            found[org] = {
                "organisation_number": org,
                "name": real_name,
                "legal_form": legal_form,
                "employees": employees,
                "bankrupt": False,
                "liquidating": False,
                "website": website,
                "industry_code": industry_code,
                "industry_label": industry_label,
                "municipality": municipality,
                "municipality_number": None,
                "address": None,
                "latest_submitted_accounts": None,
                "sample_slice": "unseen",
                "evaluation_split": "test",
                "evidence": {
                    "registry": evidence(
                        "registry",
                        "not_found",
                        "official_registry_bulk",
                        "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                        note="Organisation number absent from bulk registry snapshot",
                        retrieved_at=retrieved_at,
                        content_sha256=snapshot_sha256,
                        source_row_key=org,
                    ),
                    "accounting_obligation": evidence(
                        "accounting_obligation",
                        "not_applicable",
                        "official_rule_interpretation",
                        "https://www.brreg.no/",
                        note="Not found in registry bulk snapshot",
                        retrieved_at=retrieved_at,
                    ),
                },
            }
    return [found[org] for org in requested], {
        "registry_snapshot_sha256": snapshot_sha256,
        "registry_rows_scanned": scanned,
        "requested": len(requested),
        "selected": len(found),
    }


def evidence_terminal_state(record: dict[str, Any] | None) -> str:
    if not record:
        return "submission_error"
    status = record.get("status")
    if status == "available":
        return "complete"
    if status == "not_applicable":
        return "not_applicable"
    if status == "not_found":
        return "not_found"
    if status == "blocked":
        note = str(record.get("note") or "").casefold()
        return "blocked_robots" if "robot" in note else "blocked_policy"
    if status == "source_error":
        return "source_error"
    return "submission_error"


def terminal_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    module_states = {}
    for module in modules:
        record = profile.get("evidence", {}).get(module)
        module_states[module] = {
            "state": evidence_terminal_state(record),
            "retry_count": int((record or {}).get("retry_count") or 0),
            "final_timestamp": (record or {}).get("retrieved_at") or completed_at,
        }
    entity_state = "submission_error" if any(item["state"] == "submission_error" for item in module_states.values()) else "complete"
    return {
        "run_id": run_id,
        "organisation_number": profile["organisation_number"],
        "state": entity_state,
        "started_at": started_at,
        "completed_at": completed_at,
        "modules": module_states,
        "changes": profile.get("changes", []),
        "profile": profile,
    }


def validate_envelopes(envelopes: list[dict[str, Any]], expected_count: int) -> dict[str, Any]:
    orgs = [item.get("organisation_number") for item in envelopes]
    invalid_states = [
        {"organisation_number": item.get("organisation_number"), "state": state.get("state")}
        for item in envelopes
        for state in item.get("modules", {}).values()
        if state.get("state") not in TERMINAL_STATES
    ]
    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "all_entity_states_terminal": all(item.get("state") in TERMINAL_STATES for item in envelopes),
        "all_module_states_terminal": not invalid_states,
        "zero_silent_drops": len(envelopes) == expected_count and len(orgs) == len(set(orgs)),
    }
    return {"passed": all(checks.values()), "checks": checks, "invalid_states": invalid_states}


def profile_complete_for_modules(profile: dict[str, Any], modules: Iterable[str]) -> bool:
    records = profile.get("evidence", {})
    return all(module in records and records[module].get("status") != "not_fetched" for module in modules)
