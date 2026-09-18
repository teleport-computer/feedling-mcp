"""Durable hourly log spool. Only validated lines enter this store.

Append/fsync precedes cursor replacement: a crash may replay lines, never skip
an acknowledged append. Reopening a sealed hour appends via a new gzip member
into an atomic replacement, so late/replayed lines never overwrite old ones.
R2 receipts name a content digest; changing an hour makes it pending again.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading


ALIASES = frozenset({"enclave", "enclave-domain"})
HOUR_FILE = re.compile(r"([0-2][0-9])\.log\.(?:part|gz|gz\.sent|gz\.tmp|gz\.sent\.tmp)$")


def utcnow():
    return datetime.now(timezone.utc)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)
    sync_dir(path.parent)


class HourlyGzStore:
    def __init__(self, root, retention_days=30):
        if not 1 <= retention_days <= 365:
            raise ValueError("invalid_retention")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.lock = threading.RLock()

    def _folder(self, alias):
        if alias not in ALIASES:
            raise ValueError("invalid_container_alias")
        path = self.root / alias
        path.mkdir(exist_ok=True)
        return path

    def cursor(self, alias, container_id):
        """A new ID starts at zero: do not skip overlapping replacement logs."""
        with self.lock:
            try:
                data = json.loads((self._folder(alias) / "cursor.json").read_bytes())
            except FileNotFoundError:
                return (0, 0)
            # Corrupt cursor fails the attempt; never invent a successful resume.
            if (not isinstance(data, dict) or type(data.get("ns")) is not int or data["ns"] < 0
                    or type(data.get("count")) is not int or data["count"] < 1):
                raise ValueError("invalid_cursor")
            return (data["ns"], data["count"]) if data.get("id") == container_id else (0, 0)

    def checkpoint(self, alias, container_id, ns, count):
        with self.lock:
            path = self._folder(alias) / "cursor.json"
            atomic_write(path, json.dumps({"id": container_id, "ns": ns, "count": count}).encode())

    @staticmethod
    def _repair_tail(stream):
        # A kill/disk-full during append can leave an unacknowledged partial
        # record. Remove only that tail; the durable cursor replays the record.
        stream.seek(0, 2)
        end = stream.tell()
        position = end
        while position:
            start = max(0, position - 4096)
            stream.seek(start)
            block = stream.read(position - start)
            found = block.rfind(b"\n")
            if found >= 0:
                end = start + found + 1
                break
            position = start
        else:
            end = 0
        stream.truncate(end)
        stream.seek(0, 2)

    def append(self, alias, timestamp, line):
        with self.lock:
            folder = self._folder(alias) / timestamp.strftime("%Y-%m-%d")
            folder.mkdir(exist_ok=True)
            path = folder / timestamp.strftime("%H.log.part")
            with path.open("a+b") as out:
                self._repair_tail(out)
                out.write(line)
                out.flush()
                os.fsync(out.fileno())
            # The file and parent entry must survive before checkpoint advances.
            sync_dir(folder)
            sync_dir(folder.parent)

    def _seal(self, part):
        with part.open("r+b") as tail:
            self._repair_tail(tail)
            tail.flush()
            os.fsync(tail.fileno())
        final = part.with_suffix(".gz")
        temp = part.with_suffix(".gz.tmp")
        with temp.open("wb") as out:
            if final.exists():
                with final.open("rb") as prior:
                    shutil.copyfileobj(prior, out)
            with gzip.GzipFile(filename="", fileobj=out, mode="wb", mtime=0) as compressed:
                with part.open("rb") as incoming:
                    shutil.copyfileobj(incoming, compressed)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, final)
        sync_dir(final.parent)
        part.unlink()
        sync_dir(final.parent)

    def seal(self, now=None, alias=None, all_hours=False):
        """Wall-clock seal also runs while a container is completely silent."""
        now = now or utcnow()
        hour = now.astimezone(timezone.utc).strftime("%Y-%m-%d/%H")
        with self.lock:
            for name in ([alias] if alias else sorted(ALIASES)):
                folder = self._folder(name)
                for part in sorted(folder.glob("????-??-??/??.log.part")):
                    if all_hours or f"{part.parent.name}/{part.name[:2]}" < hour:
                        self._seal(part)

    def prune(self, now):
        cutoff = now.astimezone(timezone.utc) - timedelta(days=self.retention_days)
        removed = 0
        with self.lock:
            for alias in sorted(ALIASES):
                for path in self._folder(alias).glob("????-??-??/*"):
                    if not HOUR_FILE.fullmatch(path.name):
                        continue
                    try:
                        hour = datetime.strptime(path.parent.name + path.name[:2], "%Y-%m-%d%H").replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    if hour + timedelta(hours=1) <= cutoff:
                        path.unlink()
                        removed += 1
        return removed

    def archives(self):
        with self.lock:
            return sorted(self.root.glob("*/????-??-??/??.log.gz"))

    def snapshot(self, path):
        """Open an immutable inode; atomic seal may replace its pathname later."""
        with self.lock:
            try:
                stream = path.open("rb")
            except FileNotFoundError:
                return None
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            stream.seek(0)
            try:
                sent = path.with_suffix(".gz.sent").read_text()
            except FileNotFoundError:
                sent = ""
            if sent == digest:
                stream.close()
                return None
            return stream, digest

    def mark_uploaded(self, path, digest):
        with self.lock:
            if path.exists():
                atomic_write(path.with_suffix(".gz.sent"), digest.encode())
