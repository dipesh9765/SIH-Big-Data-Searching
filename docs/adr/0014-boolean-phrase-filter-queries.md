# 14. Query language: exact set evaluation, phrases by verification, filters in SQL

Status: accepted (scale-out work, `feature/scale-v2`)

## Decision
- **Syntax** (`query.py`, total parser, ADR in its docstring): free words (implicit AND), `"exact phrases"`, `AND` / `OR` / `NOT`, `-term`, `( groups )`, and filters `type:`, `ext:`, `lang:`, `before:`, `after:`.
- **Two paths.** A query of plain words keeps the fast top-k engine (`search.py`: scan or block-max pruning, honouring `mode=all|any`). Anything with an operator is evaluated **exactly** by `engine/boolean.py`: each term becomes a sorted array of matching ids with BM25 scores, AND/OR/NOT are numpy set operations (`intersect1d`, `union1d`, `isin`) and scores add. No pruning on this path, so it decodes every list it touches: exact and simple, slower for queries made of very common words.
- **Phrases without positions.** No word positions are stored. A phrase is the AND of its words, scored by BM25, then the best `phrase_candidates` (1,000) are checked against the document text with the same analyzer (`text_contains_phrase`: the analysed words occur consecutively). Stop words are not tokens, so "x the y" and "x y" are the same phrase. If there were more candidates than the cap the result is flagged `approximate` (a true phrase match ranked below the cap is missed). Storing positions would make this exact and cheaper per query but costs roughly an order of magnitude more index space; it is the right change if phrase search becomes central.
- **NOT** removes documents containing the term from the AND it sits in. A query with no positive clause matches nothing (the engine does not enumerate "everything except"); a NOT directly under an OR is ignored.
- **Filters** apply at the top level of an AND and are evaluated in SQL on the candidate ids (in chunks of 20,000): `type:` matches the media type by substring (`pdf`, `audio`, `image`), `ext:` the file extension, `lang:` the stored language/script tag (`en`, `hi`, ... for recordings; `deva`, `latn`, `mixed` for text, a script tag not a language), `before:`/`after:` the indexing date (ISO). LIKE wildcards in values are escaped. A malformed date is a 422. Filters alone select nothing: a term is required.

## Verified
`tests/test_boolean.py`: 17 operator cases against hand-computed answers; 200 random trees of AND/OR/NOT over a random corpus against Python set algebra; the same answers from PostgreSQL with data uncompacted and compacted; pagination; deleted documents excluded; filters; snippets highlight positive terms only; the phrase cap flag.

## Limits
- Operator queries on very common words decode whole lists (the exact path has no pruning).
- Phrase search is verification-based (see above); not a positional index.
- No wildcards, proximity (`NEAR`), field-restricted search or fuzzy terms inside the query language (fuzzy lookup exists separately in `engine/terms.py`).
