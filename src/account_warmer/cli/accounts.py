"""`warmer accounts …` — add/list/show/pause/resume/remove."""
from __future__ import annotations

import re

import typer
from rich.table import Table

from ..browser.proxy import redact_proxy
from ..core import repositories as repo
from ..services import accounts_service
from .common import console, db_session, die

_WINDOW_RE = re.compile(r"^(\d{1,2}:\d{2})-(\d{1,2}:\d{2})$")

accounts_app = typer.Typer(help="Manage warmed accounts.", no_args_is_help=True)


def _parse_window(window: str | None) -> tuple[str | None, str | None]:
    """Parse ``--window 07:30-23:00`` into (start, end) HH:MM strings."""
    if window is None or not window.strip():
        return None, None
    m = _WINDOW_RE.match(window.strip())
    if not m:
        raise ValueError(f"--window must be START-END in HH:MM form, e.g. 07:30-23:00 (got {window!r})")
    return m.group(1), m.group(2)


@accounts_app.command("add")
def add(
    platform: str = typer.Option(..., "--platform", help="instagram | tiktok"),
    handle: str = typer.Option(..., "--handle", help="username without @"),
    label: str = typer.Option(None, "--label", help="friendly name for dashboard/CLI"),
    tz: str = typer.Option("UTC", "--tz", help="IANA timezone (keep STABLE per account!) e.g. America/New_York"),
    locale: str = typer.Option("en-US", "--locale", help="BCP-47 locale, e.g. en-US"),
    device: str = typer.Option("android-chrome-pixel8", "--device", help="fingerprint preset key"),
    proxy: str = typer.Option(None, "--proxy", help="http(s):// or socks5://user:pass@host:port (socks5 preferred)"),
    window: str = typer.Option(None, "--window", help="daily active hours HH:MM-HH:MM, e.g. 07:30-23:00"),
    ramp: str = typer.Option("default", "--ramp", help="ramp profile key from warmer.yaml"),
    comment_tags: str = typer.Option(
        None, "--comment-tags",
        help="comma-separated comment pool tags from comments/<platform>_comments.yaml "
             "(e.g. food,travel); default = general pool only",
    ),
    videos_per_day: int = typer.Option(None, "--videos-per-day", help="daily cap override (NULL = global/ramp default)"),
    likes_per_day: int = typer.Option(None, "--likes-per-day"),
    comments_per_day: int = typer.Option(None, "--comments-per-day"),
    saves_per_day: int = typer.Option(None, "--saves-per-day"),
    follows_per_day: int = typer.Option(None, "--follows-per-day"),
) -> None:
    """Register a new account (status starts 'new'; log in with `warmer login`)."""
    if platform not in ("instagram", "tiktok"):
        raise die(f"--platform must be instagram or tiktok (got {platform!r})")
    try:
        window_start, window_end = _parse_window(window)
    except ValueError as exc:
        raise die(str(exc))

    tags = [t.strip() for t in comment_tags.split(",") if t.strip()] if comment_tags else None
    if tags:
        # Validate against the pool file so a typo fails at add-time, not mid-session.
        try:
            from ..core.behavior import load_comment_pool

            pool = load_comment_pool(platform)
        except (FileNotFoundError, ValueError) as exc:
            raise die(str(exc))
        unknown = [t for t in tags if t not in pool]
        if unknown:
            raise die(
                f"unknown comment tag(s) {', '.join(unknown)} for {platform}; "
                f"available: {', '.join(sorted(pool))}"
            )

    with db_session() as db:
        try:
            account = repo.create_account(
                db,
                platform=platform,
                handle=handle,
                label=label,
                timezone_id=tz,
                locale=locale,
                device_profile=device,
                proxy_url=proxy or None,
                window_start=window_start,
                window_end=window_end,
                daily_video_cap=videos_per_day,
                daily_like_cap=likes_per_day,
                daily_comment_cap=comments_per_day,
                daily_save_cap=saves_per_day,
                daily_follow_cap=follows_per_day,
                ramp_profile=ramp,
                comment_tags=tags,
            )
        except repo.DuplicateAccountError as exc:
            raise die(str(exc))

    console.print(f"[green]added account #{account.id}[/]: {platform}/{account.handle} "
                  f"(status={account.status}, tz={tz}, device={device})")
    if proxy is None:
        console.print("[yellow]no --proxy given — falls back to .env DEFAULT_PROXY; "
                      "a per-account residential/mobile proxy is strongly recommended (SPEC R1)[/]")


@accounts_app.command("list")
def list_accounts() -> None:
    """Table of all accounts with status and identity."""
    table = Table(title="Accounts")
    for col in ("id", "platform", "handle", "label", "status", "tz", "device", "window", "proxy"):
        table.add_column(col)
    with db_session() as db:
        rows = repo.list_accounts(db)
        for a in rows:
            window = f"{a.window_start}-{a.window_end}" if (a.window_start or a.window_end) else "-"
            from ..browser.proxy import redact_proxy

            table.add_row(
                str(a.id), a.platform, a.handle, a.label or "-", a.status,
                a.timezone_id, a.device_profile, window, redact_proxy(a.proxy_url) or "-",
            )
    console.print(table)


@accounts_app.command("show")
def show(account: int = typer.Argument(..., help="account id")) -> None:
    """Full detail for one account incl. today's usage vs caps."""
    from ..core.settings_overrides import load_config_with_overrides

    table = Table(title=f"Account #{account}")
    with db_session() as db:
        a = repo.get_account(db, account)
        if a is None:
            raise die(f"account {account} not found")
        cfg = load_config_with_overrides()
        from ..services.stats_service import resolve_effective_caps

        caps = resolve_effective_caps(a, cfg)
        rows = [
            ("id", str(a.id)),
            ("platform / handle", f"{a.platform}/{a.handle}"),
            ("label", a.label or "-"),
            ("status", a.status),
            ("proxy", redact_proxy(a.proxy_url) or "-"),
            ("timezone / locale", f"{a.timezone_id} / {a.locale}"),
            ("device_profile", a.device_profile),
            ("window (local)", f"{a.window_start or cfg.defaults.window.start}-{a.window_end or cfg.defaults.window.end}"),
            ("ramp_profile", a.ramp_profile),
            ("last_login_at", a.last_login_at or "-"),
            ("last_session_at", a.last_session_at or "-"),
            ("notes", a.notes or "-"),
        ]
        for kind in ("videos", "likes", "comments", "saves", "follows"):
            used = repo.count_today(db, a, {"videos": "video", "likes": "like", "comments": "comment", "saves": "save", "follows": "follow"}[kind])
            rows.append((f"today {kind} (cap {caps[kind]})", f"{used}/{caps[kind]}"))

    for k, v in rows:
        table.add_row(k, str(v))
    console.print(table)


@accounts_app.command("pause")
def pause(account: int = typer.Argument(...)) -> None:
    """Set status to 'paused' (scheduler skips it)."""
    with db_session() as db:
        if repo.get_account(db, account) is None:
            raise die(f"account {account} not found")
        accounts_service.set_status(db, account, "paused")
    console.print(f"[green]#{account} paused[/]")


@accounts_app.command("resume")
def resume(account: int = typer.Argument(...)) -> None:
    """Resume a paused/flagged account (status back to 'active')."""
    with db_session() as db:
        if repo.get_account(db, account) is None:
            raise die(f"account {account} not found")
        accounts_service.set_status(db, account, "active")
    console.print(f"[green]#{account} resumed (active)[/]")


@accounts_app.command("remove")
def remove(
    account: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip confirmation"),
) -> None:
    """Remove an account and all its history (browser profile dir stays on disk)."""
    with db_session() as db:
        a = repo.get_account(db, account)
        if a is None:
            raise die(f"account {account} not found")
        if not yes:
            answer = typer.prompt(f"Remove {a.platform}/{a.handle}? (y/N)", default="N")
            if answer.strip().lower() != "y":
                console.print("cancelled")
                return
        accounts_service.remove(db, account)
    console.print(f"[green]removed #{account}[/]")
