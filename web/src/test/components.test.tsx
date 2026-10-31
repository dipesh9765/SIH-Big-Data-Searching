import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { ApiError, keyStore, request, type Api, type AssetOut, type HitOut, type SearchOut } from "../api";
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

describe("SearchTab", () => {
  it("searches, highlights the match and shows the summary", async () => {
    const client = fakeApi({ search: vi.fn().mockResolvedValue(result([hit()])) });
    render(<SearchTab semantic={false} client={client} suggestDelay={0} />);
    await userEvent.type(screen.getByLabelText("Search query"), "glacier");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    const card = await screen.findByRole("heading", { name: "Glacier report" });
    expect(card).toBeInTheDocument();
    expect(screen.getByText("glacier", { selector: "mark" })).toBeInTheDocument();
    expect(client.search).toHaveBeenCalledWith("glacier", "keyword", 0, 10);
    expect(screen.getByText(/1 result for “glacier”/)).toBeInTheDocument();
  });

  it("offers a correction when nothing matched and runs it on click", async () => {
    const search = vi
      .fn()
      .mockResolvedValueOnce(result([], { did_you_mean: "glacier" }))
      .mockResolvedValueOnce(result([hit()]));
    render(<SearchTab semantic={false} client={fakeApi({ search })} suggestDelay={0} />);
    await userEvent.type(screen.getByLabelText("Search query"), "glaicer{enter}");
    await userEvent.click(await screen.findByRole("button", { name: "glacier" }));
    expect(search).toHaveBeenLastCalledWith("glacier", "keyword", 0, 10);
    expect(await screen.findByRole("heading", { name: "Glacier report" })).toBeInTheDocument();
  });

  it("shows autocomplete suggestions and completes the last word with the keyboard", async () => {
    const suggest = vi.fn().mockResolvedValue(["glacier", "glaciers"]);
    render(<SearchTab semantic={false} client={fakeApi({ suggest })} suggestDelay={0} />);
    const box = screen.getByLabelText("Search query");
    await userEvent.type(box, "solar gla");
    const list = await screen.findByRole("listbox");
    expect(within(list).getAllByRole("option")).toHaveLength(2);
    await userEvent.keyboard("{ArrowDown}{Enter}");
    expect(box).toHaveValue("solar glacier ");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("does not ask for suggestions for one letter", async () => {
    const suggest = vi.fn().mockResolvedValue(["x"]);
    render(<SearchTab semantic={false} client={fakeApi({ suggest })} suggestDelay={0} />);
    await userEvent.type(screen.getByLabelText("Search query"), "g");
    expect(suggest).not.toHaveBeenCalled();
  });

  it("only offers the ranking choice when the server supports it", () => {
    const { rerender } = render(<SearchTab semantic={false} client={fakeApi()} suggestDelay={0} />);
    expect(screen.queryByLabelText("Ranking")).not.toBeInTheDocument();
    rerender(<SearchTab semantic client={fakeApi()} suggestDelay={0} />);
    expect(screen.getByLabelText("Ranking")).toBeInTheDocument();
  });

  it("passes the chosen ranking to the server", async () => {
    const client = fakeApi({ search: vi.fn().mockResolvedValue(result([hit()])) });
    render(<SearchTab semantic client={client} suggestDelay={0} />);
    await userEvent.selectOptions(screen.getByLabelText("Ranking"), "hybrid");
    await userEvent.type(screen.getByLabelText("Search query"), "water{enter}");
    await waitFor(() => { expect(client.search).toHaveBeenCalledWith("water", "hybrid", 0, 10); });
  });

  it("reports errors from the server", async () => {
    const search = vi.fn().mockRejectedValue(new ApiError(422, "after: expects a date like 2025-01-31"));
    render(<SearchTab semantic={false} client={fakeApi({ search })} suggestDelay={0} />);
    await userEvent.type(screen.getByLabelText("Search query"), "x after:bad{enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("expects a date");
  });

  it("loads the next page when a full page came back", async () => {
    const page = (start: number) =>
      result(Array.from({ length: 10 }, (_, i) => hit({ document: { ...hit().document, id: start + i, title: `Doc ${String(start + i)}` } })));
    const search = vi.fn().mockResolvedValueOnce(page(0)).mockResolvedValueOnce(page(10));
    render(<SearchTab semantic={false} client={fakeApi({ search })} suggestDelay={0} />);
    await userEvent.type(screen.getByLabelText("Search query"), "doc{enter}");
    await screen.findByRole("heading", { name: "Doc 0" });
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("heading", { name: "Doc 19" })).toBeInTheDocument();
    expect(search).toHaveBeenLastCalledWith("doc", "keyword", 10, 10);
    expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(20);
  });

  it("renders document text as text, never as markup", async () => {
    const evil = hit({ snippet: "<img src=x onerror=alert(1)> glacier", highlights: [[29, 36]] });
    const client = fakeApi({ search: vi.fn().mockResolvedValue(result([evil])) });
    const { container } = render(<SearchTab semantic={false} client={client} suggestDelay={0} />);
    await userEvent.type(screen.getByLabelText("Search query"), "glacier{enter}");
    await screen.findByRole("heading", { name: "Glacier report" });
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)> glacier");
    expect(screen.getByText("glacier", { selector: "mark" })).toBeInTheDocument();
  });

  it("shows a play button for a hit inside a recording", async () => {
    const media = { asset_id: 9, kind: "transcript", start_ms: 83_000, end_ms: 99_000, file_url: "/x" };
    const client = fakeApi({
      search: vi.fn().mockResolvedValue(
        result([hit({ media, document: { ...hit().document, media_type: "audio/mpeg" } })]),
      ),
    });
    render(<SearchTab semantic={false} client={client} suggestDelay={0} />);
    await userEvent.type(screen.getByLabelText("Search query"), "glacier{enter}");
    expect(await screen.findByRole("button", { name: /Play from 1:23/ })).toBeInTheDocument();
  });
});

describe("MediaPlayer", () => {
  it("loads a signed URL and renders an audio or video element", async () => {
    const client = fakeApi();
    const { container, rerender } = render(
      <MediaPlayer assetId={9} mediaType="audio/mpeg" startMs={5000} client={client} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /Play from 0:05/ }));
    await waitFor(() => { expect(container.querySelector("audio")).not.toBeNull(); });
    expect(container.querySelector("audio")?.getAttribute("src")).toBe("/v1/assets/9/file?token=abc");
    rerender(<MediaPlayer assetId={10} mediaType="video/mp4" startMs={0} client={client} />);
  });

  it("shows why it could not load", async () => {
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
    expect(await screen.findByText("indexed")).toBeInTheDocument();
    expect(screen.getByText(/3 segments · 1:01 · en · OCR confidence 95%/)).toBeInTheDocument();
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
  });

  it("shows the server's reason when a file is refused", async () => {
    const upload = vi.fn().mockRejectedValue(new ApiError(415, "unsupported file type '.exe'"));
    render(<UploadTab accepted={[]} client={fakeApi({ upload })} pollInterval={10} />);
    await userEvent.upload(screen.getByLabelText("Choose files to upload"), new File(["x"], "tool.exe"));
    expect(await screen.findByText("refused")).toBeInTheDocument();
    expect(screen.getByText(/unsupported file type/)).toBeInTheDocument();
  });

  it("reports a failed file with its reason", async () => {
    const upload = vi.fn().mockResolvedValue({ asset_id: 6, job_id: 2, status: "queued", duplicate: false });
    const poll = vi.fn().mockResolvedValue(asset({ status: "failed", error: "unreadable PDF" }));
    render(<UploadTab accepted={[]} client={fakeApi({ upload, asset: poll })} pollInterval={10} />);
    await userEvent.upload(screen.getByLabelText("Choose files to upload"), new File(["x"], "bad.pdf"));
    expect(await screen.findByText("failed")).toBeInTheDocument();
    expect(screen.getByText("unreadable PDF")).toBeInTheDocument();
  });

  it("says when a file was uploaded before", async () => {
    const upload = vi.fn().mockResolvedValue({ asset_id: 5, job_id: null, status: "indexed", duplicate: true });
    render(<UploadTab accepted={[]} client={fakeApi({ upload })} pollInterval={10} />);
    await userEvent.upload(screen.getByLabelText("Choose files to upload"), new File(["x"], "again.txt"));
    expect(await screen.findByText("already uploaded")).toBeInTheDocument();
  });
});

describe("LibraryTab", () => {
  it("lists uploads and deletes one after confirmation", async () => {
    const assets = vi.fn().mockResolvedValue([asset(), asset({ asset_id: 6, title: "broken.pdf", status: "failed", error: "unreadable PDF" })]);
    const deleteAsset = vi.fn().mockResolvedValue(undefined);
    const confirm = vi.fn().mockReturnValue(true);
    render(<LibraryTab client={fakeApi({ assets, deleteAsset })} confirm={confirm} />);
    expect(await screen.findByText("broken.pdf")).toBeInTheDocument();
    expect(screen.getByText("unreadable PDF")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete scan.pdf" }));
    expect(confirm).toHaveBeenCalled();
    expect(deleteAsset).toHaveBeenCalledWith(5);
    await waitFor(() => { expect(screen.queryByText("scan.pdf")).not.toBeInTheDocument(); });
    expect(screen.getByText("broken.pdf")).toBeInTheDocument();
  });

  it("does nothing when the deletion is declined", async () => {
    const deleteAsset = vi.fn();
    render(<LibraryTab client={fakeApi({ assets: vi.fn().mockResolvedValue([asset()]), deleteAsset })} confirm={() => false} />);
    await userEvent.click(await screen.findByRole("button", { name: "Delete scan.pdf" }));
    expect(deleteAsset).not.toHaveBeenCalled();
    expect(screen.getByText("scan.pdf")).toBeInTheDocument();
  });

  it("filters by status", async () => {
    const assets = vi.fn().mockResolvedValue([]);
    render(<LibraryTab client={fakeApi({ assets })} />);
    await screen.findByText("Nothing here yet.");
    await userEvent.selectOptions(screen.getByRole("combobox"), "failed");
    await waitFor(() => { expect(assets).toHaveBeenLastCalledWith("failed"); });
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

  it("shows index status and the image tab when semantic search is on", async () => {
    render(<App client={fakeApi({ stats: vi.fn().mockResolvedValue(stats) })} />);
    expect(await screen.findByText(/1,200 documents · 45,000 terms · 3 jobs in progress/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Images" })).toBeInTheDocument();
  });

  it("hides the image tab without semantic search", async () => {
    render(<App client={fakeApi({ stats: vi.fn().mockResolvedValue({ ...stats, semantic: false }) })} />);
    await screen.findByText(/documents/);
    expect(screen.queryByRole("button", { name: "Images" })).not.toBeInTheDocument();
  });

  it("asks for the API key when the server refuses, and unlocks with it", async () => {
    const fetchStats = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(401, "missing or invalid API key"))
      .mockResolvedValue(stats);
    render(<App client={fakeApi({ stats: fetchStats })} />);
    const field = await screen.findByLabelText(/needs an API key/);
    await userEvent.type(field, "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Unlock" }));
    expect(await screen.findByRole("navigation", { name: "Sections" })).toBeInTheDocument();
    expect(keyStore.get()).toBe("s3cret");
  });

  it("shows when the service cannot be reached", async () => {
    render(<App client={fakeApi({ stats: vi.fn().mockRejectedValue(new Error("Failed to fetch")) })} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to fetch");
  });

  it("switches tabs", async () => {
    render(<App client={fakeApi({ stats: vi.fn().mockResolvedValue(stats) })} />);
    await userEvent.click(await screen.findByRole("button", { name: "Upload" }));
    expect(screen.getByRole("region", { name: "Upload" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Library" }));
    expect(screen.getByRole("region", { name: "Library" })).toBeInTheDocument();
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
