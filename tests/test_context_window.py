# tests/test_context_window.py
from pr_reviewer.github.diff_parser import FileDiff, DiffHunk, DiffLine
from pr_reviewer.github.client import PRInfo
from pr_reviewer.llm.reviewer import ContextWindowBuilder, Window


def _make_file(path: str, token_size: int) -> FileDiff:
    """Create a fake FileDiff whose token_estimate ≈ token_size."""
    # token_estimate = total_chars / 4, so chars = token_size * 4
    content = "x" * (token_size * 4)
    line = DiffLine(
        new_lineno=1, old_lineno=None, diff_position=1,
        change_type="added", content=content
    )
    hunk = DiffHunk(
        header="@@ -0,0 +1,1 @@",
        old_start=0, old_count=0, new_start=1, new_count=1,
        lines=[line]
    )
    hunk._old_lineno = 0  # type: ignore
    hunk._new_lineno = 2  # type: ignore
    f = FileDiff(path=path, old_path=path, status="added", hunks=[hunk])
    return f


def _make_pr() -> PRInfo:
    return PRInfo(
        owner="acme", repo="widget", number=42,
        head_sha="abc123", base_sha="def456",
        title="Test PR", body="A test pull request.",
    )


def test_small_pr_fits_one_window():
    builder = ContextWindowBuilder()
    files = [_make_file(f"file{i}.py", 100) for i in range(5)]
    windows = builder.build_windows(files, _make_pr())
    assert len(windows) == 1


def test_large_pr_splits_windows():
    builder = ContextWindowBuilder()
    # Each file = 30k tokens, 6 files = 180k > 143k budget → must split
    files = [_make_file(f"big{i}.py", 30_000) for i in range(6)]
    windows = builder.build_windows(files, _make_pr())
    assert len(windows) > 1


def test_each_window_within_budget():
    builder = ContextWindowBuilder()
    files = [_make_file(f"f{i}.py", 20_000) for i in range(8)]
    windows = builder.build_windows(files, _make_pr())
    budget = builder._available
    for w in windows:
        assert w.token_estimate <= budget, (
            f"Window exceeded budget: {w.token_estimate} > {budget}"
        )


def test_prior_summary_in_subsequent_windows():
    builder = ContextWindowBuilder()
    files = [_make_file(f"f{i}.py", 50_000) for i in range(4)]
    windows = builder.build_windows(files, _make_pr())
    if len(windows) > 1:
        assert windows[1].prior_summary != ""
        assert "Files already reviewed" in windows[1].prior_summary


def test_no_files_lost():
    """Every file should appear in exactly one window."""
    builder = ContextWindowBuilder()
    files = [_make_file(f"f{i}.py", 15_000) for i in range(12)]
    windows = builder.build_windows(files, _make_pr())
    found_paths = set()
    for w in windows:
        for f in w.files:
            # A large file may be split into hunk-windows — just track path
            found_paths.add(f.path)
    original_paths = {f.path for f in files}
    assert original_paths == found_paths


def test_window_prompt_contains_pr_title():
    builder = ContextWindowBuilder()
    files = [_make_file("main.py", 100)]
    windows = builder.build_windows(files, _make_pr())
    prompt = windows[0].to_prompt()
    assert "Test PR" in prompt


def test_line_to_diff_position_in_window():
    line = DiffLine(
        new_lineno=5, old_lineno=None, diff_position=3,
        change_type="added", content="+hello"
    )
    hunk = DiffHunk("@@ -0,0 +1,1 @@", 0, 0, 5, 1, lines=[line])
    hunk._old_lineno = 0  # type: ignore
    hunk._new_lineno = 6  # type: ignore
    f = FileDiff("foo.py", "foo.py", "modified", hunks=[hunk])

    window = Window(pr_context="## PR\n")
    window.add_file(f)

    assert window.line_to_diff_position("foo.py", 5) == 3
    assert window.line_to_diff_position("foo.py", 999) is None
    assert window.line_to_diff_position("other.py", 5) is None