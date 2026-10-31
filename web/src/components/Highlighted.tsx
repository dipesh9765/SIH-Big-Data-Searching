import { segments } from "../format";

interface Props {
  text: string;
  highlights: readonly [number, number][];
}

/** Text with the matching ranges wrapped in <mark>. Rendered as text nodes only, never as HTML. */
export function Highlighted({ text, highlights }: Props) {
  return (
    <>
      {segments(text, highlights).map((piece, index) =>
        piece.match ? <mark key={index}>{piece.text}</mark> : <span key={index}>{piece.text}</span>,
      )}
    </>
  );
}
