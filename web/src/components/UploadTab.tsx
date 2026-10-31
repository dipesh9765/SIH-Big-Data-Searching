import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeEvent, DragEvent } from "react";

import { api, ApiError, type Api, type AssetOut } from "../api";
import { formatBytes, formatClock } from "../format";

type Stage = "uploading" | "refused" | "queued" | "processing" | "indexed" | "failed";

interface Item {
  key: number;
  name: string;
  size: number;
  stage: Stage;
  assetId: number | null;
  detail: string;
  asset: AssetOut | null;
}

interface Props {
  accepted: string[];
  client?: Api;
  /** How often to ask where each upload is, in milliseconds. */
  pollInterval?: number;
}

const TERMINAL: Stage[] = ["refused", "indexed", "failed"];

function describe(asset: AssetOut): string {
  const parts: string[] = [];
  if (asset.units > 1) parts.push(`${String(asset.units)} segments`);
  if (asset.duration_ms !== null) parts.push(formatClock(asset.duration_ms));
  if (asset.language) parts.push(asset.language);
  if (asset.ocr_confidence !== null) parts.push(`OCR confidence ${asset.ocr_confidence.toFixed(0)}%`);
  return parts.join(" · ");
}

export function UploadTab({ accepted, client = api, pollInterval = 1500 }: Props) {
  const [items, setItems] = useState<Item[]>([]);
  const [hover, setHover] = useState(false);
  const counter = useRef(0);
  const input = useRef<HTMLInputElement | null>(null);

  const patch = useCallback((key: number, change: Partial<Item>) => {
    setItems((current) => current.map((item) => (item.key === key ? { ...item, ...change } : item)));
  }, []);

  const send = useCallback(
    async (file: File, key: number) => {
      try {
        const result = await client.upload(file);
        patch(key, {
          stage: result.status,
          assetId: result.asset_id,
          detail: result.duplicate && result.job_id === null ? "already uploaded" : "",
        });
      } catch (failure) {
        patch(key, {
          stage: "refused",
          detail: failure instanceof ApiError || failure instanceof Error ? failure.message : "failed",
        });
      }
    },
    [client, patch],
  );

  function add(files: FileList | File[]): void {
    const list = Array.from(files);
    const fresh = list.map<Item>((file) => ({
      key: ++counter.current,
      name: file.name,
      size: file.size,
      stage: "uploading",
      assetId: null,
      detail: "",
      asset: null,
    }));
    setItems((current) => [...fresh, ...current]);
    fresh.forEach((item, index) => {
      const file = list[index];
      if (file) void send(file, item.key);
    });
  }

  useEffect(() => {
    const waiting = items.filter((i) => !TERMINAL.includes(i.stage) && i.assetId !== null);
    if (waiting.length === 0) return;
    const timer = setInterval(() => {
      for (const item of waiting) {
        if (item.assetId === null) continue;
        client
          .asset(item.assetId)
          .then((asset) => { patch(item.key, {
              stage: asset.status,
              asset,
              detail: asset.status === "failed" ? (asset.error ?? "failed") : describe(asset),
            }); },
          )
          .catch(() => undefined); // a poll that fails is simply tried again
      }
    }, pollInterval);
    return () => { clearInterval(timer); };
  }, [items, client, patch, pollInterval]);

  function drop(event: DragEvent): void {
    event.preventDefault();
    setHover(false);
    add(event.dataTransfer.files);
  }

  function pick(event: ChangeEvent<HTMLInputElement>): void {
    if (event.target.files) add(event.target.files);
    event.target.value = "";
  }

  return (
    <section aria-label="Upload">
      <div
        className={hover ? "drop over" : "drop"}
        onDragOver={(event) => {
          event.preventDefault();
          setHover(true);
        }}
        onDragLeave={() => { setHover(false); }}
        onDrop={drop}
      >
        <p>Drop files here, or</p>
        <button type="button" onClick={() => input.current?.click()}>
          Choose files
        </button>
        <input
          ref={input}
          type="file"
          multiple
          hidden
          aria-label="Choose files to upload"
          accept={accepted.join(",")}
          onChange={pick}
        />
        {accepted.length > 0 && <p className="meta">Accepted: {accepted.join(" ")}</p>}
      </div>

      <ul className="uploads">
        {items.map((item) => (
          <li key={item.key} className={`upload ${item.stage}`}>
            <span className="name">{item.name}</span>
            <span className="meta">{formatBytes(item.size)}</span>
            <span className={`badge ${item.stage}`}>{item.stage}</span>
            {item.detail && <span className="detail">{item.detail}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}
