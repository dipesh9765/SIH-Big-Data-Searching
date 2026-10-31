import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent, SyntheticEvent } from "react";

import { api, type Api, type HitOut, type Ranking, type SearchOut } from "../api";
import { splitLastWord } from "../format";
import { Highlighted } from "./Highlighted";
import { MediaPlayer } from "./MediaPlayer";

interface Props {
  semantic: boolean;
  client?: Api;
  /** Milliseconds to wait after typing before asking for suggestions. */
  suggestDelay?: number;
}

const PAGE = 10;

function message(failure: unknown): string {
  return failure instanceof Error ? failure.message : "Something went wrong.";
}

export function SearchTab({ semantic, client = api, suggestDelay = 150 }: Props) {
  const [text, setText] = useState("");
  const [ranking, setRanking] = useState<Ranking>("keyword");
  const [hits, setHits] = useState<HitOut[]>([]);
  const [meta, setMeta] = useState<SearchOut | null>(null);
  const [asked, setAsked] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [active, setActive] = useState(-1);
  const listId = useId();
  const shown = splitLastWord(text).word.length >= 2 ? suggestions : [];
  const requestId = useRef(0);

  useEffect(() => {
    const { word } = splitLastWord(text);
    if (word.length < 2) return; // too short to suggest; the list is hidden (see `shown`)
    const timer = setTimeout(() => {
      client
        .suggest(text)
        .then((found) => {
          setSuggestions(found.filter((candidate) => candidate !== word.toLowerCase()));
          setActive(-1);
        })
        .catch(() => { setSuggestions([]); });
    }, suggestDelay);
    return () => { clearTimeout(timer); };
  }, [text, client, suggestDelay]);

  async function run(query: string, offset: number): Promise<void> {
    const mine = ++requestId.current;
    setBusy(true);
    setError(null);
    try {
      const result = await client.search(query, ranking, offset, PAGE);
      if (mine !== requestId.current) return; // a newer search replaced this one
      setMeta(result);
      setHits((previous) => (offset === 0 ? result.results : [...previous, ...result.results]));
      setAsked(query);
    } catch (failure) {
      if (mine === requestId.current) setError(message(failure));
    } finally {
      if (mine === requestId.current) setBusy(false);
    }
  }

  function submit(event: SyntheticEvent): void {
    event.preventDefault();
    const query = text.trim();
    if (!query) return;
    setSuggestions([]);
    void run(query, 0);
  }

  function choose(word: string): void {
    const { head } = splitLastWord(text);
    setText(`${head}${word} `);
    setSuggestions([]);
  }

  function keys(event: KeyboardEvent<HTMLInputElement>): void {
    if (shown.length === 0) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current) => (current + 1) % shown.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current) => (current <= 0 ? shown.length - 1 : current - 1));
    } else if (event.key === "Enter" && active >= 0) {
      event.preventDefault();
      choose(shown[active] ?? "");
    } else if (event.key === "Escape") {
      setSuggestions([]);
    }
  }

  const more = meta !== null && hits.length >= PAGE && hits.length % PAGE === 0;

  return (
    <section aria-label="Search">
      <form onSubmit={submit} role="search" className="searchbar">
        <div className="combo">
          <input
            type="search"
            aria-label="Search query"
            placeholder="Search your documents, scans and recordings"
            value={text}
            onChange={(event) => { setText(event.target.value); }}
            onKeyDown={keys}
            role="combobox"
            aria-expanded={shown.length > 0}
            aria-controls={listId}
            aria-autocomplete="list"
            autoComplete="off"
          />
          {shown.length > 0 && (
            <ul id={listId} role="listbox" className="suggestions">
              {shown.map((word, index) => (
                <li
                  key={word}
                  role="option"
                  aria-selected={index === active}
                  className={index === active ? "active" : undefined}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    choose(word);
                  }}
                >
                  {word}
                </li>
              ))}
            </ul>
          )}
        </div>
        {semantic && (
          <select
            aria-label="Ranking"
            value={ranking}
            onChange={(event) => { setRanking(event.target.value as Ranking); }}
          >
            <option value="keyword">Keywords</option>
            <option value="semantic">Meaning</option>
            <option value="hybrid">Keywords + meaning</option>
          </select>
        )}
        <button type="submit" disabled={busy || !text.trim()}>
          {busy ? "Searching…" : "Search"}
        </button>
      </form>

      <details className="help">
        <summary>Search syntax</summary>
        <ul>
          <li><code>"climate change"</code> an exact phrase</li>
          <li><code>solar OR wind</code>, <code>solar -coal</code>, <code>(a OR b) c</code></li>
          <li><code>report type:pdf</code>, <code>ext:docx</code>, <code>after:2025-01-01</code>, <code>lang:deva</code></li>
        </ul>
      </details>

      {error !== null && <p className="error" role="alert">{error}</p>}

      <div aria-live="polite" className="summary">
        {meta !== null && !busy && hits.length > 0 &&
          `${meta.total === null ? "Top" : String(meta.total)} result${meta.total === 1 ? "" : "s"} for “${asked}” · ${meta.took_ms} ms · ${meta.strategy}`}
        {meta?.approximate === true && " · phrase matches may be incomplete"}
      </div>

      {meta !== null && !busy && hits.length === 0 && error === null && (
        <p className="empty">
          No results for “{asked}”.
          {meta.did_you_mean !== null && (
            <>
              {" "}Did you mean{" "}
              <button
                type="button"
                className="link"
                onClick={() => {
                  setText(meta.did_you_mean ?? "");
                  void run(meta.did_you_mean ?? "", 0);
                }}
              >
                {meta.did_you_mean}
              </button>
              ?
            </>
          )}
        </p>
      )}

      <ol className="results">
        {hits.map((hit) => (
          <li key={`${hit.document.id}`} className="card">
            <h3>{hit.document.title}</h3>
            <p className="snippet" lang="und">
              <Highlighted text={hit.snippet} highlights={hit.highlights} />
            </p>
            {hit.media !== null && (
              <MediaPlayer
                assetId={hit.media.asset_id}
                mediaType={hit.document.media_type}
                startMs={hit.media.start_ms}
                client={client}
              />
            )}
            <p className="meta">
              {hit.document.source_name} · {hit.document.media_type} · score {hit.score.toFixed(3)}
            </p>
          </li>
        ))}
      </ol>

      {more && (
        <button type="button" disabled={busy} onClick={() => void run(asked, hits.length)}>
          Load more
        </button>
      )}
    </section>
  );
}
