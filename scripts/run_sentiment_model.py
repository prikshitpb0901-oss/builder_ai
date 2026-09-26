#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.sentiment import sentiment_input_eligibility  # noqa: E402

MODEL_ID = "NOSIBLE/financial-sentiment-v1.2-base"
MODEL_REVISION = "acc796e59f4b568fe73e127de81c10a982b88845"
SYSTEM_PROMPT = "Classify the financial sentiment as positive, neutral, or negative."
OUTPUT_LABELS = ("positive", "neutral", "negative")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def normalize_generated_label(value: str) -> str | None:
    normalized = value.strip().casefold().strip(".,:;!?\"'")
    return normalized if normalized in OUTPUT_LABELS else None


def label_token_ids(tokenizer) -> dict[str, list[int]]:
    output: dict[str, list[int]] = {}
    for label in OUTPUT_LABELS:
        ids = set()
        for variant in (label, " " + label):
            encoded = tokenizer.encode(variant, add_special_tokens=False)
            if len(encoded) == 1:
                ids.add(encoded[0])
        if not ids:
            raise RuntimeError(f"Pinned tokenizer does not encode {label!r} as one token")
        output[label] = sorted(ids)
    return output


def load_model():
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("Install the optional model runtime with: uv sync --extra sentiment") from exc

    if torch.cuda.is_available():
        device, dtype = "cuda", torch.bfloat16
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        device, dtype = "mps", torch.float16
    else:
        device, dtype = "cpu", torch.float32
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID,
        revision=MODEL_REVISION,
        trust_remote_code=True,
        dtype=dtype,
    ).to(device)
    model.eval()
    return model, tokenizer, torch, device


def classify(model, tokenizer, torch, device: str, text: str) -> tuple[str, dict[str, float]]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    prompt = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,
    )
    inputs = tokenizer([prompt], return_tensors="pt").to(device)
    ids_by_label = label_token_ids(tokenizer)
    allowed_ids = sorted({token_id for ids in ids_by_label.values() for token_id in ids})
    with torch.inference_mode():
        generated = model.generate(
            **inputs,
            max_new_tokens=1,
            do_sample=False,
            prefix_allowed_tokens_fn=lambda _batch_id, _input_ids: allowed_ids,
            return_dict_in_generate=True,
            output_scores=True,
        )
    token_id = generated.sequences[0, -1].item()
    label = normalize_generated_label(tokenizer.decode([token_id], skip_special_tokens=True))
    if label is None:
        label = next((name for name, ids in ids_by_label.items() if token_id in ids), None)
    if label is None:
        raise RuntimeError("Constrained generation returned an unknown label token")
    logits = generated.scores[0][0]
    label_logits = torch.stack([torch.max(logits[ids_by_label[name]]) for name in OUTPUT_LABELS])
    probabilities = torch.softmax(label_logits.float(), dim=0).cpu().tolist()
    return label, {name: round(float(value), 8) for name, value in zip(OUTPUT_LABELS, probabilities)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the pinned NOSIBLE baseline on exact-company, independently sourced snippets.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--corpus-label", default="unspecified_unqualified_input")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")

    items = read_jsonl(Path(args.input))
    eligible = []
    rejected = []
    for item in items[:args.limit]:
        accepted, reasons = sentiment_input_eligibility(item)
        if accepted:
            eligible.append(item)
        else:
            rejected.append({"id": item.get("id"), "reasons": reasons})

    model, tokenizer, torch, device = load_model()
    predictions = []
    latencies = []
    for item in eligible:
        started = time.monotonic()
        label, probabilities = classify(model, tokenizer, torch, device, str(item["text"]))
        latencies.append(int((time.monotonic() - started) * 1000))
        predictions.append({
            "id": item["id"],
            "organisation_number": item.get("organisation_number"),
            "label": label,
            "label_probabilities": probabilities,
            "exact_entity": True,
            "source_class": item["source_class"],
            "source_url": item["source_url"],
            "retrieved_at": item["retrieved_at"],
            "evidence_span": item["evidence_span"],
            "content_sha256": item["content_sha256"],
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "system_prompt": SYSTEM_PROMPT,
            "thinking_enabled": False,
            "generation_constraint": "single token restricted to positive|neutral|negative",
        })

    write_jsonl(Path(args.predictions), predictions)
    ordered = sorted(latencies)
    p50 = ordered[(len(ordered) - 1) // 2] if ordered else None
    p95 = ordered[int((len(ordered) - 1) * 0.95)] if ordered else None
    report = {
        "generated_at": utc_now(),
        "corpus_label": args.corpus_label,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "device": device,
        "input_items": min(len(items), args.limit),
        "eligible_items": len(eligible),
        "rejected_items": rejected,
        "predictions": len(predictions),
        "latency_ms": {"p50": p50, "p95": p95},
        "qualification": "not_evaluated_use_evaluate_sentiment.py_on_frozen_gold",
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
