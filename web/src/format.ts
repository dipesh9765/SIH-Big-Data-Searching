/** 83_000 ms -> "1:23"; 3_725_000 ms -> "1:02:05". */
export function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const two = (n: number): string => String(n).padStart(2, "0");
  return hours > 0 ? `${hours}:${two(minutes)}:${two(seconds)}` : `${minutes}:${two(seconds)}`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit] ?? "GB"}`;
}

export type Segment = { text: string; match: boolean };

/**
 * Cut `text` into plain and highlighted pieces from [start, end) offsets.
 * The server sends offsets, not HTML, so nothing from a document is ever rendered as markup.
 * Offsets that are out of order, overlapping or out of range are ignored rather than trusted.
 */
export function segments(text: string, highlights: readonly [number, number][]): Segment[] {
  const pieces: Segment[] = [];
  let cursor = 0;
  for (const [start, end] of [...highlights].sort((a, b) => a[0] - b[0])) {
    if (start < cursor || end <= start || end > text.length) continue;
    if (start > cursor) pieces.push({ text: text.slice(cursor, start), match: false });
    pieces.push({ text: text.slice(start, end), match: true });
    cursor = end;
  }
  if (cursor < text.length) pieces.push({ text: text.slice(cursor), match: false });
  return pieces;
}

/** The last whitespace-separated word of `text` and what precedes it, for autocomplete. */
export function splitLastWord(text: string): { head: string; word: string } {
  const match = /^(.*?)(\S*)$/s.exec(text);
  return { head: match?.[1] ?? "", word: match?.[2] ?? "" };
}
