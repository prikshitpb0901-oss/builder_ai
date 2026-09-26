#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader


UA = "SignalpostResearchPOC/1.0 (+https://builderr.ai)"
OCR_NUMBER = r"(?-i:\b[0-9O][0-9O .,-]{0,8})"
PATTERNS = (
    (0, "full_time_equivalents", re.compile(rf"(?i)(?:antall|tal\s+p[aå])\s+(?:aarsverk|arsverk|årsverk)\s+i\s+(?:regnskapsaret|rekneskapsaret)\s*(?:er|:|=)?\s*({OCR_NUMBER})")),
    (0, "full_time_equivalents", re.compile(rf"(?i)antall\s+(?:aarsverk|arsverk|årsverk)(?:\s+sysselsatt\s+i\s+regnskapsaret)?\s*(?:er|:|=)?\s*({OCR_NUMBER})")),
    (0, "full_time_equivalents", re.compile(rf"(?i)selskapet\s+har(?:\s+[i1]\s+\d{{4}})?\s+sysselsatt\s+({OCR_NUMBER})\s+(?:aarsverk|arsverk|årsverk)")),
    (0, "full_time_equivalents", re.compile(rf"(?i)selskapet\s+har\s+({OCR_NUMBER})\s+(?:aarsverk|arsverk|årsverk)")),
    (0, "full_time_equivalents", re.compile(rf"(?i)antall\s+(?:aarsverk|arsverk|årsverk)\s+(?:sysselsatt|syssetsatt)\s+i\s+regnskapsaret\s*(?:er|:|=)?\s*({OCR_NUMBER})")),
    (1, "employees", re.compile(rf"(?i)gjennomsnittlig(?:e)?\s+antall\s+ansatte(?:\s+i\s+regnskapsaret)?\s*(?:er|:|=)?\s*({OCR_NUMBER})")),
    (1, "employees", re.compile(rf"(?i)antall\s+ansatte\s*(?:er|:|=)?\s*({OCR_NUMBER})")),
    (2, "employees", re.compile(rf"(?i)({OCR_NUMBER})\s+(?:heltids)?ansatte\b")),
)
WORD_COUNTS = {"ingen": 0, "en": 1, "ett": 1, "to": 2, "tre": 3, "fire": 4, "fem": 5}
WORD_EMPLOYEE_PATTERN = re.compile(
    r"(?i)\b(?:det\s+er|selskapet\s+har)\s+(ingen|en|ett|to|tre|fire|fem)\s+ansatte\b"
)
ZERO_WORKFORCE_PATTERN = re.compile(
    r"(?i)\b(?:selskapet|stiftelsen|legatet|sameiet|det)\s+"
    r"(?:har\s+ingen\s+(ansatte|(?:aarsverk|arsverk|årsverk))|"
    r"har\s+ikke\s+hatt\s+(?:noen\s+)?ansatte|"
    r"hadde\s+ingen\s+ansatte|"
    r"ikke\s+har\s+ansatte)\b"
)
WORKFORCE_TERMS = re.compile(r"(?i)ansatt|aarsverk|arsverk|årsverk|sysselsatt")


def needs_ocr(text: str) -> bool:
    """OCR image-heavy reports even when a small machine-readable cover exists."""
    return len(text.strip()) < 100 or not WORKFORCE_TERMS.search(text)


def number_value(value: str) -> int | float | None:
    cleaned = value.replace(" ", "").replace("O", "0").strip(".,-")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif re.search(r"\.\d{1,2}$", cleaned):
        pass
    else:
        cleaned = cleaned.replace(".", "")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if not 0 <= number <= 100_000:
        return None
    return int(number) if number.is_integer() else round(number, 2)


def extract_candidate(text: str) -> tuple[int | float | None, str | None, str, str | None]:
    compact = re.sub(r"[\t\r ]+", " ", text)
    matches = []
    for priority, measure, pattern in PATTERNS:
        for match in pattern.finditer(compact):
            start = max(0, compact.rfind("\n", 0, match.start()) + 1)
            end_pos = compact.find("\n", match.end())
            end = len(compact) if end_pos < 0 else end_pos
            span = compact[start:end].strip()[:500]
            if re.search(r"(?i)konsern|group", span):
                continue
            count = number_value(match.group(1))
            if count is not None:
                matches.append((priority, count, span, measure))
    for match in WORD_EMPLOYEE_PATTERN.finditer(compact):
        start = max(0, compact.rfind("\n", 0, match.start()) + 1)
        end_pos = compact.find("\n", match.end())
        end = len(compact) if end_pos < 0 else end_pos
        span = compact[start:end].strip()[:500]
        if not re.search(r"(?i)konsern|group", span):
            matches.append((2, WORD_COUNTS[match.group(1).casefold()], span, "employees"))
    for match in ZERO_WORKFORCE_PATTERN.finditer(compact):
        start = max(0, compact.rfind("\n", 0, match.start()) + 1)
        end_pos = compact.find("\n", match.end())
        end = len(compact) if end_pos < 0 else end_pos
        span = compact[start:end].strip()[:500]
        if not re.search(r"(?i)konsern|group", span):
            measure = "full_time_equivalents" if match.group(1) and re.search(r"(?i)verk", match.group(1)) else "employees"
            matches.append((0, 0, span, measure))
    if not matches:
        return None, None, "no_employee_phrase", None
    best_priority = min(item[0] for item in matches)
    best = [item for item in matches if item[0] == best_priority]
    values = {item[1] for item in best}
    if len(values) != 1:
        return None, None, "conflicting_employee_counts", None
    chosen = sorted(best, key=lambda item: item[3] != "full_time_equivalents")[0]
    return chosen[1], chosen[2], "accepted", chosen[3]


def ocr_pdf(pdf_path: Path, *, pages: int, dpi: int) -> str:
    with tempfile.TemporaryDirectory(prefix="signalpost-annual-ocr-") as temporary:
        prefix = Path(temporary) / "page"
        subprocess.run(
            ["pdftoppm", "-f", "1", "-l", str(pages), "-jpeg", "-r", str(dpi), str(pdf_path), str(prefix)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
        )
        text = []
        for image_path in sorted(Path(temporary).glob("page-*.jpg")):
            completed = subprocess.run(
                ["tesseract", str(image_path), "stdout", "-l", "eng", "--psm", "6"],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            text.append(completed.stdout)
        return "\n".join(text)


def collect(profile: dict, cache_dir: Path, *, ocr_pages: int, ocr_dpi: int) -> tuple[dict | None, dict]:
    org = str(profile["organisation_number"])
    registry = ((profile.get("evidence") or {}).get("registry") or {}).get("value") or {}
    if str(registry.get("antallAnsatte") or "").isdigit():
        return None, {"organisation_number": org, "status": "registry_count_already_available"}
    history = (profile.get("evidence") or {}).get("financial_history") or {}
    pdfs = (history.get("value") or {}).get("pdfs") or []
    if not pdfs:
        return None, {"organisation_number": org, "status": "no_annual_report"}
    latest = sorted(pdfs, key=lambda item: str(item.get("year") or ""), reverse=True)[0]
    url = str(latest["url"])
    cache_path = cache_dir / f"{org}-{latest['year']}.pdf"
    try:
        if cache_path.exists():
            raw = cache_path.read_bytes()
            cache_hit = True
        else:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read(20_000_001)
            if len(raw) > 20_000_000 or not raw.startswith(b"%PDF"):
                return None, {"organisation_number": org, "status": "unsupported_pdf", "bytes": len(raw)}
            cache_path.write_bytes(raw)
            cache_hit = False
        reader = PdfReader(io.BytesIO(raw), strict=False)
        pages = []
        for page in reader.pages[:120]:
            pages.append(page.extract_text() or "")
        text = "\n".join(pages)
        ocr_used = False
        ocr_cache_path = cache_dir / f"{org}-{latest['year']}-ocr-{ocr_pages}-{ocr_dpi}.txt"
        if needs_ocr(text) and ocr_pages > 0:
            if ocr_cache_path.exists():
                ocr_text = ocr_cache_path.read_text(encoding="utf-8", errors="replace")
            else:
                ocr_text = ocr_pdf(cache_path, pages=min(ocr_pages, len(reader.pages)), dpi=ocr_dpi)
                ocr_cache_path.write_text(ocr_text, encoding="utf-8")
            # Preserve the exact organisation number from the digital cover
            # while adding the OCR-only notes used for workforce extraction.
            text = text + "\n" + ocr_text
            ocr_used = True
        if org not in re.sub(r"\D", "", text):
            return None, {"organisation_number": org, "status": "organisation_number_not_in_pdf", "cache_hit": cache_hit, "ocr_used": ocr_used}
        count, span, status, measure = extract_candidate(text)
        if count is None:
            return None, {"organisation_number": org, "status": status, "cache_hit": cache_hit, "pages": len(reader.pages), "ocr_used": ocr_used}
        digest = hashlib.sha256(raw).hexdigest()
        metrics = {"workforce_value": count, "measure": measure, "year": str(latest["year"]), "scope": "company_phrase"}
        metrics[str(measure)] = count
        observation = {
            "id": "annual-workforce-" + hashlib.sha256(f"{org}|{latest['year']}|{count}|{digest}".encode()).hexdigest()[:24],
            "organisation_number": org,
            "platform": "brreg",
            "signal_type": "workforce_snapshot",
            "source_url": url,
            "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "content_sha256": digest,
            "exact_entity": True,
            "identity_proof": [
                {"type": "official_report_url_organisation_number", "value": org},
                {"type": "organisation_number_in_pdf", "value": org},
            ],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "official_annual_account_copy",
            "evidence_span": span,
            "effective_at": str(latest["year"]),
            "metrics": metrics,
            "strategy": "annual_report_workforce_snapshot",
        }
        return observation, {"organisation_number": org, "status": "accepted", "workforce_value": count, "measure": measure, "cache_hit": cache_hit, "ocr_used": ocr_used}
    except Exception as exc:
        return None, {"organisation_number": org, "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:180]}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract conservative company-scope workforce counts from official annual-report PDFs.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--organisations", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ocr-pages", type=int, default=15)
    parser.add_argument("--ocr-dpi", type=int, default=130)
    args = parser.parse_args()
    wanted = [line.strip() for line in Path(args.organisations).read_text().splitlines() if line.strip()]
    profile_map = {
        str(row["organisation_number"]): row
        for row in (json.loads(line) for line in Path(args.profiles).read_text().splitlines() if line.strip())
        if str(row["organisation_number"]) in set(wanted)
    }
    eligible = []
    for org in wanted:
        profile = profile_map[org]
        registry = ((profile.get("evidence") or {}).get("registry") or {}).get("value") or {}
        pdfs = ((((profile.get("evidence") or {}).get("financial_history") or {}).get("value") or {}).get("pdfs") or [])
        if not str(registry.get("antallAnsatte") or "").isdigit() and pdfs:
            eligible.append(profile)
    if args.limit:
        eligible = eligible[: args.limit]
    cache_dir = Path(args.cache)
    cache_dir.mkdir(parents=True, exist_ok=True)
    collected = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(collect, profile, cache_dir, ocr_pages=args.ocr_pages, ocr_dpi=args.ocr_dpi): str(profile["organisation_number"])
            for profile in eligible
        }
        for future in as_completed(futures):
            collected[futures[future]] = future.result()
    observations = [collected[str(profile["organisation_number"])][0] for profile in eligible if collected[str(profile["organisation_number"])][0]]
    company_results = [collected[str(profile["organisation_number"])][1] for profile in eligible]
    Path(args.output).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in observations), encoding="utf-8")
    statuses = {}
    for row in company_results:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
    report = {
        "connector": "official_annual_report_workforce_v1",
        "eligible": len(eligible),
        "accepted": len(observations),
        "status_counts": statuses,
        "claim_boundary": "Latest official PDF, exact organisation number in document, company-scope employee phrase only; group phrases and conflicts abstain.",
        "company_results": company_results,
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "company_results"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
