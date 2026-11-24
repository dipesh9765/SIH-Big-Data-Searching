import { describe, expect, it } from "vitest";

import { parsePlace, placeToSearch, samePlace } from "../url";

describe("the address bar", () => {
  it("reads the tab, the query and the ranking", () => {
    expect(parsePlace("?tab=library")).toEqual({ tab: "library", q: "", ranking: "keyword" });
    expect(parsePlace("?q=climate%20change&ranking=hybrid")).toEqual({
      tab: "search",
      q: "climate change",
      ranking: "hybrid",
    });
  });
  it("ignores values it does not know", () => {
    expect(parsePlace("?tab=admin&ranking=magic")).toEqual({ tab: "search", q: "", ranking: "keyword" });
    expect(parsePlace("")).toEqual({ tab: "search", q: "", ranking: "keyword" });
  });
  it("writes only what differs from the defaults", () => {
    expect(placeToSearch({ tab: "search", q: "", ranking: "keyword" })).toBe("");
    expect(placeToSearch({ tab: "search", q: "a b", ranking: "keyword" })).toBe("?q=a+b");
    expect(placeToSearch({ tab: "add", q: "", ranking: "keyword" })).toBe("?tab=add");
    expect(placeToSearch({ tab: "search", q: "x", ranking: "semantic" })).toBe("?q=x&ranking=semantic");
  });
  it("round-trips, including non-ASCII queries", () => {
    const place = { tab: "search" as const, q: "किताब \"exact\" -x", ranking: "hybrid" as const };
    expect(parsePlace(placeToSearch(place))).toEqual(place);
  });
  it("tells whether two places are the same", () => {
    const a = { tab: "search" as const, q: "x", ranking: "keyword" as const };
    expect(samePlace(a, { ...a })).toBe(true);
    expect(samePlace(a, { ...a, q: "y" })).toBe(false);
  });
});
