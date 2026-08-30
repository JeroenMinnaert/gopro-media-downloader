"""SQLite manifest: the source of truth for what exists, what is done, what failed.

Two tables, because one media id can map to several physical files: a chaptered
recording fans out into GX01.../GX02..., and that fan-out is only discoverable
when /media/{id}/download is called. So work is *queued* per item (resolve then
fetch its chapters) but *state* is tracked per file.

Concurrency: one connection behind a lock, WAL mode, short transactions. With a
handful of workers, contention is a non-issue and workers can read without a
queue round-trip.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"

# Item states:  pending -> resolving -> resolved -> done
#               (also: failed, skipped)
# File states:  pending -> downloading -> done  (also: failed, skipped)
SCHEMA = """
CREATE TABLE IF NOT EXISTS media_items (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  filename TEXT,
  captured_at TEXT NOT NULL,
  captured_at_timezone TEXT,
  file_size INTEGER,
  item_count INTEGER,
  camera_model TEXT,
  date_folder TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  skip_reason TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  first_seen_at TEXT,
  last_synced_at TEXT,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS media_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  media_id TEXT NOT NULL REFERENCES media_items(id),
  item_number INTEGER NOT NULL DEFAULT 1,
  filename TEXT NOT NULL,
  expected_size INTEGER,
  actual_size INTEGER,
  size_unverified INTEGER NOT NULL DEFAULT 0,
  checksum TEXT,
  checksum_algo TEXT,
  checksum_state TEXT,
  target_path TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  last_attempt_at TEXT,
  completed_at TEXT,
  UNIQUE(media_id, item_number)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE INDEX IF NOT EXISTS idx_files_state ON media_files(state);
CREATE INDEX IF NOT EXISTS idx_items_state ON media_items(state);
CREATE INDEX IF NOT EXISTS idx_items_date ON media_items(date_folder);
"""

ACTIVE_ITEM_STATES = ("pending", "resolved", "failed")
DONE_FILE_STATES = ("done", "skipped")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Manifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        self._migrate()
        self.set_meta("schema_version", SCHEMA_VERSION)

    def _migrate(self) -> None:
        """Add columns introduced after a manifest was first created."""
        with self._lock:
            existing = {
                row["name"] for row in self.conn.execute("PRAGMA table_info(media_files)")
            }
            if "checksum_state" not in existing:
                self.conn.execute("ALTER TABLE media_files ADD COLUMN checksum_state TEXT")
                self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> Manifest:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- meta --------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            self.conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def quick_check(self) -> bool:
        with self._lock:
            row = self.conn.execute("PRAGMA quick_check").fetchone()
        return bool(row) and row[0] == "ok"

    # -- items -------------------------------------------------------------

    def upsert_item(self, item, date_folder: str, skip_reason: str | None = None) -> None:
        """Insert or refresh item metadata. Never disturbs download progress."""
        now = _now()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO media_items (
                  id, type, filename, captured_at, captured_at_timezone, file_size,
                  item_count, camera_model, date_folder, state, skip_reason,
                  first_seen_at, last_synced_at, raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  type=excluded.type,
                  filename=excluded.filename,
                  captured_at=excluded.captured_at,
                  captured_at_timezone=excluded.captured_at_timezone,
                  file_size=excluded.file_size,
                  item_count=excluded.item_count,
                  camera_model=excluded.camera_model,
                  last_synced_at=excluded.last_synced_at,
                  raw_json=excluded.raw_json,
                  skip_reason=excluded.skip_reason,
                  -- a finished item is never resurrected by a re-sync
                  state=CASE
                    WHEN media_items.state='done' THEN 'done'
                    WHEN excluded.skip_reason IS NOT NULL THEN 'skipped'
                    WHEN media_items.state='skipped' AND excluded.skip_reason IS NULL THEN 'pending'
                    ELSE media_items.state
                  END,
                  -- date_folder is frozen once files have been placed on disk
                  date_folder=CASE
                    WHEN media_items.state IN ('done','resolved') THEN media_items.date_folder
                    ELSE excluded.date_folder
                  END
                """,
                (
                    item.id,
                    item.type,
                    item.filename,
                    item.captured_at,
                    item.captured_at_timezone,
                    item.file_size,
                    item.item_count,
                    item.camera_model,
                    date_folder,
                    "skipped" if skip_reason else "pending",
                    skip_reason,
                    now,
                    now,
                    item.raw_json,
                ),
            )
            self.conn.commit()

    def get_item(self, media_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute("SELECT * FROM media_items WHERE id=?", (media_id,)).fetchone()

    def set_item_state(self, media_id: str, state: str, skip_reason: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE media_items SET state=?, skip_reason=COALESCE(?, skip_reason) WHERE id=?",
                (state, skip_reason, media_id),
            )
            self.conn.commit()

    def claim_item(self, media_id: str) -> bool:
        """Atomically move an item to 'resolving'. False if another worker won."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE media_items SET state='resolving', attempts=attempts+1 "
                "WHERE id=? AND state IN ('pending','resolved','failed')",
                (media_id,),
            )
            self.conn.commit()
            return cur.rowcount == 1

    def mark_item_failed(self, media_id: str, error: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE media_items SET state='failed', last_error=? WHERE id=?",
                (error[:500], media_id),
            )
            self.conn.commit()

    def release_item(self, media_id: str, charge_attempt: bool = True) -> None:
        """Return an item to the queue unblamed (shutdown, breaker, auth pause)."""
        with self._lock:
            if charge_attempt:
                self.conn.execute(
                    "UPDATE media_items SET state='pending' WHERE id=? AND state='resolving'",
                    (media_id,),
                )
            else:
                self.conn.execute(
                    "UPDATE media_items SET state='pending', attempts=MAX(attempts-1,0) "
                    "WHERE id=? AND state='resolving'",
                    (media_id,),
                )
            self.conn.commit()

    def refresh_item_state(self, media_id: str) -> str:
        """Derive an item's state from its files. Returns the resulting state."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN state IN ('done','skipped') THEN 1 ELSE 0 END) AS finished, "
                "SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed "
                "FROM media_files WHERE media_id=?",
                (media_id,),
            ).fetchone()
            total, finished, failed = row["total"], row["finished"] or 0, row["failed"] or 0
            if total and finished == total:
                state = "done"
            elif failed:
                state = "failed"
            else:
                state = "resolved"
            self.conn.execute(
                "UPDATE media_items SET state=? WHERE id=? AND state != 'skipped'",
                (state, media_id),
            )
            self.conn.commit()
        return state

    def pending_items(
        self,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = None,
        include_failed: bool = True,
        max_attempts: int = 4,
        types: tuple[str, ...] | None = None,
        columns: str = "i.*",
    ) -> list[sqlite3.Row]:
        """Items with outstanding work, oldest capture first."""
        states = ["pending", "resolved"] + (["failed"] if include_failed else [])
        sql = f"""
            SELECT {columns} FROM media_items i
            WHERE i.skip_reason IS NULL
              AND i.state IN ({','.join('?' * len(states))})
              AND i.attempts < ?
              AND (
                i.state = 'pending'
                -- never resolved (e.g. the download endpoint failed): there are
                -- no file rows to point at, but there is still work to do
                OR NOT EXISTS (SELECT 1 FROM media_files f WHERE f.media_id = i.id)
                OR EXISTS (
                  SELECT 1 FROM media_files f
                  WHERE f.media_id = i.id AND f.state NOT IN ('done','skipped')
                )
              )
        """
        params: list[Any] = [*states, max_attempts]
        if types:
            sql += f" AND i.type IN ({','.join('?' * len(types))})"
            params.extend(types)
        if since:
            sql += " AND i.date_folder >= ?"
            params.append(since)
        if until:
            sql += " AND i.date_folder <= ?"
            params.append(until)
        sql += " ORDER BY i.captured_at ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    # -- files -------------------------------------------------------------

    def path_owner(
        self,
        relpath: str,
        exclude_media_id: str | None = None,
        exclude_item_number: int | None = None,
    ) -> str | None:
        """Which media item claims this path, or None if it is free.

        A file's own row is excluded so that re-resolving an item (to refresh
        its signed URLs) does not see itself as a collision.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT media_id, item_number FROM media_files WHERE target_path=?", (relpath,)
            ).fetchone()
        if row is None:
            return None
        if (
            exclude_media_id is not None
            and row["media_id"] == exclude_media_id
            and (exclude_item_number is None or row["item_number"] == exclude_item_number)
        ):
            return None
        return row["media_id"]

    def path_taken(self, relpath: str, exclude_media_id: str | None = None) -> bool:
        return self.path_owner(relpath, exclude_media_id) is not None

    def get_file(self, media_id: str, item_number: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM media_files WHERE media_id=? AND item_number=?",
                (media_id, item_number),
            ).fetchone()

    def files_for(self, media_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM media_files WHERE media_id=? ORDER BY item_number",
                    (media_id,),
                ).fetchall()
            )

    def upsert_file(
        self,
        media_id: str,
        item_number: int,
        filename: str,
        target_path: str,
        expected_size: int | None,
        checksum: str | None = None,
        checksum_algo: str | None = None,
    ) -> int:
        """Create or refresh a file row; returns its id.

        target_path is immutable once assigned -- resume depends on a given file
        always landing in the same place.
        """
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO media_files (
                  media_id, item_number, filename, expected_size, checksum,
                  checksum_algo, target_path, state
                ) VALUES (?,?,?,?,?,?,?, 'pending')
                ON CONFLICT(media_id, item_number) DO UPDATE SET
                  filename=excluded.filename,
                  expected_size=COALESCE(excluded.expected_size, media_files.expected_size),
                  checksum=COALESCE(excluded.checksum, media_files.checksum),
                  checksum_algo=COALESCE(excluded.checksum_algo, media_files.checksum_algo)
                """,
                (media_id, item_number, filename, expected_size, checksum, checksum_algo, target_path),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT id FROM media_files WHERE media_id=? AND item_number=?",
                (media_id, item_number),
            ).fetchone()
        return int(row["id"])

    def claim_file(self, file_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE media_files SET state='downloading', attempts=attempts+1, "
                "last_attempt_at=? WHERE id=? AND state IN ('pending','failed')",
                (_now(), file_id),
            )
            self.conn.commit()
            return cur.rowcount == 1

    def mark_done(
        self,
        file_id: int,
        actual_size: int,
        size_unverified: bool = False,
        checksum: str | None = None,
        checksum_algo: str | None = None,
        checksum_state: str | None = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE media_files SET state='done', actual_size=?, size_unverified=?, "
                "checksum=COALESCE(?, checksum), checksum_algo=COALESCE(?, checksum_algo), "
                "checksum_state=COALESCE(?, checksum_state), completed_at=?, "
                "last_error=NULL WHERE id=?",
                (
                    actual_size, int(size_unverified), checksum, checksum_algo,
                    checksum_state, _now(), file_id,
                ),
            )
            self.conn.commit()

    def files_needing_checksum(self, limit: int | None = None) -> list[sqlite3.Row]:
        """Done files with no origin hash recorded (downloaded before checksums,
        or resumed so they could not be hashed while streaming)."""
        sql = """
            SELECT f.*, i.raw_json, i.filename AS item_filename
            FROM media_files f JOIN media_items i ON i.id = f.media_id
            WHERE f.state = 'done' AND (f.checksum IS NULL OR f.checksum = '')
            ORDER BY f.media_id, f.item_number
        """
        params: list[Any] = []
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    def set_checksum(
        self, file_id: int, checksum: str, algo: str = "s3-etag", state: str | None = None
    ) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE media_files SET checksum=?, checksum_algo=?, "
                "checksum_state=COALESCE(?, checksum_state) WHERE id=?",
                (checksum, algo, state, file_id),
            )
            self.conn.commit()

    def checksum_summary(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT COALESCE(checksum_state,'not_checked') AS s, COUNT(*) n "
                "FROM media_files WHERE state='done' GROUP BY s"
            ).fetchall()
        return {r["s"]: r["n"] for r in rows}

    def mark_failed(self, file_id: int, error: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE media_files SET state='failed', last_error=? WHERE id=?",
                (error[:500], file_id),
            )
            self.conn.commit()

    def mark_skipped(self, file_id: int, reason: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE media_files SET state='skipped', last_error=? WHERE id=?",
                (reason[:500], file_id),
            )
            self.conn.commit()

    def mark_pending(self, file_id: int, charge_attempt: bool = True) -> None:
        with self._lock:
            if charge_attempt:
                self.conn.execute(
                    "UPDATE media_files SET state='pending' WHERE id=? AND state='downloading'",
                    (file_id,),
                )
            else:
                self.conn.execute(
                    "UPDATE media_files SET state='pending', attempts=MAX(attempts-1,0) "
                    "WHERE id=? AND state='downloading'",
                    (file_id,),
                )
            self.conn.commit()

    def reset_file(self, file_id: int) -> None:
        """Force a file back into the queue regardless of its current state."""
        with self._lock:
            self.conn.execute(
                "UPDATE media_files SET state='pending', last_error=NULL, "
                "actual_size=NULL, completed_at=NULL WHERE id=?",
                (file_id,),
            )
            self.conn.commit()

    # -- maintenance -------------------------------------------------------

    def reset_stale(self) -> tuple[int, int]:
        """Crash leftovers go back in the queue. Their .part files make it cheap."""
        with self._lock:
            files = self.conn.execute(
                "UPDATE media_files SET state='pending' WHERE state='downloading'"
            ).rowcount
            items = self.conn.execute(
                "UPDATE media_items SET state='pending' WHERE state='resolving'"
            ).rowcount
            self.conn.commit()
        return items, files

    def reset_failed(self) -> tuple[int, int]:
        with self._lock:
            files = self.conn.execute(
                "UPDATE media_files SET state='pending', attempts=0, last_error=NULL "
                "WHERE state='failed'"
            ).rowcount
            items = self.conn.execute(
                "UPDATE media_items SET state=CASE WHEN EXISTS "
                "(SELECT 1 FROM media_files f WHERE f.media_id=media_items.id) "
                "THEN 'resolved' ELSE 'pending' END, attempts=0, last_error=NULL "
                "WHERE state='failed'"
            ).rowcount
            self.conn.commit()
        return items, files

    # -- reporting ---------------------------------------------------------

    def counts(self) -> dict[str, dict[str, dict[str, int]]]:
        with self._lock:
            files = self.conn.execute(
                "SELECT state, COUNT(*) AS n, "
                "COALESCE(SUM(COALESCE(actual_size, expected_size)), 0) AS bytes "
                "FROM media_files GROUP BY state"
            ).fetchall()
            items = self.conn.execute(
                "SELECT state, COUNT(*) AS n, COALESCE(SUM(file_size),0) AS bytes "
                "FROM media_items GROUP BY state"
            ).fetchall()
        return {
            "files": {r["state"]: {"n": r["n"], "bytes": r["bytes"]} for r in files},
            "items": {r["state"]: {"n": r["n"], "bytes": r["bytes"]} for r in items},
        }

    def remaining_bytes(self, since: str | None = None, until: str | None = None) -> int:
        """Bytes still to fetch.

        Resolved items contribute their files' expected sizes; unresolved ones
        fall back to the listing's size hint, so the disk-space check is not
        fooled by a manifest that has only just been built.
        """
        params: list[Any] = []
        date_clause = ""
        if since:
            date_clause += " AND i.date_folder >= ?"
            params.append(since)
        if until:
            date_clause += " AND i.date_folder <= ?"
            params.append(until)
        with self._lock:
            resolved = self.conn.execute(
                "SELECT COALESCE(SUM(f.expected_size),0) AS b FROM media_files f "
                "JOIN media_items i ON i.id=f.media_id "
                "WHERE f.state NOT IN ('done','skipped') AND i.skip_reason IS NULL"
                + date_clause,
                params,
            ).fetchone()["b"]
            unresolved = self.conn.execute(
                "SELECT COALESCE(SUM(i.file_size),0) AS b FROM media_items i "
                "WHERE i.state='pending' AND i.skip_reason IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM media_files f WHERE f.media_id=i.id)"
                + date_clause,
                params,
            ).fetchone()["b"]
        return int(resolved or 0) + int(unresolved or 0)

    def bytes_for_items(self, media_ids: list[str]) -> int:
        """Outstanding bytes for a specific selection of items.

        Used for the disk-space check so that `--limit` / `--since` runs are
        measured against what they will actually fetch, not the whole library.
        """
        total = 0
        for start in range(0, len(media_ids), 400):  # stay under SQLite's param cap
            chunk = media_ids[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            with self._lock:
                row = self.conn.execute(
                    f"""
                    SELECT COALESCE(SUM(size), 0) AS total FROM (
                      SELECT CASE
                        WHEN EXISTS (SELECT 1 FROM media_files f WHERE f.media_id = i.id)
                        THEN (
                          SELECT COALESCE(SUM(f.expected_size), 0) FROM media_files f
                          WHERE f.media_id = i.id AND f.state NOT IN ('done','skipped')
                        )
                        ELSE COALESCE(i.file_size, 0)
                      END AS size
                      FROM media_items i WHERE i.id IN ({placeholders})
                    )
                    """,
                    chunk,
                ).fetchone()
            total += int(row["total"] or 0)
        return total

    def item_byte_total(self, media_id: str) -> tuple[int, int | None]:
        """(bytes on disk across the item's files, listing size) for cross-check."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(actual_size),0) AS total FROM media_files "
                "WHERE media_id=? AND state='done'",
                (media_id,),
            ).fetchone()
            item = self.conn.execute(
                "SELECT file_size FROM media_items WHERE id=?", (media_id,)
            ).fetchone()
        return int(row["total"] or 0), (item["file_size"] if item else None)

    def failures(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT f.*, i.date_folder FROM media_files f "
            "JOIN media_items i ON i.id=f.media_id WHERE f.state='failed' "
            "ORDER BY f.last_attempt_at DESC"
        )
        params: list[Any] = []
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = list(self.conn.execute(sql, params).fetchall())
        return rows

    def failed_items(self) -> list[sqlite3.Row]:
        """Items that failed before producing any file rows (resolve failures)."""
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM media_items WHERE state='failed' AND NOT EXISTS "
                    "(SELECT 1 FROM media_files f WHERE f.media_id=media_items.id)"
                ).fetchall()
            )

    def all_files(self, states: Iterable[str] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT f.*, i.date_folder FROM media_files f JOIN media_items i ON i.id=f.media_id"
        params: list[Any] = []
        if states:
            states = list(states)
            sql += f" WHERE f.state IN ({','.join('?' * len(states))})"
            params.extend(states)
        sql += " ORDER BY i.captured_at ASC, f.item_number ASC"
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    def skipped_items(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT id, filename, type, skip_reason FROM media_items WHERE state='skipped'"
                ).fetchall()
            )

    def date_folders(self) -> int:
        with self._lock:
            return int(
                self.conn.execute(
                    "SELECT COUNT(DISTINCT date_folder) AS n FROM media_items WHERE state='done'"
                ).fetchone()["n"]
            )
