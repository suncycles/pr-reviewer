# pr_reviewer/storage/db.py
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Generator

from pr_reviewer.github.diff_parser import DiffHunk


SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY,
    owner       TEXT NOT NULL,
    repo        TEXT NOT NULL,
    pr_number   INTEGER NOT NULL,
    head_sha    TEXT NOT NULL,
    persona     TEXT NOT NULL,
    reviewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    UNIQUE(owner, repo, pr_number, head_sha, persona)
);

CREATE TABLE IF NOT EXISTS reviewed_hunks (
    id          INTEGER PRIMARY KEY,
    review_id   INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    file_path   TEXT NOT NULL,
    hunk_hash   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posted_comments (
    id              INTEGER PRIMARY KEY,
    review_id       INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    file_path       TEXT NOT NULL,
    diff_position   INTEGER NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'medium',
    body            TEXT NOT NULL
);
"""


@dataclass
class ReviewRecord:
    id: int
    owner: str
    repo: str
    pr_number: int
    head_sha: str
    persona: str
    reviewed_at: str
    input_tokens: int
    output_tokens: int


class ReviewDB:
    def __init__(self, db_path: str = "pr_reviewer.db") -> None:
        self.db_path = str(Path(db_path).expanduser())
        self._init_db()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ── Reviews ───────────────────────────────────────────────────────────────

    def get_last_review(
        self, owner: str, repo: str, pr_number: int, persona: str
    ) -> ReviewRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM reviews
                WHERE owner=? AND repo=? AND pr_number=? AND persona=?
                ORDER BY reviewed_at DESC LIMIT 1
                """,
                (owner, repo, pr_number, persona),
            ).fetchone()
        if row is None:
            return None
        return ReviewRecord(**dict(row))

    def create_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        persona: str,
    ) -> int:
        """Insert a review row and return its id."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO reviews (owner, repo, pr_number, head_sha, persona)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner, repo, pr_number, head_sha, persona)
                    DO UPDATE SET reviewed_at=CURRENT_TIMESTAMP
                RETURNING id
                """,
                (owner, repo, pr_number, head_sha, persona),
            )
            return cur.fetchone()[0]

    def update_token_usage(
        self, review_id: int, input_tokens: int, output_tokens: int
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE reviews
                SET input_tokens = input_tokens + ?,
                    output_tokens = output_tokens + ?
                WHERE id = ?
                """,
                (input_tokens, output_tokens, review_id),
            )

    def list_reviews(
        self, owner: str, repo: str, pr_number: int
    ) -> list[ReviewRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM reviews
                WHERE owner=? AND repo=? AND pr_number=?
                ORDER BY reviewed_at DESC
                """,
                (owner, repo, pr_number),
            ).fetchall()
        return [ReviewRecord(**dict(r)) for r in rows]

    # ── Hunks ─────────────────────────────────────────────────────────────────

    def save_hunk_hashes(
        self, review_id: int, file_path: str, hunks: list[DiffHunk]
    ) -> None:
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO reviewed_hunks (review_id, file_path, hunk_hash) VALUES (?, ?, ?)",
                [(review_id, file_path, h.content_hash()) for h in hunks],
            )

    def get_reviewed_hunk_hashes(self, review_id: int) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT hunk_hash FROM reviewed_hunks WHERE review_id=?",
                (review_id,),
            ).fetchall()
        return {r["hunk_hash"] for r in rows}

    def get_unreviewed_hunks(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        persona: str,
        file_path: str,
        current_hunks: list[DiffHunk],
    ) -> list[DiffHunk]:
        """Return only hunks not seen in the most recent review of this PR."""
        last = self.get_last_review(owner, repo, pr_number, persona)
        if last is None:
            return current_hunks
        seen = self.get_reviewed_hunk_hashes(last.id)
        return [h for h in current_hunks if h.content_hash() not in seen]

    # ── Comments ──────────────────────────────────────────────────────────────

    def save_comments(
        self,
        review_id: int,
        comments: list[dict],
    ) -> None:
        """comments: list of dicts with file_path, diff_position, severity, body."""
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO posted_comments
                    (review_id, file_path, diff_position, severity, body)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        review_id,
                        c["file_path"],
                        c["diff_position"],
                        c.get("severity", "medium"),
                        c["body"],
                    )
                    for c in comments
                ],
            )

    def get_comments_for_review(self, review_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM posted_comments WHERE review_id=?",
                (review_id,),
            ).fetchall()
        return [dict(r) for r in rows]