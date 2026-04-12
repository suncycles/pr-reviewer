# pr_reviewer/llm/reviewer.py
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import anthropic

from pr_reviewer.github.diff_parser import FileDiff, DiffHunk
from pr_reviewer.github.client import PRInfo
from pr_reviewer.llm.prompts import PERSONAS


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ReviewComment:
    path: str
    diff_position: int
    severity: str
    body: str  # already formatted with [SEVERITY] prefix

    def to_github_payload(self) -> dict:
        return {
            "path": self.path,
            "position": self.diff_position,
            "body": self.body,
        }


@dataclass
class ReviewResult:
    summary: str
    comments: list[ReviewComment]
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# ── Window: a batch of files that fits in one API call ────────────────────────

@dataclass
class Window:
    pr_context: str           # PR title + description header
    prior_summary: str = ""   # one-liner summary of files in earlier windows
    files: list[FileDiff] = field(default_factory=list)

    def add_file(self, f: FileDiff) -> None:
        self.files.append(f)

    @property
    def token_estimate(self) -> int:
        base = len(self.pr_context.split()) + len(self.prior_summary.split())
        return int(base * 1.3) + sum(f.token_estimate for f in self.files)

    def to_prompt(self) -> str:
        parts = [self.pr_context]
        if self.prior_summary:
            parts.append(f"## Previously reviewed files (summary)\n{self.prior_summary}\n")
        parts.append("## Diff\n")
        for f in self.files:
            parts.append(f"### File: {f.path}")
            for hunk in f.hunks:
                parts.append(hunk.header)
                for line in hunk.lines:
                    parts.append(line.content)
        return "\n".join(parts)

    def line_to_diff_position(self, file_path: str, new_lineno: int) -> int | None:
        for f in self.files:
            if f.path == file_path:
                return f.line_to_diff_position(new_lineno)
        return None


# ── Context window builder ────────────────────────────────────────────────────

class ContextWindowBuilder:
    MAX_TOKENS = 150_000
    SYSTEM_OVERHEAD = 3_000
    RESPONSE_BUDGET = 4_096

    def __init__(self, model: str = "claude-opus-4-5") -> None:
        self.model = model
        self._available = self.MAX_TOKENS - self.SYSTEM_OVERHEAD - self.RESPONSE_BUDGET

    def build_windows(self, files: list[FileDiff], pr_info: PRInfo) -> list[Window]:
        pr_header = self._pr_header(pr_info)
        windows: list[Window] = []
        current = Window(pr_context=pr_header)

        for file in files:
            if not file.hunks:
                continue  # binary file or no changes to show

            if file.token_estimate > self._available:
                # Large single file: split at hunk boundaries
                hunk_windows = self._split_large_file(file, pr_header)
                if current.files:
                    windows.append(current)
                    current = Window(
                        pr_context=pr_header,
                        prior_summary=self._summarize(current.files),
                    )
                windows.extend(hunk_windows[:-1])
                # Put the last hunk-window's file into current so we can
                # potentially pack more files after it
                for hw in hunk_windows:
                    windows.append(hw)
                current = Window(
                    pr_context=pr_header,
                    prior_summary=self._summarize(
                        [f for w in windows for f in w.files]
                    ),
                )
                continue

            if current.token_estimate + file.token_estimate <= self._available:
                current.add_file(file)
            else:
                if current.files:
                    windows.append(current)
                current = Window(
                    pr_context=pr_header,
                    prior_summary=self._summarize(current.files),
                )
                current.add_file(file)

        if current.files:
            windows.append(current)

        return windows

    def _split_large_file(
        self, file: FileDiff, pr_header: str
    ) -> list[Window]:
        """Split a single oversized file across multiple windows at hunk boundaries."""
        windows: list[Window] = []
        current = Window(pr_context=pr_header)
        # Create a mini-file for each hunk
        for hunk in file.hunks:
            mini = FileDiff(
                path=file.path,
                old_path=file.old_path,
                status=file.status,
                hunks=[hunk],
            )
            if current.token_estimate + mini.token_estimate <= self._available:
                current.add_file(mini)
            else:
                if current.files:
                    windows.append(current)
                current = Window(pr_context=pr_header)
                current.add_file(mini)
        if current.files:
            windows.append(current)
        return windows

    def _pr_header(self, pr_info: PRInfo) -> str:
        body_preview = (pr_info.body or "")[:500]
        return (
            f"## PR: {pr_info.title}\n"
            f"Repository: {pr_info.owner}/{pr_info.repo}  PR #{pr_info.number}\n\n"
            f"{body_preview}\n"
        )

    def _summarize(self, files: list[FileDiff]) -> str:
        if not files:
            return ""
        parts = []
        for f in files:
            added = sum(
                1 for h in f.hunks for l in h.lines if l.change_type == "added"
            )
            removed = sum(
                1 for h in f.hunks for l in h.lines if l.change_type == "removed"
            )
            parts.append(f"{f.path} (+{added}/-{removed} lines)")
        return "Files already reviewed: " + ", ".join(parts)


# ── Main reviewer ─────────────────────────────────────────────────────────────

class Reviewer:
    def __init__(self, persona: str, model: str = "claude-opus-4-5") -> None:
        if persona not in PERSONAS:
            raise ValueError(f"Unknown persona {persona!r}. Choose from: {list(PERSONAS)}")
        self.persona = persona
        self.model = model
        self.client = anthropic.Anthropic()
        self.system_prompt = PERSONAS[persona]
        self.window_builder = ContextWindowBuilder(model=model)

    def review(
        self, files: list[FileDiff], pr_info: PRInfo
    ) -> ReviewResult:
        """Full review: build windows, call Claude for each, aggregate."""
        windows = self.window_builder.build_windows(files, pr_info)
        all_comments: list[ReviewComment] = []
        all_summaries: list[str] = []
        total_in = total_out = 0

        for i, window in enumerate(windows):
            result = self._review_window(window, window_num=i + 1, total=len(windows))
            all_comments.extend(result.comments)
            all_summaries.append(result.summary)
            total_in += result.input_tokens
            total_out += result.output_tokens

        combined_summary = (
            all_summaries[0]
            if len(all_summaries) == 1
            else "**Review across {} windows:**\n".format(len(all_summaries))
            + "\n\n".join(f"Window {i+1}: {s}" for i, s in enumerate(all_summaries))
        )

        return ReviewResult(
            summary=combined_summary,
            comments=all_comments,
            input_tokens=total_in,
            output_tokens=total_out,
        )

    def _review_window(
        self, window: Window, window_num: int = 1, total: int = 1
    ) -> ReviewResult:
        prompt = window.to_prompt()

        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=self.system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text
        return self._parse(raw, window, response.usage)

    def _parse(self, raw: str, window: Window, usage) -> ReviewResult:
        # Attempt direct JSON parse
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Claude occasionally adds a sentence or markdown fence
            # Strip ```json ... ``` fences first
            stripped = re.sub(r"```json\s*|\s*```", "", raw, flags=re.DOTALL).strip()
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                # Last-ditch: find the outermost {...}
                match = re.search(r"\{.*\}", stripped, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group())
                    except json.JSONDecodeError:
                        data = {"summary": raw[:500], "comments": []}
                else:
                    data = {"summary": raw[:500], "comments": []}

        comments: list[ReviewComment] = []
        for c in data.get("comments", []):
            file_path = c.get("file", "")
            line = c.get("line")
            if not file_path or line is None:
                continue

            try:
                line = int(line)
            except (TypeError, ValueError):
                continue

            diff_pos = window.line_to_diff_position(file_path, line)
            if diff_pos is None:
                # Claude hallucinated this line — drop it silently
                continue

            severity = c.get("severity", "medium").lower()
            if severity not in ("high", "medium", "low"):
                severity = "medium"

            body = f"**[{severity.upper()}]** {c.get('comment', '')}"
            comments.append(
                ReviewComment(
                    path=file_path,
                    diff_position=diff_pos,
                    severity=severity,
                    body=body,
                )
            )

        return ReviewResult(
            summary=data.get("summary", ""),
            comments=comments,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

    def estimate_tokens(self, files: list[FileDiff], pr_info: PRInfo) -> int:
        """Estimate total tokens without calling the API."""
        windows = self.window_builder.build_windows(files, pr_info)
        return sum(w.token_estimate for w in windows)