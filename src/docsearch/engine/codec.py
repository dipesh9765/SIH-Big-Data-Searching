"""Compressed postings blocks.

A postings list (the documents containing one term, with the term's frequency in each) is stored
as a sequence of blocks of up to `BLOCK_SIZE` postings. Each block is self-contained:

* the metadata (`first_doc`, `last_doc`, `count`, `max_tf`, `min_len`) is kept outside the payload,
  so a reader can skip a block, or bound the best score any posting in it can reach, without
  decompressing anything;
* the payload is the block's document ids as varint *deltas* followed by the term frequencies as
  varints. Ids are sorted, so deltas are small and most take one byte.

This is the same shape Lucene and most search engines use (delta + variable-byte or bit-packing,
fixed-size blocks, per-block maxima for pruning). Bit-packing would be denser; varint was chosen
because it is ~20 lines, easy to verify, and fast enough in pure Python (see ADR 0006).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

BLOCK_SIZE = 128


def encode_varints(values: Iterable[int], out: bytearray | None = None) -> bytearray:
    """LEB128-style unsigned varints: 7 bits per byte, high bit set on all but the last byte."""
    buffer = bytearray() if out is None else out
    for number in values:
        if number < 0:
            raise ValueError("varints are unsigned")
        value = number
        while value >= 0x80:
            buffer.append((value & 0x7F) | 0x80)
            value >>= 7
        buffer.append(value)
    return buffer


def decode_varints(data: bytes | memoryview, count: int, offset: int = 0) -> tuple[list[int], int]:
    """Read `count` varints starting at `offset`; returns (values, offset after the last one)."""
    values: list[int] = []
    position = offset
    size = len(data)
    for _ in range(count):
        shift = 0
        result = 0
        while True:
            if position >= size:
                raise ValueError("truncated varint")
            byte = data[position]
            position += 1
            result |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
            if shift > 63:
                raise ValueError("varint too long")
        values.append(result)
    return values, position


@dataclass(frozen=True, slots=True)
class Block:
    """One compressed run of postings plus the metadata needed to skip or bound it."""

    first_doc: int
    last_doc: int
    count: int
    max_tf: int  # largest term frequency in the block
    min_len: int  # shortest document in the block (a short document scores higher)
    payload: bytes

    def decode(self) -> tuple[list[int], list[int]]:
        """Return (document ids, term frequencies), both in ascending-id order."""
        count = self.count
        payload = self.payload
        if len(payload) == 2 * count - 1:
            # Every varint is at least one byte, so this length means every one is exactly one
            # byte: slice the bytes directly (C speed). Dense lists of common terms mostly land
            # here, and they are where most of the decoding time goes.
            deltas: Sequence[int] = payload[: count - 1]
            tfs: Sequence[int] = payload[count - 1 :]
        else:
            deltas, position = decode_varints(payload, count - 1)
            tfs, end = decode_varints(payload, count, position)
            if end != len(payload):
                raise ValueError("block payload has trailing bytes")
        doc_ids = list(itertools.accumulate(deltas, initial=self.first_doc))
        if doc_ids[-1] != self.last_doc:
            raise ValueError("block metadata does not match its payload")
        return doc_ids, list(tfs)


def encode_block(doc_ids: Sequence[int], tfs: Sequence[int], lengths: Sequence[int]) -> Block:
    """Compress one block. `doc_ids` must be strictly ascending and non-empty."""
    count = len(doc_ids)
    if count == 0 or count > BLOCK_SIZE:
        raise ValueError(f"a block holds 1..{BLOCK_SIZE} postings, got {count}")
    if not (len(tfs) == len(lengths) == count):
        raise ValueError("doc_ids, tfs and lengths must be the same length")
    if any(b <= a for a, b in itertools.pairwise(doc_ids)):
        raise ValueError("doc ids must be strictly ascending")
    if min(tfs) < 1:
        raise ValueError("term frequencies are at least 1")
    payload = encode_varints(b - a for a, b in itertools.pairwise(doc_ids))
    encode_varints(tfs, payload)
    return Block(
        first_doc=doc_ids[0],
        last_doc=doc_ids[-1],
        count=count,
        max_tf=max(tfs),
        min_len=min(lengths),
        payload=bytes(payload),
    )


def build_blocks(
    postings: Iterable[tuple[int, int]],
    length_of: Mapping[int, int] | npt.NDArray[np.int64] | None = None,
) -> list[Block]:
    """Split sorted (doc_id, tf) postings into blocks. `length_of` maps doc id -> token length."""
    blocks: list[Block] = []
    ids: list[int] = []
    tfs: list[int] = []
    for doc_id, tf in postings:
        ids.append(doc_id)
        tfs.append(tf)
        if len(ids) == BLOCK_SIZE:
            blocks.append(_flush(ids, tfs, length_of))
            ids, tfs = [], []
    if ids:
        blocks.append(_flush(ids, tfs, length_of))
    return blocks


def _flush(
    ids: list[int], tfs: list[int], length_of: Mapping[int, int] | npt.NDArray[np.int64] | None
) -> Block:
    lengths = [1] * len(ids) if length_of is None else [int(length_of[d]) for d in ids]
    return encode_block(ids, tfs, lengths)


def iter_postings(blocks: Iterable[Block]) -> Iterator[tuple[int, int]]:
    """Decode every block in order (used by tests and by the compactor's merge)."""
    for block in blocks:
        doc_ids, tfs = block.decode()
        yield from zip(doc_ids, tfs, strict=True)
