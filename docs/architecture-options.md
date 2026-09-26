# Component decisions

## Selected POC stack

| Need | Component | Decision |
|---|---|---|
| Crawl scheduling | Scrapy (BSD-3) | Primary nationwide scheduler: queues, retries, throttling, dedupe, and pipelines |
| JavaScript fallback | Playwright via scrapy-playwright (Apache-2) | Invoke only when static extraction detects an incomplete JavaScript shell |
| Structured metadata | extruct (BSD-3) | Parse JSON-LD, microdata, RDFa, and OpenGraph before heuristics |
| Main text | Trafilatura (Apache-2) | Boilerplate-reduced Norwegian/English text extraction |
| Schema validation | Pydantic (MIT) | Typed claims, explicit nullable fields, and reject/abstain behavior |
| Difficult PDFs | Docling (MIT; model licences checked separately) | Only after plain PDF extraction fails on layout or tables |
| Entity matching | Exact organisation number, then RapidFuzz (MIT) | Organisation number is authoritative; fuzzy matching only queues unresolved candidates |
| Storage | PostgreSQL + object storage; DuckDB/Parquet for evaluation | Raw snapshots and facts remain separate; a vector database is not the system of record |

The Scrapy orchestrator is implemented with robots enforcement, one retry, AutoThrottle, per-domain
concurrency, a 10-second attempt timeout, disk-backed resume state, immutable page events, and the same
exact-entity/social publication gate. Tightening the earlier 15-second/two-retry policy on the same
204-site POC corpus preserved 154 available sites while reducing elapsed time from 388.1 to 117.5 seconds.

The independent scale run froze 1,000 unique registry website hosts with zero organisation or host overlap
against the POC and three prior website audits. It produced terminal outcomes for all 1,000 seeds (696
available, 304 explicit source errors) from 4,251 requests/68.3 MB in 600.7 seconds. Response-download
p50/p95 was 0.82/3.16 seconds and peak RSS was 434 MB. An identical resume made zero requests and reproduced
byte-identical output. This qualifies scheduler operational capacity only; identity accuracy remains tied
to the separate frozen audit, and deployed-service reliability/security/cost remain unqualified.

## Rejected as the foundation

- LLM-first and browser-agent crawlers: too costly and nondeterministic for bulk evidence collection.
- Firecrawl as the core: capable, but its AGPL core/managed extras add unnecessary licensing and stack
  constraints for this POC. It can be a separately measured managed fallback.
- Crawl4AI as the scheduler: useful in an extraction bake-off, but younger and less deterministic than
  Scrapy for queues, retries, and provenance.
- Public LinkedIn, Meta, Indeed, or Glassdoor scrapers: source terms and reproducibility make them an
  unsuitable foundation. Record outbound profile URLs from company sites; use registry or licensed
  sources for employee counts.
- Generic sentiment on company marketing pages: structurally biased. No sentiment score ships before a
  labelled Norwegian news/social evaluation.

## Bake-off contract

Freeze 100 companies, 500 pages, and 30 PDFs across size, legal form, industry, and site technology.
Compare:

1. Scrapy + extruct + Trafilatura.
2. The same stack with a Playwright fallback.
3. Crawl4AI on the identical corpus.
4. A managed Firecrawl run only if its licence/cost is acceptable.
5. An LLM extractor only on deterministic-baseline failures.

Report correct-domain rate, wrong-domain rate, static/JS success, pages per minute, p50/p95 latency,
field precision/recall, evidence-span validity, retry rate, RAM peak, and cost per company. A component
advances only when it improves its target without worsening wrong-company or unsupported-claim rates.
