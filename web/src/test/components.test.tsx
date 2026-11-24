import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import {
  ApiError,
  keyStore,
  request,
  type Api,
  type AssetOut,
  type HitOut,
  type Ranking,
  type SearchOut,
} from "../api";
import { LibraryTab } from "../components/LibraryTab";
import { MediaPlayer } from "../components/MediaPlayer";
import { SearchTab } from "../components/SearchTab";
import { UploadTab } from "../components/UploadTab";

function hit(overrides: Partial<HitOut> = {}): HitOut {
  return {
    document: {
      id: 1,
      title: "Glacier report",
      source_name: "glacier.txt",
      media_type: "text/plain",
      tokens: 40,
      created_at: "2026-10-30T10:00:00",
    },
    score: 3.21,
    snippet: "the glacier retreat is accelerating",
    highlights: [[4, 11]],
    media: null,
    ...overrides,
  };
}

function result(hits: HitOut[], extra: Partial<SearchOut> = {}): SearchOut {
  return {
    query: "glacier",
    mode: "all",
    total: hits.length,
    approximate: false,
    did_you_mean: null,
    strategy: "scan-all",
    took_ms: 4.2,
    results: hits,
    ...extra,
  };
}

function fakeApi(overrides: Partial<Api> = {}): Api {
  return {
    stats: vi.fn(),
    search: vi.fn().mockResolvedValue(result([])),
    suggest: vi.fn().mockResolvedValue([]),
    searchImages: vi.fn().mockResolvedValue([]),
    upload: vi.fn(),
    asset: vi.fn(),
    assets: vi.fn().mockResolvedValue([]),
    deleteAsset: vi.fn().mockResolvedValue(undefined),
    fileUrl: vi.fn().mockResolvedValue("/v1/assets/9/file?token=abc"),
    ...overrides,
  };
}

function asset(overrides: Partial<AssetOut> = {}): AssetOut {
  return {
    asset_id: 5,
    title: "scan.pdf",
    source_name: "scan.pdf",
    size: 2048,
    status: "indexed",
    document_id: 1,
    error: null,
    created_at: "2026-10-30T10:00:00",
    ocr_confidence: null,
    units: 1,
    duration_ms: null,
    language: null,
    ...overrides,
  };
}

/** Stands in for the app shell: the address bar decides what is searched. */
function Harness({ client, semantic = false, start = "" }: { client: Api; semantic?: boolean; start?: string }) {
  const [place, setPlace] = useState<{ q: string; ranking: Ranking }>({ q: start, ranking: "keyword" });
  return (
    <SearchTab
      key={`${place.q}\u0000${place.ranking}`}
      q={place.q}
      ranking={place.ranking}
      semantic={semantic}
      onSearch={(q, ranking) => {
        setPlace({ q, ranking });
      }}
      client={client}
      suggestDelay={0}
    />
  );
}

async function ask(text: string): Promise<void> {
  await userEvent.type(screen.getByLabelText("Search query"), text);
  await userEvent.keyboard("{Enter}");
}

describe("SearchTab", () => {
  it("shows ways to narrow a search before anything is asked", () => {
    render(<Harness client={fakeApi()} />);
    expect(screen.getByRole("button", { name: '"exact phrase"' })).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: /entries/i })).not.toBeInTheDocument();
  });

  it("puts an example into the box when it is clicked", async () => {
    render(<Harness client={fakeApi()} />);
    await userEvent.click(screen.getByRole("button", { name: "solar OR wind" }));
    expect(screen.getByLabelText("Search query")).toHaveValue("solar OR wind");
  });

  it("searches, highlights the match and says how many were found", async () => {
    const client = fakeApi({ search: vi.fn().mockResolvedValue(result([hit()])) });
    render(<Harness client={client} />);
    await ask("glacier");
    expect(await screen.findByRole("heading", { name: "Glacier report" })).toBeInTheDocument();
    expect(screen.getByText("glacier", { selector: "mark" })).toBeInTheDocument();
    expect(client.search).toHaveBeenCalledWith("glacier", "keyword", 0, 10);
    expect(screen.getByText(/Showing 1 match for “glacier”, found in 4 ms/)).toBeInTheDocument();
  });

  it("shows the file kind in the margin of a document and the moment for a recording", async () => {
    const media = { asset_id: 9, kind: "transcript", start_ms: 83_000, end_ms: 99_000, file_url: "/x" };
    const spoken = hit({
      media,
      document: { ...hit().document, id: 2, title: "Keynote", source_name: "keynote.mp3", media_type: "audio/mpeg" },
    });
    render(<Harness client={fakeApi({ search: vi.fn().mockResolvedValue(result([hit(), spoken])) })} />);
    await ask("glacier");
    await screen.findByRole("heading", { name: "Keynote" });
    const entries = screen.getAllByRole("listitem").filter((li) => li.className === "entry");
    expect(within(entries[0] as HTMLElement).getByText("txt")).toBeInTheDocument();
    expect(within(entries[1] as HTMLElement).getByText("1:23")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Play from 1:23" })).toBeInTheDocument();
  });

  it("draws a loading state while the answer is on its way", async () => {
    let finish: (value: SearchOut) => void = () => undefined;
    const search = vi.fn().mockReturnValue(new Promise<SearchOut>((resolve) => { finish = resolve; }));
    render(<Harness client={fakeApi({ search })} />);
    await ask("glacier");
    expect(await screen.findByText("Searching")).toBeInTheDocument();
    await act(async () => {
      finish(result([hit()]));
      await Promise.resolve();
    });
    expect(await screen.findByRole("heading", { name: "Glacier report" })).toBeInTheDocument();
    expect(screen.queryByText("Searching")).not.toBeInTheDocument();
  });

  it("says what to do when nothing matches, and offers a correction that runs on click", async () => {
    const search = vi
      .fn()
      .mockResolvedValueOnce(result([], { did_you_mean: "glacier" }))
      .mockResolvedValueOnce(result([hit()]));
    render(<Harness client={fakeApi({ search })} />);
    await ask("glaicer");
    expect(await screen.findByText(/Nothing matches/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "glacier" }));
    expect(search).toHaveBeenLastCalledWith("glacier", "keyword", 0, 10);
    expect(await screen.findByRole("heading", { name: "Glacier report" })).toBeInTheDocument();
  });

  it("explains a failure and tries again on request", async () => {
    const search = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(422, "after: expects a date like 2025-01-31"))
      .mockResolvedValueOnce(result([hit()]));
    render(<Harness client={fakeApi({ search })} />);
    await ask("x after:bad");
    expect(await screen.findByRole("alert")).toHaveTextContent("expects a date");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Glacier report" })).toBeInTheDocument();
    expect(search).toHaveBeenCalledTimes(2);
  });

  it("suggests words while typing and completes the last one with the keyboard", async () => {
    const suggest = vi.fn().mockResolvedValue(["glacier", "glaciers"]);
    render(<Harness client={fakeApi({ suggest })} />);
    const box = screen.getByLabelText("Search query");
    await userEvent.type(box, "solar gla");
    const list = await screen.findByRole("listbox");
    expect(within(list).getAllByRole("option")).toHaveLength(2);
    await userEvent.keyboard("{ArrowDown}{Enter}");
    expect(box).toHaveValue("solar glacier ");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("does not open suggestions for a query that arrived in the address", async () => {
    const suggest = vi.fn().mockResolvedValue(["glacier"]);
    render(<Harness client={fakeApi({ suggest })} start="glaicer" />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(suggest).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("does not ask for suggestions for a single letter", async () => {
    const suggest = vi.fn().mockResolvedValue(["x"]);
    render(<Harness client={fakeApi({ suggest })} />);
    await userEvent.type(screen.getByLabelText("Search query"), "g");
    expect(suggest).not.toHaveBeenCalled();
  });

  it("offers the choice of ranking only when the server supports it", () => {
    const { unmount } = render(<Harness client={fakeApi()} />);
    expect(screen.queryByLabelText("Ranking")).not.toBeInTheDocument();
    unmount();
    render(<Harness client={fakeApi()} semantic />);
    expect(screen.getByLabelText("Ranking")).toBeInTheDocument();
  });

  it("sends the chosen ranking to the server", async () => {
    const client = fakeApi({ search: vi.fn().mockResolvedValue(result([hit()])) });
    render(<Harness client={client} semantic />);
    await userEvent.selectOptions(screen.getByLabelText("Ranking"), "hybrid");
    await ask("water");
    await waitFor(() => {
      expect(client.search).toHaveBeenCalledWith("water", "hybrid", 0, 10);
    });
  });

  it("shows more results when a full page came back", async () => {
    const page = (start: number): SearchOut =>
      result(
        Array.from({ length: 10 }, (_, i) =>
          hit({ document: { ...hit().document, id: start + i, title: `Doc ${String(start + i)}` } }),
        ),
      );
    const search = vi.fn().mockResolvedValueOnce(page(0)).mockResolvedValueOnce(page(10));
    render(<Harness client={fakeApi({ search })} />);
    await ask("doc");
    await screen.findByRole("heading", { name: "Doc 0" });
    await userEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(await screen.findByRole("heading", { name: "Doc 19" })).toBeInTheDocument();
    expect(search).toHaveBeenLastCalledWith("doc", "keyword", 10, 10);
    expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(20);
  });

  it("renders document text as text, never as markup", async () => {
    const evil = hit({ snippet: "<img src=x onerror=alert(1)> glacier", highlights: [[29, 36]] });
    const { container } = render(<Harness client={fakeApi({ search: vi.fn().mockResolvedValue(result([evil])) })} />);
    await ask("glacier");
    await screen.findByRole("heading", { name: "Glacier report" });
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)> glacier");
    expect(screen.getByText("glacier", { selector: "mark" })).toBeInTheDocument();
  });
});

describe("MediaPlayer", () => {
  it("loads a signed address and shows an audio or video element", async () => {
    const client = fakeApi();
    const { container } = render(<MediaPlayer assetId={9} mediaType="audio/mpeg" startMs={5000} client={client} />);
    await userEvent.click(screen.getByRole("button", { name: "Play from 0:05" }));
    await waitFor(() => {
      expect(container.querySelector("audio")).not.toBeNull();
    });
    expect(container.querySelector("audio")?.getAttribute("src")).toBe("/v1/assets/9/file?token=abc");
  });

  it("uses a video element for video", async () => {
    const { container } = render(<MediaPlayer assetId={9} mediaType="video/mp4" startMs={0} client={fakeApi()} />);
    await userEvent.click(screen.getByRole("button"));
    await waitFor(() => {
      expect(container.querySelector("video")).not.toBeNull();
    });
  });

  it("says why it could not load", async () => {
    const client = fakeApi({ fileUrl: vi.fn().mockRejectedValue(new Error("asset not found")) });
    render(<MediaPlayer assetId={9} mediaType="video/mp4" startMs={0} client={client} />);
    await userEvent.click(screen.getByRole("button"));
    expect(await screen.findByRole("alert")).toHaveTextContent("asset not found");
  });
});

describe("UploadTab", () => {
  it("uploads files and follows them until they are searchable", async () => {
    const upload = vi.fn().mockResolvedValue({ asset_id: 5, job_id: 1, status: "queued", duplicate: false });
    const poll = vi
      .fn()
      .mockResolvedValueOnce(asset({ status: "processing" }))
      .mockResolvedValue(asset({ status: "indexed", ocr_confidence: 95.3, language: "en", duration_ms: 61_000, units: 3 }));
    render(<UploadTab accepted={[".pdf", ".txt"]} client={fakeApi({ upload, asset: poll })} pollInterval={10} />);
    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    await userEvent.upload(screen.getByLabelText("Choose files to upload"), file);
    expect(upload).toHaveBeenCalledWith(file);
    expect(await screen.findByText("Searchable")).toBeInTheDocument();
    expect(screen.getByText("3 passages, 1:01 long, language en, scan read at 95% confidence")).toBeInTheDocument();
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
  });

  it("starts with an empty state that says what will appear", () => {
    render(<UploadTab accepted={[]} client={fakeApi()} />);
    expect(screen.getByText(/Nothing added in this visit/)).toBeInTheDocument();
  });

  it("shows the server's reason when a file is refused", async () => {
    const upload = vi.fn().mockRejectedValue(new ApiError(415, "unsupported file type '.exe'"));
    render(<UploadTab accepted={[]} client={fakeApi({ upload })} pollInterval={10} />);
    await userEvent.upload(screen.getByLabelText("Choose files to upload"), new File(["x"], "tool.exe"));
    expect(await screen.findByText("Not accepted")).toBeInTheDocument();
    expect(screen.getByText(/unsupported file type/)).toBeInTheDocument();
  });

  it("reports a file that could not be read, with the reason", async () => {
    const upload = vi.fn().mockResolvedValue({ asset_id: 6, job_id: 2, status: "queued", duplicate: false });
    const poll = vi.fn().mockResolvedValue(asset({ status: "failed", error: "unreadable PDF" }));
    render(<UploadTab accepted={[]} client={fakeApi({ upload, asset: poll })} pollInterval={10} />);
    await userEvent.upload(screen.getByLabelText("Choose files to upload"), new File(["x"], "bad.pdf"));
    expect(await screen.findByText("Could not read it")).toBeInTheDocument();
    expect(screen.getByText("unreadable PDF")).toBeInTheDocument();
  });

  it("says when the same file was added before", async () => {
    const upload = vi.fn().mockResolvedValue({ asset_id: 5, job_id: null, status: "indexed", duplicate: true });
    render(<UploadTab accepted={[]} client={fakeApi({ upload })} pollInterval={10} />);
    await userEvent.upload(screen.getByLabelText("Choose files to upload"), new File(["x"], "again.txt"));
    expect(await screen.findByText("These exact bytes were added before.")).toBeInTheDocument();
  });
});

describe("LibraryTab", () => {
  const two = [
    asset(),
    asset({ asset_id: 6, title: "broken.pdf", status: "failed", error: "unreadable PDF" }),
  ];

  it("lists what was added, in a table, with the reason for a failure", async () => {
    render(<LibraryTab client={fakeApi({ assets: vi.fn().mockResolvedValue(two) })} />);
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Name" })).toBeInTheDocument();
    expect(screen.getByText("broken.pdf")).toBeInTheDocument();
    expect(screen.getByText("unreadable PDF")).toBeInTheDocument();
    expect(screen.getAllByText("2.0 KB")).toHaveLength(2);
  });

  it("asks before deleting, then deletes", async () => {
    const deleteAsset = vi.fn().mockResolvedValue(undefined);
    render(<LibraryTab client={fakeApi({ assets: vi.fn().mockResolvedValue(two), deleteAsset })} />);
    await userEvent.click(await screen.findByRole("button", { name: "Delete scan.pdf" }));
    expect(deleteAsset).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Delete for good" }));
    expect(deleteAsset).toHaveBeenCalledWith(5);
    await waitFor(() => {
      expect(screen.queryByText("scan.pdf")).not.toBeInTheDocument();
    });
    expect(screen.getByText("broken.pdf")).toBeInTheDocument();
  });

  it("keeps the file when the user changes their mind", async () => {
    const deleteAsset = vi.fn();
    render(<LibraryTab client={fakeApi({ assets: vi.fn().mockResolvedValue([asset()]), deleteAsset })} />);
    await userEvent.click(await screen.findByRole("button", { name: "Delete scan.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Keep it" }));
    expect(deleteAsset).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Delete scan.pdf" })).toBeInTheDocument();
  });

  it("shows an empty state that points at the next step", async () => {
    render(<LibraryTab client={fakeApi()} />);
    expect(await screen.findByText(/Nothing has been added yet/)).toBeInTheDocument();
  });

  it("filters by state", async () => {
    const assets = vi.fn().mockResolvedValue([]);
    render(<LibraryTab client={fakeApi({ assets })} />);
    await screen.findByText(/Nothing has been added yet/);
    await userEvent.selectOptions(screen.getByRole("combobox"), "failed");
    await waitFor(() => {
      expect(assets).toHaveBeenLastCalledWith("failed");
    });
    expect(await screen.findByText("Nothing in this state.")).toBeInTheDocument();
  });

  it("reports a load failure", async () => {
    render(<LibraryTab client={fakeApi({ assets: vi.fn().mockRejectedValue(new Error("database unavailable")) })} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");
  });
});

describe("App", () => {
  const stats = {
    documents: 1200,
    terms: 45000,
    block_postings: 0,
    delta_postings: 0,
    queue: { queued: 2, running: 1, done: 5, dead: 0 },
    accepted_extensions: [".pdf", ".png"],
    max_upload_bytes: 1,
    version: "1",
    semantic: true,
  };

  afterEach(() => {
    window.history.replaceState(null, "", "/");
  });

  it("says how big the index is and how much is being read", async () => {
    render(<App client={fakeApi({ stats: vi.fn().mockResolvedValue(stats) })} />);
    expect(await screen.findByText("1,200 documents indexed, 3 files being read")).toBeInTheDocument();
  });

  it("shows the pictures tab only when semantic search is on", async () => {
    const { unmount } = render(<App client={fakeApi({ stats: vi.fn().mockResolvedValue(stats) })} />);
    expect(await screen.findByRole("button", { name: "Pictures" })).toBeInTheDocument();
    unmount();
    render(<App client={fakeApi({ stats: vi.fn().mockResolvedValue({ ...stats, semantic: false }) })} />);
    await screen.findByText(/documents indexed/);
    expect(screen.queryByRole("button", { name: "Pictures" })).not.toBeInTheDocument();
  });

  it("keeps the search in the address bar and runs it when the page is opened with one", async () => {
    window.history.replaceState(null, "", "/?q=glacier");
    const search = vi.fn().mockResolvedValue(result([hit()]));
    render(<App client={fakeApi({ stats: vi.fn().mockResolvedValue(stats), search })} />);
    expect(await screen.findByRole("heading", { name: "Glacier report" })).toBeInTheDocument();
    expect(search).toHaveBeenCalledWith("glacier", "keyword", 0, 10);
    expect(screen.getByLabelText("Search query")).toHaveValue("glacier");
  });

  it("writes a new search to the address bar and follows Back", async () => {
    const search = vi.fn().mockResolvedValue(result([hit()]));
    render(<App client={fakeApi({ stats: vi.fn().mockResolvedValue(stats), search })} />);
    await screen.findByText(/documents indexed/);
    await ask("glacier");
    await screen.findByRole("heading", { name: "Glacier report" });
    expect(window.location.search).toBe("?q=glacier");
    await userEvent.click(screen.getByRole("button", { name: "Library" }));
    expect(window.location.search).toBe("?tab=library&q=glacier");
    act(() => {
      window.history.back();
    });
    await waitFor(() => {
      expect(window.location.search).toBe("?q=glacier");
    });
    expect(await screen.findByRole("heading", { name: "Glacier report" })).toBeInTheDocument();
  });

  it("asks for the API key when the server refuses, and unlocks with it", async () => {
    const stat = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(401, "missing or invalid API key"))
      .mockResolvedValue(stats);
    render(<App client={fakeApi({ stats: stat })} />);
    const field = await screen.findByLabelText("API key");
    await userEvent.type(field, "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Unlock" }));
    expect(await screen.findByRole("navigation", { name: "Sections" })).toBeInTheDocument();
    expect(keyStore.get()).toBe("s3cret");
  });

  it("shows when the service cannot be reached", async () => {
    render(<App client={fakeApi({ stats: vi.fn().mockRejectedValue(new Error("Failed to fetch")) })} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to fetch");
  });

  it("moves between sections and marks the current one", async () => {
    render(<App client={fakeApi({ stats: vi.fn().mockResolvedValue(stats) })} />);
    await userEvent.click(await screen.findByRole("button", { name: "Add files" }));
    expect(screen.getByRole("heading", { name: "Add files" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add files" })).toHaveAttribute("aria-current", "page");
    await userEvent.click(screen.getByRole("button", { name: "Library" }));
    expect(screen.getByRole("heading", { name: "Library" })).toBeInTheDocument();
  });
});

describe("request", () => {
  it("sends the API key as a bearer token and turns errors into ApiError", async () => {
    keyStore.set("abc");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "nope" }), { status: 403, headers: { "content-type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await expect(request("/v1/stats")).rejects.toMatchObject({ status: 403, message: "nope" });
    const headers = (fetchMock.mock.calls[0] as [string, RequestInit])[1].headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer abc");
    vi.unstubAllGlobals();
  });

  it("returns undefined for 204 and parses JSON otherwise", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(null, { status: 204 })));
    expect(await request("/v1/assets/1", { method: "DELETE" })).toBeUndefined();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response('{"a":1}', { status: 200 })));
    expect(await request("/v1/x")).toEqual({ a: 1 });
    vi.unstubAllGlobals();
  });
});
