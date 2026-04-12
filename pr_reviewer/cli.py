# pr_reviewer/cli.py
from __future__ import annotations

import sys
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table
from rich import box

from pr_reviewer.config import get_settings
from pr_reviewer.github.client import GitHubClient, parse_pr_url
from pr_reviewer.github.diff_parser import parse_diff, FileDiff
from pr_reviewer.llm.prompts import VALID_PERSONAS
from pr_reviewer.llm.reviewer import Reviewer, ContextWindowBuilder
from pr_reviewer.storage.db import ReviewDB

console = Console()


def _make_clients(settings):
    gh = GitHubClient(settings.github_token)
    db = ReviewDB(settings.db_path)
    return gh, db


def _cost_estimate(
    input_tokens: int, output_tokens: int, settings
) -> float:
    return (
        input_tokens / 1_000_000 * settings.cost_per_million_input
        + output_tokens / 1_000_000 * settings.cost_per_million_output
    )


@click.group()
def cli():
    """AI-powered GitHub PR reviewer using Claude."""


# ── review ────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("pr_url")
@click.option(
    "--persona",
    type=click.Choice(VALID_PERSONAS),
    default="style",
    show_default=True,
    help="Review lens to apply.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be posted without actually posting.",
)
@click.option(
    "--no-post",
    is_flag=True,
    help="Run review locally but skip posting to GitHub.",
)
@click.option(
    "--incremental/--no-incremental",
    default=True,
    show_default=True,
    help="Skip hunks already reviewed in a previous run.",
)
def review(
    pr_url: str,
    persona: str,
    dry_run: bool,
    no_post: bool,
    incremental: bool,
) -> None:
    """Fetch a PR diff, send to Claude, post inline review comments."""
    settings = get_settings()

    try:
        owner, repo, number = parse_pr_url(pr_url)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    with GitHubClient(settings.github_token) as gh:
        db = ReviewDB(settings.db_path)
        reviewer = Reviewer(persona=persona, model=settings.model)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            t = progress.add_task("Fetching PR info…", total=None)
            try:
                pr_info = gh.get_pr_info(owner, repo, number)
            except RuntimeError as e:
                console.print(f"[red]GitHub error:[/red] {e}")
                sys.exit(1)

            progress.update(t, description="Fetching diff…")
            raw_diff = gh.get_diff(owner, repo, number)
            all_files = parse_diff(raw_diff)

            if not all_files:
                console.print("[yellow]No diff found in this PR.[/yellow]")
                return

            # Incremental: filter to new hunks only
            if incremental:
                progress.update(t, description="Checking review history…")
                filtered_files: list[FileDiff] = []
                for f in all_files:
                    new_hunks = db.get_unreviewed_hunks(
                        owner, repo, number, persona, f.path, f.hunks
                    )
                    if new_hunks:
                        import copy
                        nf = copy.copy(f)
                        nf.hunks = new_hunks
                        filtered_files.append(nf)
                if not filtered_files:
                    console.print(
                        "[green]No new hunks since last review. Nothing to do.[/green]\n"
                        "Use [bold]--no-incremental[/bold] to force a full re-review."
                    )
                    return
                files = filtered_files
            else:
                files = all_files

            # Show header panel
            est_tokens = reviewer.estimate_tokens(files, pr_info)
            est_input_cost = est_tokens / 1_000_000 * settings.cost_per_million_input
            file_count = len(files)
            hunk_count = sum(len(f.hunks) for f in files)
            builder = ContextWindowBuilder(model=settings.model)
            windows = builder.build_windows(files, pr_info)
            window_count = len(windows)

        console.print(
            Panel(
                f"[bold]{pr_info.title}[/bold]\n"
                f"Persona: [cyan]{persona}[/cyan]  │  "
                f"Files: {file_count}  │  "
                f"Hunks: {hunk_count}  │  "
                f"Windows: {window_count}\n"
                f"Est. tokens: ~{est_tokens:,}  │  "
                f"Est. cost: ~${est_input_cost:.4f}",
                title=f"[bold blue]PR #{number}[/bold blue]",
                border_style="blue",
            )
        )

        if dry_run:
            # Run the review, show output, don't post
            console.print("\n[bold yellow]DRY RUN — no comments will be posted[/bold yellow]\n")
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                t2 = progress.add_task(f"Reviewing ({window_count} window(s))…", total=None)
                result = reviewer.review(files, pr_info)
                progress.update(t2, description="Done.")

            _print_review_result(result, settings)
            console.print(
                f"\n[bold]{len(result.comments)}[/bold] comment(s) would be posted to GitHub."
            )
            console.print("Run without [bold]--dry-run[/bold] to post.")
            return

        # Real review
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            t3 = progress.add_task(f"Reviewing ({window_count} window(s))…", total=None)
            result = reviewer.review(files, pr_info)
            progress.update(t3, description="Saving to DB…")

            review_id = db.create_review(owner, repo, number, pr_info.head_sha, persona)
            db.update_token_usage(review_id, result.input_tokens, result.output_tokens)

            # Save hunk hashes for incremental tracking
            for f in files:
                db.save_hunk_hashes(review_id, f.path, f.hunks)

            if result.comments and not no_post:
                progress.update(t3, description="Posting review to GitHub…")
                github_comments = [c.to_github_payload() for c in result.comments]
                db.save_comments(
                    review_id,
                    [
                        {
                            "file_path": c.path,
                            "diff_position": c.diff_position,
                            "severity": c.severity,
                            "body": c.body,
                        }
                        for c in result.comments
                    ],
                )
                try:
                    gh.post_review(
                        owner,
                        repo,
                        number,
                        commit_sha=pr_info.head_sha,
                        comments=github_comments,
                        body=result.summary,
                    )
                    progress.update(t3, description="Posted ✓")
                except RuntimeError as e:
                    console.print(f"[red]Failed to post review:[/red] {e}")
                    sys.exit(1)
            else:
                progress.update(t3, description="Done.")

        _print_review_result(result, settings)

        actual_cost = _cost_estimate(result.input_tokens, result.output_tokens, settings)
        console.print(
            f"\n[dim]Tokens used: {result.input_tokens:,} in / "
            f"{result.output_tokens:,} out  │  "
            f"Actual cost: ~${actual_cost:.4f}[/dim]"
        )

        if no_post:
            console.print("\n[yellow]--no-post set: review NOT posted to GitHub.[/yellow]")
        elif result.comments:
            console.print(
                f"\n[green]✓ Posted {len(result.comments)} comment(s) to GitHub PR #{number}[/green]"
            )
        else:
            console.print("\n[green]✓ No issues found.[/green]")


def _print_review_result(result, settings) -> None:
    console.print(f"\n[bold]Summary:[/bold] {result.summary}\n")
    if not result.comments:
        console.print("[green]No issues found.[/green]")
        return

    for c in result.comments:
        severity_color = {"high": "red", "medium": "yellow", "low": "blue"}.get(
            c.severity, "white"
        )
        console.print(
            Panel(
                c.body,
                title=(
                    f"[{severity_color}]{c.severity.upper()}[/{severity_color}]  "
                    f"[bold]{c.path}[/bold]  pos={c.diff_position}"
                ),
                border_style=severity_color,
                box=box.ROUNDED,
            )
        )


# ── history ───────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("pr_url")
def history(pr_url: str) -> None:
    """Show past review history for a PR."""
    settings = get_settings()
    try:
        owner, repo, number = parse_pr_url(pr_url)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    db = ReviewDB(settings.db_path)
    records = db.list_reviews(owner, repo, number)

    if not records:
        console.print(f"No review history for PR #{number}.")
        return

    table = Table(title=f"Review history: {owner}/{repo}#{number}", box=box.ROUNDED)
    table.add_column("ID", style="dim")
    table.add_column("SHA", style="cyan")
    table.add_column("Persona")
    table.add_column("Reviewed at")
    table.add_column("Input tok", justify="right")
    table.add_column("Output tok", justify="right")
    table.add_column("Cost", justify="right")

    for r in records:
        cost = _cost_estimate(r.input_tokens, r.output_tokens, settings)
        table.add_row(
            str(r.id),
            r.head_sha[:8],
            r.persona,
            r.reviewed_at,
            f"{r.input_tokens:,}",
            f"{r.output_tokens:,}",
            f"${cost:.4f}",
        )

    console.print(table)


# ── cost ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("pr_url")
@click.option("--persona", type=click.Choice(VALID_PERSONAS), default="style")
def cost(pr_url: str, persona: str) -> None:
    """Estimate token usage and cost before running a review."""
    settings = get_settings()
    try:
        owner, repo, number = parse_pr_url(pr_url)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    with GitHubClient(settings.github_token) as gh:
        pr_info = gh.get_pr_info(owner, repo, number)
        raw_diff = gh.get_diff(owner, repo, number)

    files = parse_diff(raw_diff)
    reviewer = Reviewer(persona=persona, model=settings.model)
    est_tokens = reviewer.estimate_tokens(files, pr_info)
    est_cost = est_tokens / 1_000_000 * settings.cost_per_million_input

    console.print(
        Panel(
            f"Files: {len(files)}  │  "
            f"Hunks: {sum(len(f.hunks) for f in files)}\n"
            f"Est. input tokens: ~{est_tokens:,}\n"
            f"Est. cost (input only): ~${est_cost:.4f}",
            title=f"Cost estimate — {persona}",
            border_style="cyan",
        )
    )


# ── diff ──────────────────────────────────────────────────────────────────────

@cli.command("diff")
@click.argument("pr_url")
def show_diff(pr_url: str) -> None:
    """Pretty-print the parsed diff (debug tool)."""
    settings = get_settings()
    try:
        owner, repo, number = parse_pr_url(pr_url)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    with GitHubClient(settings.github_token) as gh:
        raw_diff = gh.get_diff(owner, repo, number)

    files = parse_diff(raw_diff)
    console.print(f"[bold]Parsed {len(files)} file(s)[/bold]\n")

    for f in files:
        status_color = {
            "added": "green",
            "deleted": "red",
            "modified": "yellow",
            "renamed": "cyan",
        }.get(f.status, "white")
        console.print(
            f"[{status_color}][{f.status.upper()}][/{status_color}] "
            f"[bold]{f.path}[/bold]  "
            f"({len(f.hunks)} hunk(s), ~{f.token_estimate} tokens)"
        )
        for hunk in f.hunks:
            console.print(f"  [dim]{hunk.header}[/dim]")
            for line in hunk.lines:
                color = (
                    "green" if line.change_type == "added"
                    else "red" if line.change_type == "removed"
                    else "dim"
                )
                console.print(
                    f"    [{color}]{line.content[:80]}[/{color}]"
                    f"  [dim]pos={line.diff_position}  "
                    f"new={line.new_lineno}[/dim]"
                )