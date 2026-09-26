# External discovery and sentiment

External intelligence is now 55/100 in the revised competition rubric. Individual connectors remain
quarantined until their data-rights and held-out gates pass; planned or experimental output earns zero.

## Missing-website discovery

Use the [Brønnøysund entity registry](https://data.brreg.no/enhetsregisteret/api/dokumentasjon/en/index.html)
first. Its open data is available under [NLOD 2.0](https://data.norge.no/nlod/en/2.0). Search is only
for an entity whose registry website is absent, invalid, or dead.

Recommended production path: a separately contracted [Brave Search API](https://brave.com/search/api/)
connector, provided its [terms](https://api-dashboard.search.brave.com/terms-of-service) and the chosen
plan explicitly cover the required evidence retention. If permanent result storage is not permitted,
keep provider output transient and retain only independently fetched page evidence after verification.

Two adapters are available, and neither scrapes a search-results page. `apply_search_discovery.py` accepts
exported JSONL only for a plan with result-storage rights. `run_brave_discovery.py` is the default transient
path: it keeps search output in memory, immediately crawls an accepted candidate, and discards raw titles,
snippets, ranks, query text, and response bodies. Both reject directories and social networks. Publication
still requires independently fetched proof of the exact legal entity.

Do not use Google or Bing grounding output to build a stored crawl corpus. Google Custom Search JSON API
is [closed to new customers and scheduled to discontinue on 1 January 2027](https://developers.google.com/custom-search/v1/overview).
Serper and Tavily can be evaluated as declared alternatives only after their storage, source, and crawling
rights are accepted for this use.

Production discovery gate:

- at least 500 frozen organisations, including at least 200 verified site-exists positives, 150 hard
  parent/franchise/ambiguous negatives, and 100 no-site or unresolved cases;
- at least 300 published predictions;
- exact-domain precision at least 99.5%, recall at least 70%, and eligible-population coverage at least 35%;
- zero wrong-company publications; and
- URL, redirect chain, retrieval time, content hash, exact-entity proof, cost, and latency for every publication.

## Norwegian company sentiment

The preferred first comparator is
[`NOSIBLE/financial-sentiment-v1.2-base`](https://huggingface.co/NOSIBLE/financial-sentiment-v1.2-base)
(Apache-2.0). It is finance-oriented and multilingual, but its published Norwegian result is based on a
small translation-derived slice, so it is only a hypothesis until tested on exact-company Norwegian news.
`run_sentiment_model.py` pins the model revision, uses the required system prompt with thinking disabled,
constrains generation to one of the three allowed label tokens, and preserves the input evidence metadata.
The checked-in three-snippet output is a synthetic runtime smoke test, not evaluation evidence.

The native-Norwegian comparator is
[`ltg/norbert3-base_sentence-sentiment`](https://huggingface.co/ltg/norbert3-base_sentence-sentiment)
(CC BY 4.0). Its source domain is reviews rather than company events. Pin and inspect its remote model code
before use. The larger NorBERT variant may be added only if it materially improves the same frozen corpus.

[NoReC](https://github.com/ltgoslo/norec) and
[`ltg/norec_sentence`](https://huggingface.co/datasets/ltg/norec_sentence) are useful research references,
but the text is CC BY-NC 4.0 and is not the commercial POC corpus. No clean, native, human-labelled Norwegian
company-news corpus with clear commercial reuse rights was found.

POC sentiment gate:

- at least 300 recent exact-company snippets, independently labelled and adjudicated, with positive,
  neutral, and negative support; include mixed items when the product intends to publish that class;
- exact-entity errors: zero;
- evidence support: 100%, including source URL, retrieval time, content hash, and claim span;
- positive and negative precision at least 90%, macro-F1 at least 0.80, and coverage at least 50%; and
- company-owned marketing copy always rejected as independent sentiment.

For production claims, use at least 600 entity-event bundles with national/local, Bokmål/Nynorsk, sector,
name-collision, negation, allegation, and stale-item stress cases. A company-level rollup requires at least
two independent publishers; otherwise expose only dated item-level context or abstain.
