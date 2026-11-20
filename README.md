# humanish (Account Warmer v1)

Local, self-hosted Instagram/TikTok **account warmer**: drives real browsers
(mobile-emulated Chrome, one persistent profile per account+platform) to watch
feeds and like/save/comment/follow at low randomized rates — so new or dormant
accounts behave human *before* heavy posting begins.

See [SPEC.md](SPEC.md) for the full spec (data model, anti-bot strategy, risk
register) and [STATE.md](STATE.md) for current build state.

## Quickstart

```bash
# 1. Python 3.12+ required. Create venv & install package:
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium        # plus Google Chrome installed = preferred channel

# 2. Init runtime dirs + DB schema, create .env from example:
warmer init

# 3. Add an account (each gets its own stable identity — keep it consistent!)
warmer accounts add --platform instagram --handle myhandle \
    --label "Brand IG" --tz America/New_York --proxy socks5://user:pass@host:port

# 4. First login is MANUAL: opens a visible browser, you log in (password/2FA),
#    the app verifies state via selectors and marks the account active.
warmer login --account 1

# 5. Verify selectors resolve on the live page (do this after any UI change):
warmer check-selectors --account 1

# 6. Warm now (single session) or start the supervisor loop (+ dashboard):
warmer run --account 1 --now
warmer run --all --with-dashboard      # dashboard: http://127.0.0.1:8321

# 7. Watch what happened:
warmer stats --days 7
warmer logs --level warning --category bot_signal

# 8. Stop the supervisor gracefully (if running in background):
warmer stop
```

## CLI Reference

| Command | Purpose |
|---------|---------|
| `warmer init` | Create data/, logs/, .env, initialize DB schema |
| `warmer accounts add --platform <p> --handle <h>` | Add an account (options: --label, --tz, --proxy, --window, --comment-tags, caps) |
| `warmer accounts list / show <id> / pause <id> / resume <id> / remove <id>` | Manage accounts |
| `warmer login --account <id> [--timeout 900]` | Manual one-time login (visible browser) |
| `warmer check-login --account <id>` | Quick session-state probe |
| `warmer run --all [--with-dashboard]` | Supervisor loop: scheduler + optional dashboard |
| `warmer run --account <id> --now` | Single warming session immediately |
| `warmer stop` | Graceful stop (SIGTERM to pid in data/run.lock) |
| `warmer stats [--account <id>] [--days 7]` | Counters vs caps, warnings, selector health |
| `warmer logs [--level warning] [--category bot_signal] [--tail 50]` | Query event log |
| `warmer check-selectors --account <id>` | Live DOM verification of all selector groups |

## Dashboard

Start with `warmer run --all --with-dashboard` → http://127.0.0.1:8321

- **Dashboard** (`/`) — active sessions, account overview, recent warnings, **browser mode** (channel/headless) and an **operator hints** list (no-proxy / shared-proxy / stalled-session / selector-drift warnings)
- **Accounts** (`/accounts-page`) — add accounts, pause/resume, view details
- **Account Detail** (`/account/<id>`) — profile, daily cap progress bars, selector drift warnings, session history
- **Sessions** (`/sessions-page`) — full session history with all counters, **heartbeat + stalled flag**, and a stop button that requests a graceful stop for running sessions
- **Logs** (`/logs-page`) — filterable event log (level/category), auto-refresh
- **Settings** (`/settings-page`) — live-editable overrides stored in the DB (validated against a whitelist of `warmer.yaml` paths; applied on the next session, no restart)

JSON API also available: `/accounts`, `/sessions`, `/stats/overview`, `/stats/daily`, `/logs`, `/settings`, `/settings/schema`.

## The two rules that matter most

1. **One residential/mobile proxy per account** (stored on the account). Shared
   datacenter IPs across accounts are the classic multi-account tell. socks5 is
   preferred (DNS also exits through the proxy — no WebRTC/DNS leak, SPEC R3).
2. **Stable identity per account.** Timezone, locale and device profile live in
   the DB and must never be re-randomized between sessions; the account's tz
   should plausibly match its proxy's exit region (SPEC R5).

## Selector files are "starting points"

`selectors/*.json` hold ordered candidate lists for every UI element. They ship
as reasonable starting values — **run `warmer check-selectors --account <id>`**
per account before trusting them, and after any platform UI change. A fix is a
one-line JSON edit, not a code release (SPEC §7).

## Configuration

- `.env` — secrets/paths only: `WARMER_DATA_DIR`, `DASHBOARD_HOST`, `DASHBOARD_PORT`, `PLAYWRIGHT_CHANNEL`, `HEADLESS`, `DEFAULT_PROXY`
- `config/warmer.yaml` — global tuning: windows, duration ranges, behavior distributions, daily caps, ramp profiles, warning policies
- `config/instagram.yaml` / `config/tiktok.yaml` — per-platform feed modes, scroll gestures, extra ad patterns

Per-account values in the DB override these defaults, and the dashboard **Settings** page can
patch a whitelisted subset live (stored in `app_settings`, applied on the next session — no restart).

## Anti-Bot Strategy (summary)

| Layer | What we do |
|-------|-----------|
| Browser identity | Persistent profile per account; real Chrome channel first; stealth init-script patches |
| Fingerprint | Stable UA/tz/locale/device per account; hash logged per session for audit |
| Network | Per-account proxies (socks5 preferred); WebRTC masked via stealth patch |
| Behavior | Non-uniform dwell/scroll/idle distributions; ramp-up over weeks; quick-check sessions |
| Detection | Bot-signal DOM scan each loop iteration; screenshots on warning; auto-flag after N warnings |

Full details in [SPEC.md §6](SPEC.md#6-anti-bot-strategy).

## Layout

```
config/       YAML tuning (windows, durations, probabilities, caps, ramp profiles)
selectors/    UI selector configs (the anti-UI-change layer)
comments/     Comment pools with {placeholder} substitution
data/         Runtime: SQLite (WAL), per-account Playwright user-data-dirs, run.lock
logs/         Rotating log files + warning screenshots
src/account_warmer/
  core/       Config loading + live overrides, DB models/repos, scheduler, behavior planner, fingerprint
  adapters/   PlatformAdapter ABC, Instagram/TikTok browser impls, selector loader, scroll gestures
  browser/    Context factory, stealth patches, proxy resolver, human input primitives
  services/   Accounts, login, warming session, stats, supervisor loop
  cli/        Typer commands (thin wrappers over services)
  api/        FastAPI dashboard: routes + Jinja2 templates (Tailwind CDN)
tests/        pytest unit tests (behavior, scheduler, repos, selectors, config, fingerprint, proxy, api, service)
```

## Development

```bash
pip install -e ".[dev]"       # editable install with test deps
pytest                        # run all tests (132 passing)
warmer --help                 # CLI entry point (also: python -m account_warmer)
```
