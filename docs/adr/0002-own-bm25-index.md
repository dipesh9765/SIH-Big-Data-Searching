# ADR 0002: Implement the inverted index and BM25 directly

**Status:** accepted

## Context
SQLite ships FTS5, which provides an inverted index, `bm25()` ranking and snippets in a few lines. Using it would be less code and faster.

## Decision
Implement the inverted index, term statistics and BM25 scoring in this codebase on plain tables, and verify them against a brute-force reference scorer.

## Rationale
The point of the project is to show how a search engine works: the analysis chain, postings, corpus statistics, ranking and the transaction discipline that keeps them consistent. Calling FTS5 would demonstrate none of that. Owning the scoring also makes the behaviour inspectable and testable (hand-computed scores in the test suite) and lets the analyzer (Snowball stemming, shared between documents and queries) be controlled exactly.

## Consequences
- More code to maintain, and query cost grows with postings length because every posting of a query term is scored (benchmark: "common terms (OR)"). Production engines avoid this with skip lists and WAND-style pruning.
- No phrase or proximity queries (would need positions in postings).
- **If this were a product, use FTS5 or a real engine.** The README says so, and the storage layer is narrow (`Index`), so swapping is a contained change.
