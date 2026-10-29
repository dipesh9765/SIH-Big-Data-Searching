# 7. Typo tolerance and autocomplete over the term dictionary

Status: accepted (scale-out work, `feature/scale-v2`)

## Context
Users mistype and want suggestions while typing. Both operate on the *term dictionary* (every distinct analysed term with its document frequency), not on documents.

## Decision
- **Autocomplete:** a sorted term list with binary search for the prefix range, ranked by document frequency. In Postgres this is a `text_pattern_ops` btree range scan on `terms.term`.
- **Typo tolerance:** candidates come from deletion neighbourhoods (SymSpell), then every candidate is verified with a true Damerau-Levenshtein (optimal string alignment) distance. The typo budget depends on word length: 0 edits up to 3 letters, 1 up to 7, 2 beyond, because one edit turns a short word into a different word.
- `engine/terms.py` is the in-memory reference. The Postgres storage layer is expected to generate candidates with `pg_trgm` and reuse `edit_distance` to verify and rank.

## Verified
`fuzzy()` is compared with a brute-force scan of the whole dictionary on 300 random queries over 400 random terms: identical candidate sets. `edit_distance` is compared with an independent full-matrix implementation on 2,000 random string pairs at four limits.

## Consequences
- The deletion index stores every dictionary term's deletes up to 2 characters, so memory grows quickly with vocabulary size and word length (about 56 entries for a 10-letter word). It is fine as a reference and for vocabularies in the hundreds of thousands; for tens of millions of terms use a trigram index or an FST/automaton (what Lucene does).
- Distance is measured in code points. For Devanagari a missing vowel sign counts as one edit; that is reasonable, but stemmed terms (see the analyzer) are what is indexed, so suggestions are stems, not surface words. Showing readable suggestions needs a surface-form table (not built).
