"""`warmer check-selectors --account <id>` — selector health smoke test.

Step 5: full live DOM verification. Opens the account's persistent browser profile,
navigates to the feed, and checks every selector group against the real page.
Reports pass/fail per group with the matched candidate index.
"""
from __future__ import annotations

import asyncio

import typer
from rich.table import Table

from ..adapters.selector_loader import SelectorFileError, load_platform_selectors
from ..core import repositories as repo
from .common import console, db_session, die


def check_selectors_command(
    account: int = typer.Option(..., "--account", help="account id (selects platform + live DOM context)"),
    live: bool = typer.Option(True, "--live/--no-live", help="open real browser and verify against live DOM"),
) -> None:
    """Verify selector groups for the account's platform.

    With ``--live`` (default): opens the account's persistent profile in a real browser,
    navigates to the feed URL, and checks every group resolves on the actual page.
    Without ``--live``: schema validation only (fast, no browser needed).
    """
    with db_session() as db:
        a = repo.get_account(db, account)
        if a is None:
            raise die(f"account {account} not found")

    try:
        selectors = load_platform_selectors(a.platform)
    except (SelectorFileError, FileNotFoundError) as exc:
        console.print(f"[red]selector file problems for {a.platform}:[/]")
        if isinstance(exc, SelectorFileError):
            for err in exc.errors:
                console.print(f"  [red]•[/] {err}")
        else:
            console.print(f"  [red]•[/] {exc}")
        raise typer.Exit(1)

    # Schema table (always shown).
    schema_table = Table(title=f"Selectors — {selectors.platform} (schema v{selectors.schema_version})")
    schema_table.add_column("group")
    schema_table.add_column("candidates", justify="right")
    schema_table.add_column("text patterns", justify="right")
    schema_table.add_column("url hints", justify="right")

    for name, group in selectors.groups.items():
        schema_table.add_row(
            name,
            str(len(group.candidates)),
            str(len(group.compiled_text_patterns)),
            str(len(group.url_contains)),
        )

    console.print(schema_table)
    console.print(f"[green]schema OK[/]: {len(selectors.groups)} groups resolved; "
                  f"base_urls: {', '.join(selectors.base_urls)}")

    if not live:
        return

    # Live DOM check.
    console.print("\n[cyan]opening browser for live DOM verification…[/]")
    try:
        results = asyncio.run(_live_check(a, selectors))
    except Exception as exc:
        console.print(f"[red]live check failed:[/] {exc}")
        raise typer.Exit(1)

    # Results table.
    live_table = Table(title=f"Live DOM Check — {a.platform}/{a.handle}(#{a.id})")
    live_table.add_column("group", style="bold")
    live_table.add_column("status")
    live_table.add_column("matched candidate", justify="right")
    live_table.add_column("note")

    passed = 0
    failed = 0
    for name, (ok, matched_idx, note) in results.items():
        if ok:
            passed += 1
            status_str = "[green]PASS[/]"
        else:
            failed += 1
            status_str = "[red]FAIL[/]"
        live_table.add_row(name, status_str, str(matched_idx), note)

    console.print(live_table)
    if failed == 0:
        console.print(f"[green]all {passed} groups resolved on live DOM ✓[/]")
    else:
        console.print(f"[yellow]{failed}/{passed + failed} groups failed — update selectors/{a.platform}_selectors.json[/]")


async def _live_check(account, selectors) -> dict[str, tuple[bool, int, str]]:
    """Open the account's browser profile and verify each selector group on the live feed.

    Returns {group_name: (resolved: bool, matched_index: int, note: str)}.
    """
    from ..browser.context import launch_persistent_context
    from ..core.config import find_project_root, load_env
    from ..services.login_service import _build_context_options

    root_path = find_project_root()
    load_env(root_path)

    # Build context options for this account (headless for background check).
    options = _build_context_options(account, root_path, headless=True)

    playwright, context = await launch_persistent_context(options)
    results: dict[str, tuple[bool, int, str]] = {}

    try:
        page = context.pages[0] if context.pages else await context.new_page()

        # Navigate to the feed (first available base_url — dict of mode→url).
        first_mode = next(iter(selectors.base_urls), None)
        feed_url = selectors.base_urls.get(first_mode, "https://www.instagram.com/") if first_mode else "https://www.instagram.com/"
        console.print(f"  navigating to {feed_url} …")
        try:
            await page.goto(feed_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(3000)  # let the feed render
        except Exception as exc:
            console.print(f"  [yellow]navigation warning:[/] {exc}")

        # Check each group.
        for name, group in selectors.groups.items():
            if not group.candidates:
                results[name] = (False, -1, "no candidates defined")
                continue

            matched_idx = -1
            resolved = False
            note = ""

            for idx, candidate in enumerate(group.candidates):
                try:
                    el = await page.query_selector(candidate)
                    if el is not None:
                        # Check visibility.
                        visible = await el.is_visible()
                        if visible:
                            matched_idx = idx
                            resolved = True
                            note = f"matched '{candidate[:50]}'"
                            break
                        else:
                            note = f"'{candidate[:40]}' found but hidden"
                except Exception:
                    continue

            results[name] = (resolved, matched_idx, note)
            status = "✓" if resolved else "✗"
            console.print(f"  {status} {name}: {'resolved' if resolved else 'NOT FOUND'} ({note})")

    finally:
        try:
            await context.close()
        except Exception:
            pass
        await playwright.stop()

    return results
