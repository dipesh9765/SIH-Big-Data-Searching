import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeEvent, DragEvent } from "react";

import { api, type Api, type AssetOut } from "../api";
import { formatBytes, formatClock } from "../format";

type Stage = "uploading" | "refused" | "queued" | "processing" | "indexed" | "failed";

interface Item {
  key: number;
  name: string;
  size: number;
  stage: Stage;
  assetId: number | null;
  detail: string;
}

interface Props {
  accepted: string[];
  client?: Api;
  /** How often to ask where each file is, in milliseconds. */
  pollInterval?: number;
}

const DONE: Stage[] = ["refused", "indexed", "failed"];

const WORDS: Record<Stage, { text: string; tone: "good" | "bad" | "wait" }> = {
  uploading: { text: "Uploading", tone: "wait" },
  queued: { text: "Waiting for a worker", tone: "wait" },
  processing: { text: "Reading the file", tone: "wait" },
  indexed: { text: "Searchable", tone: "good" },
  failed: { text: "Could not read it", tone: "bad" },
  refused: { text: "Not accepted", tone: "bad" },
};

function describe(asset: AssetOut): string {
  const parts: string[] = [];
  if (asset.units > 1) parts.push(`${String(asset.units)} passages`);
  if (asset.duration_ms !== null) parts.push(`${formatClock(asset.duration_ms)} long`);
  if (asset.language) parts.push(`language ${asset.language}`);
  if (asset.ocr_confidence !== null) parts.push(`scan read at ${asset.ocr_confidence.toFixed(0)}% confidence`);
  return parts.join(", ");
}

export function UploadTab({ accepted, client = api, pollInterval = 1500 }: Props) {
  const [items, setItems] = useState<Item[]>([]);
  const [over, setOver] = useState(false);
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
          detail: result.duplicate && result.job_id === null ? "These exact bytes were added before." : "",
        });
      } catch (failure) {
        patch(key, { stage: "refused", detail: failure instanceof Error ? failure.message : "Upload failed." });
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
    }));
    setItems((current) => [...fresh, ...current]);
    fresh.forEach((item, index) => {
      const file = list[index];
      if (file) void send(file, item.key);
    });
  }

  useEffect(() => {
    const waiting = items.filter((item) => !DONE.includes(item.stage) && item.assetId !== null);
    if (waiting.length === 0) return;
    const timer = setInterval(() => {
      for (const item of waiting) {
        if (item.assetId === null) continue;
        client
          .asset(item.assetId)
          .then((asset) => {
            patch(item.key, {
              stage: asset.status,
              detail: asset.status === "failed" ? (asset.error ?? "") : describe(asset),
            });
          })
          .catch(() => undefined); // a poll that fails is simply tried again
      }
    }, pollInterval);
    return () => {
      clearInterval(timer);
    };
  }, [items, client, patch, pollInterval]);

  function drop(event: DragEvent): void {
    event.preventDefault();
    setOver(false);
    add(event.dataTransfer.files);
  }

  function pick(event: ChangeEvent<HTMLInputElement>): void {
    if (event.target.files) add(event.target.files);
    event.target.value = "";
  }

  return (
    <section aria-labelledby="add-title">
      <h1 id="add-title" className="page-title">Add files</h1>
      <p className="page-lede">
        Documents, scans, pictures, audio and video. Each file is read in the background; you can search it
        as soon as it says searchable.
      </p>
      <div
        className={over ? "dropzone over" : "dropzone"}
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => {
          setOver(false);
        }}
        onDrop={drop}
      >
        <p>Drop files here, or choose them from your computer.</p>
        <button
          type="button"
          className="button primary"
          onClick={() => input.current?.click()}
        >
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
        {accepted.length > 0 && <p className="index-size">Accepted: {accepted.join(" ")}</p>}
      </div>

      {items.length === 0 ? (
        <p className="notice">Nothing added in this visit. Files you add appear here with their progress.</p>
      ) : (
        <ul className="queue" aria-label="Files added">
          {items.map((item) => {
            const word = WORDS[item.stage];
            return (
              <li key={item.key}>
                <span className="name">
                  {item.name}
                  <span className="size">{formatBytes(item.size)}</span>
                </span>
                <span className={`state ${word.tone}`}>{word.text}</span>
                <span className="detail">{item.detail}</span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
