import { useEffect, useId, useState } from "react";
import type { KeyboardEvent, SyntheticEvent } from "react";

import { api, type Api, type HitOut, type Ranking, type SearchOut } from "../api";
import { fileKind, formatClock, splitLastWord } from "../format";
import { Highlighted } from "./Highlighted";
import { MediaPlayer } from "./MediaPlayer";

interface Props {
  q: string;
  ranking: Ranking;
  semantic: boolean;
  /** Called when the user asks for a search: the app puts it in the address bar. */
  onSearch: (q: string, ranking: Ranking) => void;
  client?: Api;
  /** Milliseconds to wait after typing before asking for suggestions. */
  suggestDelay?: number;
}

const PAGE = 10;
const EXAMPLES: { text: string; note: string }[] = [
  { text: '"exact phrase"', note: "words in this order" },
  { text: "solar OR wind", note: "either word" },
  { text: "report -draft", note: "leave a word out" },
  { text: "budget type:pdf after:2025-01-01", note: "narrow by kind and date" },
];

interface Loaded {
  q: string;
  ranking: Ranking;
  pages: number;
  hits: HitOut[];
  meta: SearchOut;
}
interface Failed {
  q: string;
  ranking: Ranking;
  pages: number;
  message: string;
}

function messageOf(failure: unknown): string {
  return failure instanceof Error ? failure.message : "Something went wrong.";
}

/** Mount with `key={q + ranking}`: the address bar is the source of truth, a new search is a new screen. */
export function SearchTab({ q, ranking, semantic, onSearch, client = api, suggestDelay = 150 }: Props) {
  const [text, setText] = useState(q);
  const [mode, setMode] = useState<Ranking>(ranking);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [active, setActive] = useState(-1);
  const [typing, setTyping] = useState(false); // suggestions only follow the person's own typing, not a loaded URL
  const [data, setData] = useState<Loaded | null>(null);
  const [failure, setFailure] = useState<Failed | null>(null);
  const [pageFor, setPageFor] = useState({ key: "", pages: 1 });
  const [retry, setRetry] = useState(0);
  const listId = useId();

  const key = `${q}\u0000${ranking}`;
  const pages = pageFor.key === key ? pageFor.pages : 1;

  useEffect(() => {
    if (!q) return;
    let current = true;
    client
      .search(q, ranking, (pages - 1) * PAGE, PAGE)
      .then((result) => {
        if (!current) return;
        setFailure(null);
        setData((previous) =>
          pages > 1 && previous?.q === q && previous.ranking === ranking
            ? { q, ranking, pages, hits: [...previous.hits, ...result.results], meta: result }
            : { q, ranking, pages, hits: result.results, meta: result },
        );
      })
      .catch((reason: unknown) => {
        if (current) setFailure({ q, ranking, pages, message: messageOf(reason) });
      });
    return () => {
      current = false;
    };
  }, [client, q, ranking, pages, retry]);

  useEffect(() => {
    const { word } = splitLastWord(text);
    if (!typing || word.length < 2) return;
    const timer = setTimeout(() => {
      client
        .suggest(text)
        .then((found) => {
          setSuggestions(found.filter((candidate) => candidate !== word.toLowerCase()));
          setActive(-1);
        })
        .catch(() => {
          setSuggestions([]);
        });
    }, suggestDelay);
    return () => {
      clearTimeout(timer);
    };
  }, [text, typing, client, suggestDelay]);

  const shown = splitLastWord(text).word.length >= 2 ? suggestions : [];
  const current = data?.q === q && data.ranking === ranking && data.pages === pages ? data : null;
  const failed = failure?.q === q && failure.ranking === ranking && failure.pages === pages ? failure : null;
  const loading = q !== "" && current === null && failed === null;
  const hits = data?.q === q && data.ranking === ranking ? data.hits : [];
  const meta = current?.meta ?? null;

  function submit(event: SyntheticEvent): void {
    event.preventDefault();
    const query = text.trim();
    if (!query) return;
    setSuggestions([]);
    setTyping(false);
    onSearch(query, semantic ? mode : "keyword");
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
      setActive((index) => (index + 1) % shown.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => (index <= 0 ? shown.length - 1 : index - 1));
    } else if (event.key === "Enter" && active >= 0) {
      event.preventDefault();
      choose(shown[active] ?? "");
    } else if (event.key === "Escape") {
      setSuggestions([]);
    }
  }

  const more = !loading && failed === null && current !== null && hits.length === pages * PAGE;

  return (
    <section aria-labelledby="search-title">
      <h1 id="search-title" className="visually-hidden">Search</h1>
      <form onSubmit={submit} role="search" className="query">
        <div className="combo">
          <input
            className="field large"
            type="search"
            aria-label="Search query"
            placeholder="Search everything you have added"
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setTyping(true);
            }}
            onKeyDown={keys}
            role="combobox"
            aria-expanded={shown.length > 0}
            aria-controls={listId}
            aria-autocomplete="list"
            autoComplete="off"
            spellCheck={false}
          />
          {shown.length > 0 && (
            <ul id={listId} role="listbox" className="suggestions">
              {shown.map((word, index) => (
                <li
                  key={word}
                  role="option"
                  aria-selected={index === active}
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
            className="field"
            aria-label="Ranking"
            value={mode}
            onChange={(event) => {
              setMode(event.target.value as Ranking);
            }}
          >
            <option value="keyword">By keywords</option>
            <option value="semantic">By meaning</option>
            <option value="hybrid">Keywords and meaning</option>
          </select>
        )}
        <button type="submit" className="button primary" disabled={!text.trim()}>
          Search
        </button>
      </form>

      {q === "" && (
        <div className="examples">
          <p>Ways to narrow a search</p>
          <ul>
            {EXAMPLES.map((example) => (
              <li key={example.text}>
                <button
                  type="button"
                  className="link-button"
                  onClick={() => {
                    setText(example.text);
                  }}
                >
                  {example.text}
                </button>{" "}
                <span className="index-size">{example.note}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="result-note" aria-live="polite">
        {loading && "Searching"}
        {meta !== null && hits.length > 0 && summary(meta, q)}
      </p>

      {loading && hits.length === 0 && (
        <div aria-hidden="true">
          {[0, 1, 2].map((n) => (
            <div className="skeleton" key={n}>
              <span />
              <span />
              <span />
            </div>
          ))}
        </div>
      )}

      {failed !== null && (
        <div className="notice error" role="alert">
          <p>{failed.message}</p>
          <button
            type="button"
            className="link-button"
            onClick={() => {
              setFailure(null);
              setRetry((n) => n + 1);
            }}
          >
            Try again
          </button>
        </div>
      )}

      {meta !== null && hits.length === 0 && failed === null && (
        <div className="notice">
          <p>
            Nothing matches <strong>{q}</strong>.
          </p>
          {meta.did_you_mean !== null && (
            <p>
              Did you mean{" "}
              <button
                type="button"
                className="link-button"
                onClick={() => {
                  onSearch(meta.did_you_mean ?? "", ranking);
                }}
              >
                {meta.did_you_mean}
              </button>
              ?
            </p>
          )}
        </div>
      )}

      {hits.length > 0 && (
        <ol className="entries">
          {hits.map((hit) => (
            <Entry key={hit.document.id} hit={hit} client={client} />
          ))}
        </ol>
      )}

      {more && (
        <p className="result-note">
          <button
            type="button"
            className="button"
            onClick={() => {
              setPageFor({ key, pages: pages + 1 });
            }}
          >
            Show more
          </button>
        </p>
      )}
    </section>
  );
}

function summary(meta: SearchOut, q: string): string {
  const count = meta.total === null ? "the best matches" : `${String(meta.total)} ${meta.total === 1 ? "match" : "matches"}`;
  const note = meta.approximate ? " Phrase matches may be incomplete." : "";
  return `Showing ${count} for “${q}”, found in ${String(Math.max(1, Math.round(meta.took_ms)))} ms.${note}`;
}

function Entry({ hit, client }: { hit: HitOut; client: Api }) {
  const moment = hit.media?.start_ms ?? null;
  return (
    <li className="entry">
      <div className={moment !== null ? "gutter moment" : "gutter"}>
        {moment !== null ? formatClock(moment) : fileKind(hit.document.source_name)}
      </div>
      <div>
        <h3>{hit.document.title}</h3>
        <p className="snippet" lang="und">
          <Highlighted text={hit.snippet} highlights={hit.highlights} />
        </p>
        {hit.media !== null && (
          <div className="player">
            <MediaPlayer
              assetId={hit.media.asset_id}
              mediaType={hit.document.media_type}
              startMs={hit.media.start_ms}
              client={client}
            />
          </div>
        )}
        <dl className="facts">
          <div>
            <dt>File</dt>
            <dd>{hit.document.source_name}</dd>
          </div>
          <div>
            <dt>Relevance</dt>
            <dd>{hit.score.toFixed(2)}</dd>
          </div>
        </dl>
      </div>
    </li>
  );
}
