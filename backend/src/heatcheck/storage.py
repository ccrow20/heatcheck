"""S3 and local-folder storage.

gamelogs/season=2026-27/date=2026-11-05.json   data bucket, one file per night
data/streaks.json                              site bucket
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from .dates import season_for

STREAKS_KEY = "data/streaks.json"


def gamelog_key(day: date) -> str:
    return f"gamelogs/season={season_for(day)}/date={day.isoformat()}.json"


def season_prefix(season: str) -> str:
    return f"gamelogs/season={season}/"


class Storage(Protocol):
    def get_json(self, key: str) -> Any | None: ...

    def put_json(self, key: str, obj: Any, *, cache_control: str | None = None) -> None: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def get_many_json(self, keys: list[str]) -> dict[str, Any]: ...


class LocalStorage:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents and path != self.root.resolve():
            raise ValueError(f"key {key!r} escapes the storage root")
        return path

    def get_json(self, key: str) -> Any | None:
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def put_json(self, key: str, obj: Any, *, cache_control: str | None = None) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)  # atomic on POSIX, so readers never see half a file

    def list_keys(self, prefix: str) -> list[str]:
        base = self.root / prefix
        search_root = base if base.is_dir() else base.parent
        if not search_root.exists():
            return []
        keys = []
        for path in search_root.rglob("*"):
            if path.is_file() and not path.name.endswith(".tmp"):
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    keys.append(key)
        return sorted(keys)

    def get_many_json(self, keys: list[str]) -> dict[str, Any]:
        return {key: self.get_json(key) for key in keys}


class S3Storage:
    def __init__(self, bucket: str, *, client: Any = None, max_workers: int = 16):
        self.bucket = bucket
        self.max_workers = max_workers
        if client is None:
            import boto3  # available in the Lambda runtime; install locally for S3 access

            client = boto3.client("s3")
        self.client = client

    def get_json(self, key: str) -> Any | None:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as err:
            if _is_not_found(err):
                return None
            raise
        return json.loads(resp["Body"].read().decode("utf-8"))

    def put_json(self, key: str, obj: Any, *, cache_control: str | None = None) -> None:
        extra = {"CacheControl": cache_control} if cache_control else {}
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(obj, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json",
            **extra,
        )

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kwargs)
            keys.extend(obj["Key"] for obj in resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                return sorted(keys)
            token = resp.get("NextContinuationToken")

    def get_many_json(self, keys: list[str]) -> dict[str, Any]:
        """Fetch many objects in parallel; boto3 clients are thread-safe."""
        if not keys:
            return {}
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(keys))) as pool:
            return dict(zip(keys, pool.map(self.get_json, keys), strict=True))


def _is_not_found(err: Exception) -> bool:
    code = str(getattr(err, "response", {}).get("Error", {}).get("Code", ""))
    return code in {"NoSuchKey", "404", "NotFound"}


def open_storage(location: str) -> Storage:
    """``s3://bucket`` -> S3Storage, anything else -> LocalStorage at that path."""
    if location.startswith("s3://"):
        bucket = location[len("s3://") :].strip("/")
        if not bucket or "/" in bucket:
            raise ValueError(f"expected s3://bucket-name, got {location!r}")
        return S3Storage(bucket)
    return LocalStorage(location)
