import { useEffect, useState } from "react";

import { api, type Api, type AssetOut, type AssetStatus } from "../api";
import { formatBytes, shortDate } from "../format";

interface Props {
  client?: Api;
}

type Filter = AssetStatus | "all";
const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "Everything" },
  { value: "indexed", label: "Searchable" },
  { value: "queued", label: "Waiting" },
  { value: "processing", label: "Being read" },
  { value: "failed", label: "Could not be read" },
];
const STATE: Record<AssetStatus, { text: string; tone: "good" | "bad" | "wait" }> = {
  indexed: { text: "Searchable", tone: "good" },
  queued: { text: "Waiting", tone: "wait" },
  processing: { text: "Being read", tone: "wait" },
  failed: { text: "Could not be read", tone: "bad" },
};

export function LibraryTab({ client = api }: Props) {
  const [assets, setAssets] = useState<AssetOut[] | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [asking, setAsking] = useState<number | null>(null);
  const [removing, setRemoving] = useState<number | null>(null);

  useEffect(() => {
    let current = true;
    client
      .assets(filter === "all" ? undefined : filter)
      .then((found) => {
        if (!current) return;
        setAssets(found);
        setError(null);
      })
      .catch((failure: unknown) => {
        if (current) setError(failure instanceof Error ? failure.message : "The library could not be loaded.");
      });
    return () => {
      current = false;
    };
  }, [client, filter, reload]);

  async function remove(asset: AssetOut): Promise<void> {
    setRemoving(asset.asset_id);
    try {
      await client.deleteAsset(asset.asset_id);
      setAssets((list) => list?.filter((a) => a.asset_id !== asset.asset_id) ?? null);
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "It could not be deleted.");
    } finally {
      setRemoving(null);
      setAsking(null);
    }
  }

  return (
    <section aria-labelledby="library-title">
      <h1 id="library-title" className="page-title">Library</h1>
      <div className="toolbar">
        <label>
          Show
          <select
            className="field"
            value={filter}
            onChange={(event) => {
              setAssets(null);
              setFilter(event.target.value as Filter);
            }}
          >
            {FILTERS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="link-button"
          onClick={() => {
            setReload((n) => n + 1);
          }}
        >
          Refresh
        </button>
      </div>

      {error !== null && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}

      {assets === null && error === null && (
        <div aria-hidden="true">
          {[0, 1, 2].map((n) => (
            <div className="skeleton" key={n}>
              <span />
              <span />
            </div>
          ))}
        </div>
      )}

      {assets !== null && assets.length === 0 && (
        <p className="notice">
          {filter === "all" ? "Nothing has been added yet. Use Add files to start." : "Nothing in this state."}
        </p>
      )}

      {assets !== null && assets.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">State</th>
                <th scope="col" className="num">Size</th>
                <th scope="col">Added</th>
                <th scope="col"><span className="visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {assets.map((asset) => {
                const state = STATE[asset.status];
                return (
                  <tr key={asset.asset_id}>
                    <td>
                      {asset.title}
                      {asset.error !== null && <span className="why">{asset.error}</span>}
                    </td>
                    <td className={`state ${state.tone}`}>{state.text}</td>
                    <td className="num">{formatBytes(asset.size)}</td>
                    <td>{shortDate(asset.created_at)}</td>
                    <td>
                      {asking === asset.asset_id ? (
                        <span className="confirm" role="group" aria-label={`Delete ${asset.title}?`}>
                          <button
                            type="button"
                            className="link-button danger"
                            disabled={removing === asset.asset_id}
                            onClick={() => {
                              void remove(asset);
                            }}
                          >
                            Delete for good
                          </button>
                          <button
                            type="button"
                            className="link-button"
                            onClick={() => {
                              setAsking(null);
                            }}
                          >
                            Keep it
                          </button>
                        </span>
                      ) : (
                        <button
                          type="button"
                          className="link-button danger"
                          aria-label={`Delete ${asset.title}`}
                          onClick={() => {
                            setAsking(asset.asset_id);
                          }}
                        >
                          Delete
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
