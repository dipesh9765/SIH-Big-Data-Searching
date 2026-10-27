# ADR 0003: One analyzer for documents and queries

**Status:** accepted

## Context
The prototype stored stop-word-filtered but **unstemmed** tokens (it computed the stemmed list and discarded it) and looked up the raw query string, so most queries could not match. It also rebuilt the all-languages stop-word list for every token.

## Decision
A single function, `analysis.tokenize`, defines the pipeline: Unicode word tokens → casefold → drop English stop words and tokens over 40 characters → Snowball English stem. It is used by `Index.add` and `Index.search` and returns character offsets so snippets can highlight the original words.

## Consequences
- "running", "runs" and "run" index and query as one term; tests pin this behaviour.
- Stemming conflates some unrelated words (a known trade-off of stemmers vs lemmatisers) and the stop list is English only.
- Stems are cached (`lru_cache`), which matters because vocabularies are Zipfian.
- Changing the analyzer invalidates existing indexes: it would need a version in `meta` and a re-index. `schema_version` is stored for that.
