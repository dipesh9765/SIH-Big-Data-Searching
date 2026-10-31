import { useEffect, useState } from "react";

import { api, type Api, type AssetOut, type AssetStatus } from "../api";
import { formatBytes } from "../format";

interface Props {
  client?: Api;
  /** Ask before deleting; replaced in tests. */
  confirm?: (message: string) => boolean;
}

const FILTERS: (AssetStatus | "all")[] = ["all", "indexed", "queued", "processing", "failed"];

export function LibraryTab({ client = api, confirm = (m) => window.confirm(m) }: Props) {
  const [assets, setAssets] = useState<AssetOut[]>([]);
  const [filter, setFilter] = useState<AssetStatus | "all">("all");
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const [reload, setReload] = useState(0); // bumped by the Refresh button

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
        if (current) setError(failure instanceof Error ? failure.message : "Could not load the library.");
      })
      .finally(() => {
        if (current) setLoaded(true);
      });
    return () => {
      current = false;
    };
  }, [client, filter, reload]);

  async function remove(asset: AssetOut): Promise<void> {
    if (!confirm(`Delete “${asset.title}”? It disappears from search immediately.`)) return;
    try {
      await client.deleteAsset(asset.asset_id);
      setAssets((current) => current.filter((a) => a.asset_id !== asset.asset_id));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not delete.");
    }
  }

  return (
    <section aria-label="Library">
      <div className="toolbar">
        <label>
          Show{" "}
          <select
            value={filter}
            onChange={(event) => { setFilter(event.target.value as AssetStatus | "all"); }}
          >
            {FILTERS.map((f) => (
              <option key={f} value={f}>{f}</option>
            ))}
          </select>
        </label>
        <button type="button" onClick={() => { setReload((n) => n + 1); }}>Refresh</button>
      </div>
      {error !== null && <p className="error" role="alert">{error}</p>}
      {loaded && assets.length === 0 && error === null && <p className="empty">Nothing here yet.</p>}
      <table className="library">
        <thead>
          <tr><th>Title</th><th>Status</th><th>Size</th><th>Added</th><th /></tr>
        </thead>
        <tbody>
          {assets.map((asset) => (
            <tr key={asset.asset_id}>
              <td>
                {asset.title}
                {asset.error !== null && <div className="error">{asset.error}</div>}
              </td>
              <td><span className={`badge ${asset.status}`}>{asset.status}</span></td>
              <td>{formatBytes(asset.size)}</td>
              <td>{asset.created_at.slice(0, 10)}</td>
              <td>
                <button
                  type="button"
                  className="danger"
                  aria-label={`Delete ${asset.title}`}
                  onClick={() => void remove(asset)}
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
