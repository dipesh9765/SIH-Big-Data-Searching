export interface DocumentOut {
  id: number;
  title: string;
  source_name: string;
  media_type: string;
  tokens: number;
  created_at: string;
}

export interface MediaOut {
  asset_id: number;
  kind: string;
  start_ms: number | null;
  end_ms: number | null;
  file_url: string;
}

export interface HitOut {
  document: DocumentOut;
  score: number;
  snippet: string;
  highlights: [number, number][];
  media: MediaOut | null;
}

export type Ranking = "keyword" | "semantic" | "hybrid";

export interface SearchOut {
  query: string;
  mode: "all" | "any";
  total: number | null;
  approximate: boolean;
  did_you_mean: string | null;
  strategy: string;
  took_ms: number;
  results: HitOut[];
}

export type AssetStatus = "queued" | "processing" | "indexed" | "failed";

export interface AssetOut {
  asset_id: number;
  title: string;
  source_name: string;
  size: number;
  status: AssetStatus;
  document_id: number | null;
  error: string | null;
  created_at: string;
  ocr_confidence: number | null;
  units: number;
  duration_ms: number | null;
  language: string | null;
}

export interface SubmissionOut {
  asset_id: number;
  job_id: number | null;
  status: AssetStatus;
  duplicate: boolean;
}

export interface ImageHitOut {
  asset_id: number;
  title: string;
  source_name: string;
  similarity: number;
  file_url: string;
}

export interface StatsOut {
  documents: number;
  terms: number;
  block_postings: number;
  delta_postings: number;
  queue: Record<string, number>;
  accepted_extensions: string[];
  max_upload_bytes: number;
  version: string;
  semantic: boolean;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const KEY_STORAGE = "docsearch.apiKey";

/** The API key lives in sessionStorage: it is dropped when the tab closes, and never in a URL. */
export const keyStore = {
  get(): string {
    try {
      return sessionStorage.getItem(KEY_STORAGE) ?? "";
    } catch {
      return "";
    }
  },
  set(value: string): void {
    try {
      if (value) sessionStorage.setItem(KEY_STORAGE, value);
      else sessionStorage.removeItem(KEY_STORAGE);
    } catch {
      /* storage can be unavailable (private mode); the key then lasts until reload */
    }
  },
};

async function errorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return "The request was not valid.";
  } catch {
    /* not JSON */
  }
  return `${response.status} ${response.statusText}`.trim();
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const key = keyStore.get();
  if (key) headers.set("Authorization", `Bearer ${key}`);
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) throw new ApiError(response.status, await errorMessage(response));
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(name, String(value));
  }
  return search.toString();
}

export const api = {
  stats: () => request<StatsOut>("/v1/stats"),
  search: (q: string, ranking: Ranking, offset = 0, limit = 10) =>
    request<SearchOut>(`/v1/search?${query({ q, ranking, offset, limit })}`),
  suggest: (q: string) => request<string[]>(`/v1/suggest?${query({ q })}`),
  searchImages: (q: string) => request<ImageHitOut[]>(`/v1/search/images?${query({ q })}`),
  upload: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<SubmissionOut>("/v1/documents", { method: "POST", body: form });
  },
  asset: (id: number) => request<AssetOut>(`/v1/assets/${id}`),
  assets: (status?: AssetStatus) =>
    request<AssetOut[]>(`/v1/assets?${query({ status, limit: 50 })}`),
  deleteAsset: (id: number) => request<undefined>(`/v1/assets/${id}`, { method: "DELETE" }),
  fileUrl: async (assetId: number) =>
    (await request<{ url: string }>(`/v1/assets/${assetId}/file-url`, { method: "POST" })).url,
};

export type Api = typeof api;
