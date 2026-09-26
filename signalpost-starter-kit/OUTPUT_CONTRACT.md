# Minimal output contract

Emit one JSON object per input organisation number.

```json
{
  "organisation_number": "123456789",
  "run": {
    "run_id": "2026-08-24-a",
    "started_at": "2026-08-24T06:00:00Z",
    "completed_at": "2026-08-24T06:00:08Z",
    "terminal_status": "completed"
  },
  "claims": [
    {
      "field": "official_website",
      "value": "https://example.no/",
      "availability": "available",
      "confidence": 0.99,
      "evidence_ids": ["ev-1"]
    }
  ],
  "evidence": [
    {
      "id": "ev-1",
      "source_url": "https://example.no/",
      "source_class": "company_owned",
      "retrieved_at": "2026-08-24T06:00:04Z",
      "content_sha256": "...",
      "claim_span": "Example AS, organisation number 123 456 789"
    }
  ],
  "changes": [],
  "errors": [],
  "operations": {
    "requests": 4,
    "runtime_ms": 8120,
    "third_party_cost_usd": 0
  }
}
```

Allowed availability states are `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous` and `failed`. A checked source that has zero jobs or zero locations is different from a source that was not checked.
