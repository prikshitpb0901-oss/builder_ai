from __future__ import annotations

from collections import Counter
from typing import Any
from urllib.parse import urlparse


LABELS = ("positive", "neutral", "negative", "mixed")
INDEPENDENT_SOURCE_CLASSES = {"licensed_news", "public_news", "official_notice", "licensed_review"}


def sentiment_input_eligibility(item: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons = []
    if not item.get("exact_entity"):
        reasons.append("exact company identity is not verified")
    if item.get("source_class") not in INDEPENDENT_SOURCE_CLASSES:
        reasons.append("source is not an allowed independent class")
    for field in ("source_url", "retrieved_at", "evidence_span", "content_sha256", "text"):
        if not item.get(field):
            reasons.append(f"missing {field}")
    return not reasons, reasons


def publishable_sentiment_item(item: dict[str, Any]) -> bool:
    eligible, _ = sentiment_input_eligibility({**item, "text": item.get("text") or item.get("evidence_span")})
    return bool(eligible and item.get("label") in LABELS)


def _independent_source_id(item: dict[str, Any]) -> str:
    if item.get("source_id"):
        return str(item["source_id"]).strip().casefold()
    host = (urlparse(str(item.get("source_url") or "")).hostname or "").casefold()
    return host.removeprefix("www.")


def aggregate_company_sentiment(items: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = [item for item in items if publishable_sentiment_item(item)]
    independent_sources = {_independent_source_id(item) for item in accepted}
    independent_sources.discard("")
    rejected = len(items) - len(accepted)
    if len(independent_sources) < 2:
        return {
            "status": "abstain",
            "label": None,
            "accepted_items": len(accepted),
            "rejected_items": rejected,
            "reason": "At least two independently sourced, exact-entity items are required for a company-level rollup.",
        }
    counts = Counter(item["label"] for item in accepted)
    non_neutral = {label for label in counts if label != "neutral" and counts[label]}
    label = next(iter(non_neutral)) if len(non_neutral) == 1 else "mixed" if len(non_neutral) > 1 else "neutral"
    return {
        "status": "available",
        "label": label,
        "accepted_items": len(accepted),
        "rejected_items": rejected,
        "label_counts": dict(counts),
        "independent_source_ids": sorted(independent_sources),
        "source_urls": sorted({item["source_url"] for item in accepted}),
        "warning": "Contextual dated signal, not a timeless fact about the company.",
    }


def evaluate_predictions(
    gold: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    *,
    minimum_items: int = 300,
) -> dict[str, Any]:
    if minimum_items < 300:
        raise ValueError("The POC sentiment qualification minimum cannot be lower than 300 items")
    gold_ids = [str(item["id"]) for item in gold]
    prediction_ids = [str(item["id"]) for item in predictions]
    if len(set(gold_ids)) != len(gold_ids):
        raise ValueError("Gold item IDs must be unique")
    if len(set(prediction_ids)) != len(prediction_ids):
        raise ValueError("Prediction item IDs must be unique")
    if any(item.get("label") not in LABELS for item in gold):
        raise ValueError(f"Gold labels must be one of: {', '.join(LABELS)}")

    predicted = {str(item["id"]): item for item in predictions}
    per_label = {}
    published_items = [
        predicted[item_id]
        for item_id in gold_ids
        if item_id in predicted and predicted[item_id].get("label") in LABELS
    ]
    total_correct = sum(
        predicted.get(str(item["id"]), {}).get("label") == item["label"]
        for item in gold
    )
    wrong_entity = sum(not item.get("exact_entity", False) for item in published_items)
    supported = sum(publishable_sentiment_item(item) for item in published_items)
    company_owned = sum(item.get("source_class") == "company_owned" for item in published_items)
    active_labels = tuple(label for label in LABELS if any(item["label"] == label for item in gold))
    for label in LABELS:
        tp = fp = fn = 0
        for item in gold:
            prediction = predicted.get(str(item["id"]))
            predicted_label = prediction.get("label") if prediction else None
            actual = item["label"]
            tp += actual == label and predicted_label == label
            fp += actual != label and predicted_label == label
            fn += actual == label and predicted_label != label
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_label[label] = {"precision": precision, "recall": recall, "f1": f1, "support": sum(item["label"] == label for item in gold)}
    macro_f1 = sum(per_label[label]["f1"] for label in active_labels) / len(active_labels) if active_labels else 0.0
    published = len(published_items)
    required_labels_present = all(per_label[label]["support"] > 0 for label in ("positive", "neutral", "negative"))
    exact_entity_precision = (published - wrong_entity) / published if published else None
    evidence_support_rate = supported / published if published else None
    coverage = published / len(gold) if gold else None
    gate = bool(
        len(gold) >= minimum_items
        and required_labels_present
        and wrong_entity == 0
        and company_owned == 0
        and macro_f1 >= 0.8
        and per_label["positive"]["precision"] >= 0.9
        and per_label["negative"]["precision"] >= 0.9
        and supported == published
        and coverage is not None
        and coverage >= 0.5
    )
    return {
        "gold_items": len(gold),
        "published_predictions": published,
        "accuracy": total_correct / len(gold) if gold else None,
        "macro_f1": macro_f1,
        "macro_f1_labels": list(active_labels),
        "per_label": per_label,
        "wrong_entity_predictions": wrong_entity,
        "exact_entity_precision": exact_entity_precision,
        "evidence_supported_predictions": supported,
        "evidence_support_rate": evidence_support_rate,
        "company_owned_predictions": company_owned,
        "coverage": coverage,
        "minimum_items_gate": minimum_items,
        "required_labels_present": required_labels_present,
        "qualification_passed": gate,
        "production_scale_gate_passed": bool(gate and len(gold) >= 600 and per_label["mixed"]["support"] > 0),
    }
