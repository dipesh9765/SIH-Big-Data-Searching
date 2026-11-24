import { useRef, useState } from "react";

import { api, type Api } from "../api";
import { formatClock } from "../format";

interface Props {
  assetId: number;
  mediaType: string;
  startMs: number | null;
  client?: Api;
}

/** Loads the recording on request and starts it at the moment the hit was spoken. */
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
      setError(failure instanceof Error ? failure.message : "The recording could not be loaded.");
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
      <>
        <button
          type="button"
          className="button"
          onClick={() => {
            void open();
          }}
        >
          Play from {formatClock(startMs ?? 0)}
        </button>
        {error !== null && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
      </>
    );
  }
  const common = { controls: true, src: url, onLoadedMetadata: seek, preload: "metadata" as const };
  return isVideo ? (
    <video ref={element as React.RefObject<HTMLVideoElement>} {...common} />
  ) : (
    <audio ref={element} {...common} />
  );
}
