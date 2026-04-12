# pr_reviewer/github/diff_parser.py
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


@dataclass
class DiffLine:
    # Line number in the NEW file (None for pure deletions)
    new_lineno: int | None
    # Line number in the OLD file (None for pure additions)
    old_lineno: int | None
    # 1-based position counting from the first @@ of this file's diff
    diff_position: int
    change_type: str  # "added" | "removed" | "context"
    content: str      # raw content including leading +/-/space


@dataclass
class DiffHunk:
    header: str           # e.g. "@@ -10,6 +10,8 @@ def foo():"
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[DiffLine] = field(default_factory=list)

    def content_hash(self) -> str:
        """SHA-256 of the hunk content — used for incremental review tracking."""
        blob = self.header + "\n" + "\n".join(l.content for l in self.lines)
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class FileDiff:
    path: str           # current path (right side)
    old_path: str       # previous path (left side, different on renames)
    status: str         # "added" | "modified" | "deleted" | "renamed" | "unknown"
    hunks: list[DiffHunk] = field(default_factory=list)

    @property
    def token_estimate(self) -> int:
        """Rough token count for context-window budgeting (1.3 tokens/word)."""
        total_chars = sum(
            len(l.content) for h in self.hunks for l in h.lines
        )
        return int(total_chars / 4)  # ~4 chars per token is a common heuristic

    def line_to_diff_position(self, new_lineno: int) -> int | None:
        """
        Given a line number in the new file, return its diff position.
        Returns None if the line isn't in any hunk (Claude hallucinated it).
        """
        for hunk in self.hunks:
            for dl in hunk.lines:
                if dl.new_lineno == new_lineno:
                    return dl.diff_position
        return None


# ── State machine states ──────────────────────────────────────────────────────
_ST_START = "start"
_ST_DIFF = "diff"      # saw "diff --git"
_ST_HUNK = "hunk"      # inside a hunk

_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)"
)


def parse_diff(raw: str) -> list[FileDiff]:
    """
    Parse a unified diff string into a list of FileDiff objects.
    Uses a state machine; handles renames, additions, and deletions.
    """
    files: list[FileDiff] = []
    current_file: FileDiff | None = None
    current_hunk: DiffHunk | None = None

    # diff_position counts lines from the first @@ of the CURRENT file
    file_diff_position = 0

    state = _ST_START

    for raw_line in raw.splitlines():
        # ── New file diff header ──────────────────────────────────────────
        if raw_line.startswith("diff --git "):
            # Save previous hunk/file
            if current_hunk and current_file:
                current_file.hunks.append(current_hunk)
                current_hunk = None
            if current_file:
                files.append(current_file)

            # Parse paths from "diff --git a/foo.py b/foo.py"
            parts = raw_line.split(" ")
            # parts[2] = "a/old_path", parts[3] = "b/new_path"
            old_path = parts[2][2:] if len(parts) > 2 else ""
            new_path = parts[3][2:] if len(parts) > 3 else old_path
            current_file = FileDiff(
                path=new_path,
                old_path=old_path,
                status="modified",
            )
            file_diff_position = 0
            state = _ST_DIFF
            continue

        if state == _ST_START:
            continue

        # ── File metadata lines ───────────────────────────────────────────
        if raw_line.startswith("new file mode"):
            if current_file:
                current_file.status = "added"
            continue

        if raw_line.startswith("deleted file mode"):
            if current_file:
                current_file.status = "deleted"
            continue

        if raw_line.startswith("rename from "):
            if current_file:
                current_file.old_path = raw_line[len("rename from "):]
                current_file.status = "renamed"
            continue

        if raw_line.startswith("rename to "):
            if current_file:
                current_file.path = raw_line[len("rename to "):]
            continue

        if raw_line.startswith("--- "):
            # e.g. "--- a/src/foo.py" or "--- /dev/null"
            continue

        if raw_line.startswith("+++ "):
            # e.g. "+++ b/src/foo.py"
            # Override path with the +++ line (more reliable than diff --git for renames)
            path_part = raw_line[4:]
            if path_part.startswith("b/"):
                path_part = path_part[2:]
            if current_file and path_part != "/dev/null":
                current_file.path = path_part
            continue

        if raw_line.startswith("index ") or raw_line.startswith("Binary "):
            continue

        # ── Hunk header ───────────────────────────────────────────────────
        hunk_match = _HUNK_RE.match(raw_line)
        if hunk_match and current_file:
            # Save previous hunk
            if current_hunk:
                current_file.hunks.append(current_hunk)

            old_start = int(hunk_match.group(1))
            old_count = int(hunk_match.group(2) or "1")
            new_start = int(hunk_match.group(3))
            new_count = int(hunk_match.group(4) or "1")

            # The @@ line itself counts as diff position 1 of this hunk
            # (GitHub's API counts from 1 per-file, not per-hunk)
            file_diff_position += 1

            current_hunk = DiffHunk(
                header=raw_line,
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
            )

            # Track line counters inside the hunk
            current_hunk._old_lineno = old_start  # type: ignore[attr-defined]
            current_hunk._new_lineno = new_start  # type: ignore[attr-defined]
            state = _ST_HUNK
            continue

        # ── Hunk body lines ───────────────────────────────────────────────
        if state == _ST_HUNK and current_hunk and current_file:
            if raw_line.startswith("+"):
                file_diff_position += 1
                dl = DiffLine(
                    new_lineno=current_hunk._new_lineno,  # type: ignore[attr-defined]
                    old_lineno=None,
                    diff_position=file_diff_position,
                    change_type="added",
                    content=raw_line,
                )
                current_hunk._new_lineno += 1  # type: ignore[attr-defined]
                current_hunk.lines.append(dl)

            elif raw_line.startswith("-"):
                file_diff_position += 1
                dl = DiffLine(
                    new_lineno=None,
                    old_lineno=current_hunk._old_lineno,  # type: ignore[attr-defined]
                    diff_position=file_diff_position,
                    change_type="removed",
                    content=raw_line,
                )
                current_hunk._old_lineno += 1  # type: ignore[attr-defined]
                current_hunk.lines.append(dl)

            elif raw_line.startswith(" ") or raw_line == "":
                # Context line (space prefix or blank)
                file_diff_position += 1
                dl = DiffLine(
                    new_lineno=current_hunk._new_lineno,  # type: ignore[attr-defined]
                    old_lineno=current_hunk._old_lineno,  # type: ignore[attr-defined]
                    diff_position=file_diff_position,
                    change_type="context",
                    content=raw_line,
                )
                current_hunk._old_lineno += 1  # type: ignore[attr-defined]
                current_hunk._new_lineno += 1  # type: ignore[attr-defined]
                current_hunk.lines.append(dl)
            # "\ No newline at end of file" and similar — skip
            continue

    # Flush the last hunk/file
    if current_hunk and current_file:
        current_file.hunks.append(current_hunk)
    if current_file:
        files.append(current_file)

    return files