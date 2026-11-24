import { useEffect, useState } from "react";
import type { SyntheticEvent } from "react";

import { api, type Api, type ImageHitOut } from "../api";

interface Props {
  client?: Api;
}

function Picture({ hit, client }: { hit: ImageHitOut; client: Api }) {
  const [src, setSrc] = useState<string | null>(null);

  // A signed, short-lived address, so an <img> can load a file that sits behind the API key.
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
      {src !== null ? <img src={src} alt={hit.title} loading="lazy" /> : <div className="blank" />}
      <figcaption>
        {hit.title}
        <span className="match">{(hit.similarity * 100).toFixed(0)}% match</span>
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
      setError(failure instanceof Error ? failure.message : "The search failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="pictures-title">
      <h1 id="pictures-title" className="page-title">Pictures</h1>
      <p className="page-lede">Describe what is in the picture, in English.</p>
      <form
        onSubmit={(event) => {
          void submit(event);
        }}
        role="search"
        className="query"
      >
        <input
          className="field large"
          type="search"
          aria-label="Describe the picture"
          placeholder="A red bus, snow on a mountain"
          value={text}
          onChange={(event) => {
            setText(event.target.value);
          }}
        />
        <button type="submit" className="button primary" disabled={busy || !text.trim()}>
          Find
        </button>
      </form>
      {error !== null && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {hits !== null && hits.length === 0 && (
        <p className="notice">No pictures have been indexed yet. Add some under Add files.</p>
      )}
      {hits !== null && hits.length > 0 && (
        <div className="pictures">
          {hits.map((hit) => (
            <Picture key={hit.asset_id} hit={hit} client={client} />
          ))}
        </div>
      )}
    </section>
  );
}
