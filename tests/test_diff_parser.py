# tests/test_diff_parser.py
import textwrap
from pr_reviewer.github.diff_parser import parse_diff, DiffLine


SIMPLE_DIFF = textwrap.dedent("""\
    diff --git a/src/auth.py b/src/auth.py
    index abc1234..def5678 100644
    --- a/src/auth.py
    +++ b/src/auth.py
    @@ -10,6 +10,8 @@ def authenticate(user):
         if not user:
             raise ValueError("no user")
    +    token = secrets.token_urlsafe(32)
    +    session["token"] = token
         return True
     
     def logout():
""")

RENAME_DIFF = textwrap.dedent("""\
    diff --git a/old_name.py b/new_name.py
    similarity index 90%
    rename from old_name.py
    rename to new_name.py
    index abc1234..def5678 100644
    --- a/old_name.py
    +++ b/new_name.py
    @@ -1,3 +1,3 @@
    -# old module
    +# new module
     x = 1
     y = 2
""")

NEW_FILE_DIFF = textwrap.dedent("""\
    diff --git a/new_file.py b/new_file.py
    new file mode 100644
    index 0000000..abc1234
    --- /dev/null
    +++ b/new_file.py
    @@ -0,0 +1,3 @@
    +def hello():
    +    return "world"
    +
""")


def test_parse_simple_diff():
    files = parse_diff(SIMPLE_DIFF)
    assert len(files) == 1

    f = files[0]
    assert f.path == "src/auth.py"
    assert f.status == "modified"
    assert len(f.hunks) == 1

    hunk = f.hunks[0]
    assert hunk.new_start == 10

    added = [l for l in hunk.lines if l.change_type == "added"]
    assert len(added) == 2
    assert "secrets.token_urlsafe" in added[0].content
    assert 'session["token"]' in added[1].content


def test_diff_positions_are_sequential():
    files = parse_diff(SIMPLE_DIFF)
    hunk = files[0].hunks[0]
    positions = [l.diff_position for l in hunk.lines]
    # Positions must be strictly increasing
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions), "Positions must be unique"


def test_added_lines_have_new_lineno():
    files = parse_diff(SIMPLE_DIFF)
    hunk = files[0].hunks[0]
    for line in hunk.lines:
        if line.change_type == "added":
            assert line.new_lineno is not None
            assert line.old_lineno is None
        elif line.change_type == "removed":
            assert line.old_lineno is not None
            assert line.new_lineno is None
        else:  # context
            assert line.new_lineno is not None
            assert line.old_lineno is not None


def test_line_to_diff_position():
    files = parse_diff(SIMPLE_DIFF)
    f = files[0]
    hunk = f.hunks[0]

    added = [l for l in hunk.lines if l.change_type == "added"]
    first_added = added[0]

    pos = f.line_to_diff_position(first_added.new_lineno)
    assert pos == first_added.diff_position


def test_line_to_diff_position_invalid():
    files = parse_diff(SIMPLE_DIFF)
    assert files[0].line_to_diff_position(99999) is None


def test_parse_rename():
    files = parse_diff(RENAME_DIFF)
    assert len(files) == 1
    f = files[0]
    assert f.status == "renamed"
    assert f.path == "new_name.py"
    assert f.old_path == "old_name.py"


def test_parse_new_file():
    files = parse_diff(NEW_FILE_DIFF)
    assert len(files) == 1
    f = files[0]
    assert f.status == "added"
    assert f.path == "new_file.py"
    added = [l for l in f.hunks[0].lines if l.change_type == "added"]
    assert len(added) == 3


def test_hunk_hash_stability():
    """Same hunk content must produce the same hash."""
    files = parse_diff(SIMPLE_DIFF)
    h1 = files[0].hunks[0].content_hash()
    files2 = parse_diff(SIMPLE_DIFF)
    h2 = files2[0].hunks[0].content_hash()
    assert h1 == h2


def test_hunk_hash_differs_on_change():
    diff2 = SIMPLE_DIFF.replace("secrets.token_urlsafe(32)", "random.random()")
    files1 = parse_diff(SIMPLE_DIFF)
    files2 = parse_diff(diff2)
    assert files1[0].hunks[0].content_hash() != files2[0].hunks[0].content_hash()


def test_sample_diff_file():
    """Parse the real-world sample.diff fixture without crashing."""
    import pathlib
    sample = pathlib.Path(__file__).parent / "fixtures" / "sample.diff"
    if not sample.exists():
        import pytest
        pytest.skip("fixtures/sample.diff not present")
    files = parse_diff(sample.read_text(errors="replace"))
    assert len(files) >= 1
    for f in files:
        for hunk in f.hunks:
            positions = [l.diff_position for l in hunk.lines]
            assert positions == sorted(positions), f"Non-monotonic positions in {f.path}"