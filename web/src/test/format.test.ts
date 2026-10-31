import { describe, expect, it } from "vitest";

import { formatBytes, formatClock, segments, splitLastWord } from "../format";

describe("formatClock", () => {
  it("formats minutes and seconds", () => {
    expect(formatClock(0)).toBe("0:00");
    expect(formatClock(83_000)).toBe("1:23");
    expect(formatClock(59_999)).toBe("0:59");
  });
  it("adds hours", () => {
    expect(formatClock(3_725_000)).toBe("1:02:05");
  });
  it("never goes negative", () => {
    expect(formatClock(-5)).toBe("0:00");
  });
});

describe("formatBytes", () => {
  it("scales units", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(formatBytes(300 * 1024 * 1024)).toBe("300 MB");
  });
});

describe("segments", () => {
  it("splits text around highlighted ranges", () => {
    expect(segments("the glacier melts", [[4, 11]])).toEqual([
      { text: "the ", match: false },
      { text: "glacier", match: true },
      { text: " melts", match: false },
    ]);
  });
  it("handles a highlight at either end and several highlights", () => {
    const parts = segments("aa bb cc", [[0, 2], [6, 8]]);
    expect(parts.map((p) => p.text)).toEqual(["aa", " bb ", "cc"]);
    expect(parts.map((p) => p.match)).toEqual([true, false, true]);
  });
  it("sorts offsets and ignores overlapping, empty or out-of-range ones", () => {
    const parts = segments("abcdef", [[4, 6], [0, 2], [1, 3], [3, 3], [5, 99]]);
    expect(parts.filter((p) => p.match).map((p) => p.text)).toEqual(["ab", "ef"]);
    expect(parts.map((p) => p.text).join("")).toBe("abcdef");
  });
  it("returns plain text when there is nothing to highlight", () => {
    expect(segments("plain", [])).toEqual([{ text: "plain", match: false }]);
    expect(segments("", [])).toEqual([]);
  });
  it("works for Devanagari offsets", () => {
    const text = "वहाँ किताबें मिलती हैं";
    const start = text.indexOf("किताबें");
    const parts = segments(text, [[start, start + "किताबें".length]]);
    expect(parts.find((p) => p.match)?.text).toBe("किताबें");
  });
});

describe("splitLastWord", () => {
  it("separates the word being typed", () => {
    expect(splitLastWord("solar wi")).toEqual({ head: "solar ", word: "wi" });
    expect(splitLastWord("solar ")).toEqual({ head: "solar ", word: "" });
    expect(splitLastWord("single")).toEqual({ head: "", word: "single" });
    expect(splitLastWord("")).toEqual({ head: "", word: "" });
  });
});
