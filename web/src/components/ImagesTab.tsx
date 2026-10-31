import { useEffect, useState } from "react";
import type { SyntheticEvent } from "react";

import { api, type Api, type ImageHitOut } from "../api";

interface Props {
  client?: Api;
}

function Picture({ hit, client }: { hit: ImageHitOut; client: Api }) {
  const [src, setSrc] = useState<string | null>(null);

  // A signed, short-lived URL, so an <img> can load a file that sits behind the API key.
  useEffect(() => {
    let current = true;
    client
      .fileUrl(hit.asset_id)
      .then((url) => {
        if (current) setSrc(url);
      })
      .catch(() => undefined);
    return () => {
      current = false;
    };
  }, [client, hit.asset_id]);

  return (
    <figure className="picture">
      {src !== null ? <img src={src} alt={hit.title} loading="lazy" /> : <div className="placeholder" />}
      <figcaption>
        {hit.title}
        <span className="meta"> · {(hit.similarity * 100).toFixed(0)}% match</span>
      </figcaption>
    </figure>
  );
}

export function ImagesTab({ client = api }: Props) {
  const [text, setText] = useState("");
  const [hits, setHits] = useState<ImageHitOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: SyntheticEvent): Promise<void> {
    event.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setHits(await client.searchImages(text.trim()));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Search failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Image search">
      <form onSubmit={(event) => void submit(event)} role="search" className="searchbar">
        <input
          type="search"
          aria-label="Describe the picture"
          placeholder="Describe a picture, in English: a red bus, snow on a mountain"
          value={text}
          onChange={(event) => { setText(event.target.value); }}
        />
        <button type="submit" disabled={busy || !text.trim()}>Find</button>
      </form>
      {error !== null && <p className="error" role="alert">{error}</p>}
      {hits !== null && hits.length === 0 && <p className="empty">No pictures indexed yet.</p>}
      <div className="grid">
        {hits?.map((hit) => <Picture key={hit.asset_id} hit={hit} client={client} />)}
      </div>
    </section>
  );
}
