import { useRef, useState } from "react";

import { api, type Api } from "../api";
import { formatClock } from "../format";

interface Props {
  assetId: number;
  mediaType: string;
  startMs: number | null;
  client?: Api;
}

/** A play button that loads the recording and starts at the moment the hit was spoken. */
export function MediaPlayer({ assetId, mediaType, startMs, client = api }: Props) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const element = useRef<HTMLMediaElement | null>(null);
  const isVideo = mediaType.startsWith("video/");
  const seconds = (startMs ?? 0) / 1000;

  async function open(): Promise<void> {
    setError(null);
    try {
      setUrl(await client.fileUrl(assetId));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not load the recording.");
    }
  }

  function seek(): void {
    const media = element.current;
    if (!media) return;
    media.currentTime = seconds;
    void media.play().catch(() => undefined); // autoplay can be refused; the controls still work
  }

  if (url === null) {
    return (
      <div className="media">
        <button type="button" className="play" onClick={() => void open()}>
          ▶ Play from {formatClock(startMs ?? 0)}
        </button>
        {error !== null && <span className="error" role="alert">{error}</span>}
      </div>
    );
  }
  const common = {
    controls: true,
    src: url,
    onLoadedMetadata: seek,
    preload: "metadata" as const,
  };
  return (
    <div className="media">
      {isVideo ? (
        <video ref={element as React.RefObject<HTMLVideoElement>} {...common} />
      ) : (
        <audio ref={element} {...common} />
      )}
    </div>
  );
}
