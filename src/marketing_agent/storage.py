"""Open the SQLite database, optionally shared through S3.

When AGENT_DB_S3_URI is set, the database file is downloaded from S3, used locally, and
uploaded back when the work is done (even after an error, so sent messages are never
forgotten and re-sent). A lock object next to it (`<key>.lock`, created with S3's
conditional `If-None-Match: *` write) keeps the Lambda run and the CLI from working on
the database at the same time.
"""

from __future__ import annotations

import json
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator
from urllib.parse import urlparse

from .config import Settings
from .db import Database


class LockBusy(RuntimeError):
    pass


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError(f"AGENT_DB_S3_URI must look like s3://bucket/agent.db, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def _error_code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


class S3Lock:
    def __init__(self, s3, bucket: str, key: str, *, owner: str, ttl: timedelta):
        self.s3, self.bucket, self.key, self.owner, self.ttl = s3, bucket, key, owner, ttl

    def _put(self) -> bool:
        now = datetime.now(timezone.utc)
        body = json.dumps({"owner": self.owner, "acquired_at": now.isoformat(),
                           "expires_at": (now + self.ttl).isoformat()})
        try:
            self.s3.put_object(Bucket=self.bucket, Key=self.key, Body=body.encode(), IfNoneMatch="*")
            return True
        except Exception as exc:
            if _error_code(exc) not in ("PreconditionFailed", "ConditionalRequestConflict", "412", "409"):
                raise
            return False

    def _try_acquire(self) -> bool:
        if self._put():
            return True
        holder = self.holder()
        if holder and datetime.fromisoformat(holder["expires_at"]) < datetime.now(timezone.utc):
            # Stale lock left by a crashed run: take it over.
            self.s3.delete_object(Bucket=self.bucket, Key=self.key)
            return self._put()
        return False

    def holder(self) -> dict | None:
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=self.key)["Body"].read())
        except Exception as exc:
            if _error_code(exc) in ("NoSuchKey", "404"):
                return None
            raise

    def acquire(self, wait_seconds: float = 0, poll_seconds: float = 15) -> None:
        deadline = time.monotonic() + wait_seconds
        while not self._try_acquire():
            if time.monotonic() >= deadline:
                holder = self.holder() or {}
                raise LockBusy(f"Database is in use by {holder.get('owner', 'another process')} "
                               f"since {holder.get('acquired_at', '?')}. Try again in a few minutes.")
            time.sleep(poll_seconds)

    def release(self) -> None:
        holder = self.holder()
        if holder and holder.get("owner") == self.owner:
            self.s3.delete_object(Bucket=self.bucket, Key=self.key)


@contextmanager
def open_database(settings: Settings, *, owner: str = "", lock_wait_seconds: float = 0,
                  lock_ttl: timedelta = timedelta(hours=2), s3=None) -> Iterator[Database]:
    if not settings.db_s3_uri:
        db = Database(settings.db_path)
        try:
            yield db
        finally:
            db.conn.close()
        return

    if s3 is None:
        import boto3
        s3 = boto3.client("s3")
    bucket, key = parse_s3_uri(settings.db_s3_uri)
    owner = owner or f"cli@{socket.gethostname()}"
    lock = S3Lock(s3, bucket, f"{key}.lock", owner=f"{owner}#{time.time_ns()}", ttl=lock_ttl)
    lock.acquire(lock_wait_seconds)
    try:
        local = settings.db_path
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            s3.download_file(bucket, key, str(local))
        except Exception as exc:
            if _error_code(exc) not in ("404", "NoSuchKey"):
                raise
            # First run: start from the local file if there is one, else an empty database.
        db = Database(local)
        try:
            yield db
        finally:
            db.conn.close()
            s3.upload_file(str(local), bucket, key)
    finally:
        lock.release()
