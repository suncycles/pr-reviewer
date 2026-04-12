# pr-reviewer

An AI-powered GitHub pull request reviewer. Fetches a PR diff, sends it to Claude with a structured prompt, and posts the resulting review back as inline GitHub comments — attached to exact file paths and line numbers, just like a human reviewer would leave them.

A SQLite database tracks review history so re-reviews on force-pushed commits only process changed hunks, cutting API costs significantly.

## Features

- **Three review personas** — security, performance, or style/correctness
- **Inline GitHub comments** — posted to exact diff positions, not just the PR thread
- **Incremental re-reviews** — hunk-level diffing skips unchanged code on re-runs
- **Context windowing** — handles PRs larger than the model's context window by splitting at hunk boundaries and carrying forward summaries
- **Dry-run mode** — preview exactly what would be posted before touching GitHub
- **Cost estimation** — see token counts and dollar estimates before running
- **Review history** — SQLite log of every review with token usage and cost

## Quickstart

```bash
git clone https://github.com/yourname/pr-reviewer
cd pr-reviewer
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Create a `.env` file in the project root:

```
GITHUB_TOKEN=ghp_your_token_here
ANTHROPIC_API_KEY=sk-ant-your_key_here
```

Run a review:

```bash
pr-review review https://github.com/owner/repo/pull/123
```

## Usage

```
pr-review review <pr_url> [OPTIONS]
pr-review history <pr_url>
pr-review cost <pr_url>
pr-review diff <pr_url>
```

### `review` — run a review and post comments

```bash
# Security-focused review, dry run first
pr-review review https://github.com/owner/repo/pull/123 --persona security --dry-run

# Post the real review
pr-review review https://github.com/owner/repo/pull/123 --persona security

# Run locally without posting to GitHub
pr-review review https://github.com/owner/repo/pull/123 --no-post

# Force a full re-review even if nothing changed
pr-review review https://github.com/owner/repo/pull/123 --no-incremental
```

| Option | Default | Description |
|---|---|---|
| `--persona` | `style` | `security`, `performance`, or `style` |
| `--dry-run` | off | Show output without posting to GitHub |
| `--no-post` | off | Run review but skip the GitHub API call |
| `--incremental / --no-incremental` | on | Skip hunks already reviewed |

### `history` — view past reviews

```bash
pr-review history https://github.com/owner/repo/pull/123
```

Shows a table of every review run: SHA, persona, timestamp, token usage, and cost.

### `cost` — estimate cost before running

```bash
pr-review cost https://github.com/owner/repo/pull/123 --persona security
```

Fetches the diff and estimates token count and dollar cost without calling the Anthropic API.

### `diff` — inspect the parsed diff

```bash
pr-review diff https://github.com/owner/repo/pull/123
```

Pretty-prints every file, hunk, and line with their diff positions and line numbers. Useful for debugging why a comment landed on the wrong line.

## Example output

```
╭─ PR #123: Add OAuth2 login flow ──────────────────────────────────╮
│ Persona: security  │  Files: 8  │  Hunks: 23  │  Windows: 2      │
│ Est. tokens: ~18,400  │  Est. cost: ~$0.0028                      │
╰────────────────────────────────────────────────────────────────────╯

Summary: The OAuth implementation looks mostly solid but has two issues
worth addressing before merge.

╭─ HIGH  src/auth/oauth.py  pos=34 ────────────────────────────────╮
│ **[HIGH]** The `state` parameter is generated with               │
│ `random.random()` which is not cryptographically secure. Use     │
│ `secrets.token_urlsafe(32)` to prevent CSRF attacks.             │
╰────────────────────────────────────────────────────────────────────╯

╭─ MEDIUM  src/auth/oauth.py  pos=67 ──────────────────────────────╮
│ **[MEDIUM]** Access token stored in a plain session cookie        │
│ without `HttpOnly` or `Secure` flags set.                        │
╰────────────────────────────────────────────────────────────────────╯

✓ Posted 2 comment(s) to GitHub PR #123
Tokens used: 4,821 in / 312 out  │  Actual cost: ~$0.0096
```

## Architecture

```
pr_url
  │
  ▼
GitHubClient          fetch PR info + raw unified diff
  │
  ▼
parse_diff()          state machine → FileDiff / DiffHunk / DiffLine
  │                   each DiffLine carries: new_lineno, diff_position, change_type
  ▼
ReviewDB              filter to unreviewed hunks (hunk content hash comparison)
  │
  ▼
ContextWindowBuilder  pack files into token-budget windows
  │                   splits oversized files at hunk boundaries
  │                   carries prior-window summaries as context
  ▼
Reviewer              calls Claude once per window with persona system prompt
  │                   parses JSON response, validates line numbers,
  │                   drops hallucinated positions silently
  ▼
GitHubClient          POST /repos/{owner}/{repo}/pulls/{n}/reviews
                      with inline comments at exact diff positions

ReviewDB              persist review record + hunk hashes + comments
```

## How incremental reviews work

Every hunk is hashed (SHA-256 of header + content). After a review, those hashes are written to SQLite. On the next run, only hunks whose hash wasn't seen in the last review are sent to Claude. When someone force-pushes to fix one file, only the changed hunks are re-reviewed — not the whole PR.

## Personas

| Persona | Looks for |
|---|---|
| `security` | Injection, auth bypasses, hardcoded secrets, weak crypto, missing input validation, CSRF/SSRF |
| `performance` | O(n²) algorithms, N+1 queries, blocking I/O in async contexts, unbounded memory growth |
| `style` | Unclear names, swallowed exceptions, incorrect logic, missing error handling, magic numbers |

## Project layout

```
pr-reviewer/
├── pr_reviewer/
│   ├── cli.py                  # Click entrypoint — all commands
│   ├── config.py               # pydantic-settings (reads .env)
│   ├── github/
│   │   ├── client.py           # GitHub REST API wrapper (httpx)
│   │   └── diff_parser.py      # unified diff → FileDiff/DiffHunk/DiffLine
│   ├── llm/
│   │   ├── reviewer.py         # context windowing + Anthropic API calls
│   │   └── prompts.py          # system prompts for each persona
│   └── storage/
│       └── db.py               # SQLite schema + queries
├── tests/
│   └── tests/
│       ├── test_diff_parser.py
│       ├── test_context_window.py
│       └── fixtures/
│           └── sample.diff
├── setup.py
└── pyproject.toml
```

## Running tests

```bash
pytest tests/ -v
```

## Requirements

- Python 3.9+
- A GitHub token with `repo` scope (or `pull_requests: write` for fine-grained tokens)
- An Anthropic API key

## Cost

A typical 500-line PR costs roughly $0.01–$0.05 with `claude-opus-4-5` depending on the number of files and how much context is needed. The `pr-review cost` command gives you an estimate before spending anything. Incremental re-reviews on force-pushed commits cost proportionally less — only the changed hunks are sent.