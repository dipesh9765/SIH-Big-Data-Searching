import { useEffect, useState } from "react";

import { api, ApiError, keyStore, type Api, type StatsOut } from "./api";
import { ImagesTab } from "./components/ImagesTab";
import { LibraryTab } from "./components/LibraryTab";
import { SearchTab } from "./components/SearchTab";
import { UploadTab } from "./components/UploadTab";

type Tab = "search" | "upload" | "library" | "images";

interface Props {
  client?: Api;
}

export function App({ client = api }: Props) {
  const [tab, setTab] = useState<Tab>("search");
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

  const tabs: [Tab, string][] = [
    ["search", "Search"],
    ["upload", "Upload"],
    ["library", "Library"],
    ...(stats?.semantic ? ([["images", "Images"]] as [Tab, string][]) : []),
  ];
  const waiting = (stats?.queue.queued ?? 0) + (stats?.queue.running ?? 0);

  return (
    <div className="app">
      <header>
        <h1>docsearch</h1>
        {stats !== null && (
          <p className="meta" aria-label="Index status">
            {stats.documents.toLocaleString()} documents · {stats.terms.toLocaleString()} terms
            {waiting > 0 && ` · ${String(waiting)} job${waiting === 1 ? "" : "s"} in progress`}
          </p>
        )}
      </header>

      {locked && (
        <form
          className="keybar"
          onSubmit={(event) => {
            event.preventDefault();
            keyStore.set(key);
            setAttempt((n) => n + 1);
          }}
        >
          <label>
            This server needs an API key
            <input
              type="password"
              value={key}
              onChange={(event) => { setKey(event.target.value); }}
              autoComplete="off"
            />
          </label>
          <button type="submit">Unlock</button>
        </form>
      )}
      {offline !== null && <p className="error" role="alert">{offline}</p>}

      {!locked && (
        <>
          <nav aria-label="Sections">
            {tabs.map(([id, label]) => (
              <button
                key={id}
                type="button"
                aria-current={tab === id ? "page" : undefined}
                className={tab === id ? "tab current" : "tab"}
                onClick={() => { setTab(id); }}
              >
                {label}
              </button>
            ))}
          </nav>
          <main>
            {tab === "search" && <SearchTab semantic={stats?.semantic ?? false} client={client} />}
            {tab === "upload" && (
              <UploadTab accepted={stats?.accepted_extensions ?? []} client={client} />
            )}
            {tab === "library" && <LibraryTab client={client} />}
            {tab === "images" && <ImagesTab client={client} />}
          </main>
        </>
      )}
    </div>
  );
}
