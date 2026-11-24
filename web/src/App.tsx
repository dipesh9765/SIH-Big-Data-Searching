import { useEffect, useState } from "react";

import { api, ApiError, keyStore, type Api, type Ranking, type StatsOut } from "./api";
import { ImagesTab } from "./components/ImagesTab";
import { LibraryTab } from "./components/LibraryTab";
import { SearchTab } from "./components/SearchTab";
import { UploadTab } from "./components/UploadTab";
import { parsePlace, placeToSearch, samePlace, type Place, type Tab } from "./url";

interface Props {
  client?: Api;
}

export function App({ client = api }: Props) {
  const [place, setPlace] = useState<Place>(() => parsePlace(window.location.search));
  const [stats, setStats] = useState<StatsOut | null>(null);
  const [locked, setLocked] = useState(false);
  const [offline, setOffline] = useState<string | null>(null);
  const [key, setKey] = useState(keyStore.get());
  const [attempt, setAttempt] = useState(0); // bumped to load the status again, e.g. after unlocking

  useEffect(() => {
    let current = true;
    client
      .stats()
      .then((result) => {
        if (!current) return;
        setStats(result);
        setLocked(false);
        setOffline(null);
      })
      .catch((failure: unknown) => {
        if (!current) return;
        if (failure instanceof ApiError && failure.status === 401) setLocked(true);
        else setOffline(failure instanceof Error ? failure.message : "The service is not reachable.");
      });
    return () => {
      current = false;
    };
  }, [client, attempt]);

  // Back and Forward move between places the user has been.
  useEffect(() => {
    const onPop = (): void => {
      setPlace(parsePlace(window.location.search));
    };
    window.addEventListener("popstate", onPop);
    return () => {
      window.removeEventListener("popstate", onPop);
    };
  }, []);

  function go(next: Place): void {
    if (samePlace(next, place)) return;
    window.history.pushState(null, "", `${window.location.pathname}${placeToSearch(next)}`);
    setPlace(next);
  }

  const tabs: { id: Tab; label: string }[] = [
    { id: "search", label: "Search" },
    { id: "add", label: "Add files" },
    { id: "library", label: "Library" },
    ...(stats?.semantic ? [{ id: "pictures" as const, label: "Pictures" }] : []),
  ];
  const busy = (stats?.queue.queued ?? 0) + (stats?.queue.running ?? 0);

  return (
    <>
      <header className="topbar">
        <div className="shell">
          <span className="brand">docsearch</span>
          {!locked && (
            <nav aria-label="Sections" className="tabs">
              {tabs.map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  className="tab"
                  aria-current={place.tab === tab.id ? "page" : undefined}
                  onClick={() => {
                    go({ ...place, tab: tab.id });
                  }}
                >
                  {tab.label}
                </button>
              ))}
            </nav>
          )}
          {stats !== null && (
            <p className="index-size" aria-label="Index size">
              {stats.documents.toLocaleString()} {stats.documents === 1 ? "document" : "documents"} indexed
              {busy > 0 && `, ${String(busy)} ${busy === 1 ? "file" : "files"} being read`}
            </p>
          )}
        </div>
      </header>

      <main className="shell">
        {locked && (
          <form
            className="keyform"
            onSubmit={(event) => {
              event.preventDefault();
              keyStore.set(key);
              setAttempt((n) => n + 1);
            }}
          >
            <h1 className="page-title">This server needs a key</h1>
            <label>
              API key
              <input
                className="field"
                type="password"
                value={key}
                onChange={(event) => {
                  setKey(event.target.value);
                }}
                autoComplete="off"
              />
            </label>
            <button type="submit" className="button primary">
              Unlock
            </button>
          </form>
        )}
        {offline !== null && (
          <p className="notice error" role="alert">
            {offline}
          </p>
        )}
        {!locked && place.tab === "search" && (
          <SearchTab
            key={`${place.q}\u0000${place.ranking}`}
            q={place.q}
            ranking={place.ranking}
            semantic={stats?.semantic ?? false}
            onSearch={(q: string, ranking: Ranking) => {
              go({ tab: "search", q, ranking });
            }}
            client={client}
          />
        )}
        {!locked && place.tab === "add" && (
          <UploadTab accepted={stats?.accepted_extensions ?? []} client={client} />
        )}
        {!locked && place.tab === "library" && <LibraryTab client={client} />}
        {!locked && place.tab === "pictures" && <ImagesTab client={client} />}
      </main>
    </>
  );
}
