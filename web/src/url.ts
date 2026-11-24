import type { Ranking } from "./api";

export type Tab = "search" | "add" | "library" | "pictures";

/** Where the user is. It lives in the address bar, so a search can be shared and Back works. */
export interface Place {
  tab: Tab;
  q: string;
  ranking: Ranking;
}

const TABS: readonly Tab[] = ["search", "add", "library", "pictures"];
const RANKINGS: readonly Ranking[] = ["keyword", "semantic", "hybrid"];

export function parsePlace(search: string): Place {
  const params = new URLSearchParams(search);
  const tab = params.get("tab");
  const ranking = params.get("ranking");
  return {
    tab: TABS.find((t) => t === tab) ?? "search",
    q: params.get("q") ?? "",
    ranking: RANKINGS.find((r) => r === ranking) ?? "keyword",
  };
}

export function placeToSearch(place: Place): string {
  const params = new URLSearchParams();
  if (place.tab !== "search") params.set("tab", place.tab);
  if (place.q) params.set("q", place.q);
  if (place.ranking !== "keyword") params.set("ranking", place.ranking);
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function samePlace(a: Place, b: Place): boolean {
  return a.tab === b.tab && a.q === b.q && a.ranking === b.ranking;
}
