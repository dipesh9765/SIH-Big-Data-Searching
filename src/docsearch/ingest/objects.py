"""Object storage for the original uploaded files.

Files never go in the database. They are stored under a key derived from their SHA-256, so identical
uploads share one object, a key can never collide with a different file, and no user-supplied text
(a file name) ever becomes part of a storage path.
"""

from __future__ import annotations

import io
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from minio import Minio
from minio.error import S3Error


class ObjectNotFoundError(KeyError):
    """The key is not in the store."""


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    key: str
    size: int
    modified: float  # seconds since the epoch


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = ...) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def put_file(self, key: str, path: Path, content_type: str = ...) -> None:
        """Upload a local file without reading it into memory."""
        ...

    def download(self, key: str, path: Path) -> None:
        """Write the object to a local file without holding it in memory."""
        ...

    def size(self, key: str) -> int: ...

    def get_range(self, key: str, start: int, length: int) -> bytes:
        """`length` bytes from `start` (fewer at the end of the object)."""
        ...

    def list_objects(self, prefix: str) -> Iterator[ObjectInfo]: ...

    def ping(self) -> None:
        """Raise if the store cannot be reached."""
        ...


def object_key(sha256_hex: str) -> str:
    """`assets/ab/abcdef...`; the two-character fan-out keeps directory listings small."""
    if len(sha256_hex) != 64 or any(c not in "0123456789abcdef" for c in sha256_hex):
        raise ValueError("expected a lower-case hex SHA-256")
    return f"assets/{sha256_hex[:2]}/{sha256_hex}"


class MemoryStore:
    """In-process store for tests and single-process demos."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._objects: dict[str, bytes] = {}
        self._modified: dict[str, float] = {}

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        with self._lock:
            self._objects[key] = bytes(data)
            self._modified[key] = time.time()

    def get(self, key: str) -> bytes:
        with self._lock:
            try:
                return self._objects[key]
            except KeyError:
                raise ObjectNotFoundError(key) from None

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._objects

    def delete(self, key: str) -> None:
        with self._lock:
            self._objects.pop(key, None)
            self._modified.pop(key, None)

    def put_file(
        self, key: str, path: Path, content_type: str = "application/octet-stream"
    ) -> None:
        self.put(key, path.read_bytes(), content_type)

    def download(self, key: str, path: Path) -> None:
        path.write_bytes(self.get(key))

    def size(self, key: str) -> int:
        return len(self.get(key))

    def get_range(self, key: str, start: int, length: int) -> bytes:
        return self.get(key)[start : start + length]

    def list_objects(self, prefix: str) -> Iterator[ObjectInfo]:
        with self._lock:
            snapshot = [
                ObjectInfo(k, len(v), self._modified[k])
                for k, v in self._objects.items()
                if k.startswith(prefix)
            ]
        yield from snapshot

    def ping(self) -> None:
        return None


class S3Store:
    """Any S3-compatible server (SeaweedFS, MinIO, Ceph, AWS S3) through the `minio` client."""

    def __init__(  # noqa: PLR0913 - connection settings; the last two are keyword-only
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        *,
        secure: bool = False,
        create_bucket: bool = True,
    ) -> None:
        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._bucket = bucket
        if create_bucket and not self._client.bucket_exists(bucket):
            try:
                self._client.make_bucket(bucket)
            except S3Error as exc:
                # Several processes (the API and each worker) start together and all create the
                # bucket; losing that race is success, not an error.
                if exc.code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    raise

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._client.put_object(
            self._bucket, key, io.BytesIO(data), len(data), content_type=content_type
        )

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(self._bucket, key)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                raise ObjectNotFoundError(key) from exc
            raise
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def exists(self, key: str) -> bool:
        try:
            self._client.stat_object(self._bucket, key)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                return False
            raise
        return True

    def put_file(
        self, key: str, path: Path, content_type: str = "application/octet-stream"
    ) -> None:
        self._client.fput_object(self._bucket, key, str(path), content_type=content_type)

    def download(self, key: str, path: Path) -> None:
        try:
            self._client.fget_object(self._bucket, key, str(path))
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                raise ObjectNotFoundError(key) from exc
            raise

    def size(self, key: str) -> int:
        try:
            return int(self._client.stat_object(self._bucket, key).size or 0)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                raise ObjectNotFoundError(key) from exc
            raise

    def get_range(self, key: str, start: int, length: int) -> bytes:
        try:
            response = self._client.get_object(self._bucket, key, offset=start, length=length)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                raise ObjectNotFoundError(key) from exc
            raise
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def delete(self, key: str) -> None:
        self._client.remove_object(self._bucket, key)

    def list_objects(self, prefix: str) -> Iterator[ObjectInfo]:
        for item in self._client.list_objects(self._bucket, prefix=prefix, recursive=True):
            if item.object_name is None or item.is_dir:
                continue
            modified = item.last_modified.timestamp() if item.last_modified else 0.0
            yield ObjectInfo(item.object_name, int(item.size or 0), modified)

    def ping(self) -> None:
        self._client.bucket_exists(self._bucket)
