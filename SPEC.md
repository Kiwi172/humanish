# SPEC — Instagram / TikTok Account Warmer (v1)

> Status: **v1 complete** (Steps 1–8 done — see STATE.md v8). Step 8 was a spec-vs-code audit
> that closed 13 "declared but not wired" gaps (file logging, daily video cap, live settings
> overrides, stop button, mid-session login check, scroll/typing config, topic pools, dashboard
> hints/heartbeat, single-instance lock). Remaining work is operational: live selector
> verification + end-to-end browser test.
> Last updated: 2026-10-07

---

## 1. Product Overview

The **Account Warmer** is a local, self-hosted tool that makes new or dormant
Instagram and TikTok accounts behave like real human users *before* heavy
posting/promotion begins. It drives real browsers (mobile-emulated Chrome) to
watch feeds (IG Reels / Home, TikTok For You), and optionally likes, saves,
comments, and follows at low, randomized rates — while logging everything so
the operator can spot bot-detection signals early.

**Primary goal:** reduce bot-detection risk (behavioral + fingerprint based) by
making each account's activity look organic: consistent "device" identity,
residential/mobile IP, human-timing behavior, and gradual volume ramp-up.

**Approach decision:** web-browser automation first (easiest to maintain, no
app-store/device farm), with a **platform-adapter interface** designed so a
mobile/native adapter (e.g., Android Appium) can be plugged in later without
touching the scheduler, database, or dashboard.

### Non-goals (v1)
- Posting/promotion automation, stories, Reels upload, DMs, hashtag search.
- Unattended login / 2FA solving — first login is always **manual** per account.
- Multi-machine deployment / cloud sync (single local machine assumed).

---

## 2. Core Features

### Accounts & sessions
1. **Multi-account support** — any number of IG and/or TikTok accounts, each independent.
2. **Separate persistent browser profile/state per account** — one Playwright `user-data-dir` per account+platform (cookies, localStorage, IndexedDB, service workers all persist). No shared/generic profile ever.
3. **Manual one-time login flow with session persistence** — CLI opens a visible browser on the login page; human logs in (password/QR/2FA); app verifies logged-in state via selectors and marks account `active`. Sessions survive restarts until the platform expires them.

### Warming behavior
4. **Instagram Reels + Home feed warming.**
5. **TikTok For You page (FYP) warming.**
6. **Randomized session start times** within a per-account daily active window (e.g., 08:00–23:00 local), drawn from a non-uniform distribution (not pure uniform; biased toward "natural" hours).
7. **Randomized session duration** (configurable min/max, e.g., 15–45 min) including occasional short "quick check" sessions (~5 min).
8. **Human-like scrolling, pauses, idle periods, variable watch lengths** — incremental scrolls with jitter, dwell times sampled from distributions (longer dwell when the account intends to interact), random idle bursts ("put phone down", 30 s–5 min), occasional over-scroll-and-bounce-back and back-navigation.
9. **Random likes based on configurable probability.**
10. **Random comments** drawn from a configurable comment pool/templates (`comments/*.yaml`) with placeholder substitution (e.g., `{adjective}`, topic-tagged pools per account).
11. **Duplicate action prevention per account/video** — DB unique constraints: one like/save/follow and at most one comment per (account, video); checked before acting and enforced in the database.
12. **Daily limits** for likes / comments / saves / follows (and videos watched), with a **ramp-up schedule**: new accounts start low (~8–15 videos/day) and increase over weeks; caps configurable globally (YAML) with per-account overrides (DB).
13. **Auto-skip ads** — detection via selectors + text patterns ("Ad", "Sponsored", ad URL patterns); skip via close button when present, else minimum-watch fallback before scrolling past. Skip method logged per ad.

### Identity & network consistency
14. **Proxy support per account/session** — http/https/socks5 with auth; stored per account in DB, overridable in `.env` default or per run. Residential/mobile proxies recommended for warming (documented as top risk mitigation).
15. **Timezone / locale / device-fingerprint consistency** — each account gets a stable fingerprint profile: user agent, IANA timezone, BCP-47 locale, viewport/screen/touch settings; hash of the active fingerprint is recorded per session for audit ("did this session match its identity?").

### Observability
16. **Logging** of watched videos (with watch duration), all actions, errors, and **bot-detection warnings** (captcha, "check your phone number", "unusual activity detected", login wall, cookie challenge) into a queryable DB table + rotating file logs; screenshots attached on warning/error where feasible.
17. **Local web dashboard** — accounts (status, counters vs caps), live/recent sessions, stats per account and day, log viewer with filters, settings view/edit.
18. **CLI** — add/list/pause/remove accounts, manual login, run warmer (scheduled or immediate), show stats/logs, selector smoke test, start dashboard.

---

## 3. Architecture Plan

### Layer diagram

```
┌───────────────┐      ┌───────────────────────┐
│  Typer CLI    │      │ FastAPI + Jinja2/Tail- │
│ accounts/login/│     │ wind CDN (local UI)    │
│ run/stats/logs │     │ dashboard              │
└───────┬───────┘      └───────────▲───────────┘
        │                          │ HTML + JSON endpoints
        ▼                          │
┌─────────────────────────────────────────────────────────┐
│ Service layer (business logic, shared by CLI & API)     │
│  accounts / warmer_service / stats / scheduler          │
└───────────────▲─────────────────────────────────────────┘
                │
┌───────────────┴─────────────────────────────────────────┐
│ Core engine                                             │
│  behavior planner (distributions, idle, ramp)           │
│  fingerprint builder · proxy resolver                   │
│  selector loader (JSON → resolved candidates)           │
└───────────────▲─────────────────────────────────────────┘
                │
┌───────────────┴─────────────────────────────────────────┐
│ Platform adapters — `PlatformAdapter` ABC               │
│   InstagramAdapter (browser impl)                       │
│   TikTokAdapter    (browser impl)                       │
│   [MobileAdapter]  (future, same interface)             │
└───────────────▲─────────────────────────────────────────┘
                │
        Playwright persistent contexts
        (one user-data-dir per account+platform;
         real Chrome channel preferred + stealth patches)

Data: SQLite (WAL mode) — single source of truth for state, actions, logs.
Config: .env (secrets/paths) + config/*.yaml (tuning) + selectors/*.json (UI).
```

### Process model
- **One supervisor process** runs everything: an asyncio loop hosts the
  scheduler task(s) plus (optionally) uvicorn serving the dashboard in the same
  loop. `warmer run --with-dashboard` = both; short-lived CLI commands are
  separate invocations that just talk to SQLite + config.
- **SQLite WAL mode** + `busy_timeout=5000`: dashboard reads while a session
  writes. One writer per account enforced by `sessions.status='running'` check;
  optional file lock (`data/run.lock`) prevents two supervisor processes.

### Key components (responsibilities)

| Component | Responsibility | Notes |
|---|---|---|
| `scheduler` | Per-account daily plan: pick start time inside active window, session length, whether to run a "quick check" short session; reschedule after warnings with backoff | Hand-rolled asyncio (no APScheduler dependency in v1) |
| `behavior planner` | Generates the action timeline for one session: dwell times, scroll pattern, idle bursts, interaction decisions using per-platform probabilities + daily caps + ramp profile | All randomness lives here, so adapters stay deterministic given a plan step |
| `fingerprint builder` | Resolves an account row → concrete context options (UA, tz, locale, viewport, touch, screen) from built-in device presets; computes fingerprint hash | Presets in code as data (`core/fingerprint.py`) |
| `proxy resolver` | Account proxy → Playwright proxy dict; validates scheme/auth; logs unresolved proxies as warnings | Supports `http(s)://`, `socks5://` with user:pass |
| `selector loader` | Loads `selectors/<platform>_selectors.json`, exposes logical keys (`like_button`, `ad_close`, …), each = ordered candidate list + optional text patterns; records which candidate matched (health stats) | Zero raw CSS in adapter code |
| `PlatformAdapter` (ABC) | Per-session API: `open_feed(mode) → current_video() / scroll_next() / rewatch_previous() / detect_ad() / skip_ad(ad) / like() / save() / comment(text) / follow(author) / check_login_state() / scan_bot_signals()` | Gesture/typing realism injected as `GestureParams` (adapters never read config files); browser impls use Playwright; future mobile impl can reuse the same service-layer contract |
| `warmer_service` | Orchestrates one full warming session end-to-end (open context → loop plan steps → record to DB → teardown), translates adapter results into DB writes + event logs | The only place sessions are created/closed; polls the stop flag + heartbeat each iteration, re-checks login state mid-session (R9), enforces the per-session video budget |
| `settings_overrides` | Applies live `app_settings` rows onto the validated `WarmerConfig` (whitelisted dotted paths, validated on write) | Lets the dashboard Settings page retune caps/probabilities without a YAML edit or restart (§10) |

### Warming session pseudocode (illustrative)

```python
async def run_warming_session(account):
    fp      = fingerprint.build(account)            # ua/tz/locale/viewport/proxy
    ctx     = launch_persistent_context(
                 user_data_dir=f"data/profiles/{account.id}_{account.platform}",
                 channel=fp.browser_channel,        # "chrome" if installed else stealth chromium
                 proxy=proxy.resolve(account), timezone_id=fp.tz, locale=fp.locale,
                 viewport=fp.viewport, is_mobile=True, has_touch=True)
    apply_stealth(ctx)                              # playwright-extra StealthPlugin or init-script patches
    page    = ctx.new_page()
    session = db.create_session(account, planned_duration_s=plan.duration, fingerprint_hash=fp.hash)

    adapter = get_adapter(account.platform, page, selectors=selector_loader[account.platform])
    await adapter.open_feed(mode=plan.feed_mode)     # "reels"|"home" (IG), "fyp" (TikTok)

    while not plan.expired():                        # duration budget or user stop
        sig   = await adapter.scan_bot_signals()     # captcha / check phone / unusual activity / login wall
        if sig: log_warning(session, sig); maybe_pause_account(account); break_or_backoff(sig)

        ad = await adapter.detect_ad()               # selectors + text patterns
        if ad:
            method = await adapter.skip_ad(ad)       # close_button | min_watch fallback
            db.record_watched(was_ad=1, ad_skip_method=method); continue

        dwell  = plan.sample_dwell(interact_intent=plan.maybe_interact())   # distribution-based
        video  = await adapter.watch_current(dwell)  # human scrolling + waits; variable watch length
        db.record_watched(video, watch_seconds=dwell)

        decision = plan.decide()                     # like/save/comment/follow with probabilities + caps
        if decision == LIKE    and not db.already_liked(account, video.key):   await adapter.like();    db.add_action(...)
        elif decision == SAVE  and ...:                                                     await adapter.save();  db.add_action(...)
        elif decision == COMMENT and not db.already_commented(account, video.key) and cap_ok("comment"):
            text = comment_pool.pick(account)                                              await adapter.comment(text); db.add_comment(...)
        elif decision == FOLLOW and cap_ok("follow") and not db.already_followed(...):      await adapter.follow(video.author); db.add_action(...)

        if plan.maybe_idle():  await human_pause(plan.idle_range())   # "put the phone down"
    teardown(ctx, session)                            # final counters, status, stop_reason
```

### Tech stack (final for v1)
- **Python 3.12**, `uv` or plain pip; package under `src/`.
- **Playwright** (Python) + **playwright-extra** (`StealthPlugin`). *Risk:* playwright-extra lags Playwright releases — fallback is a manual init-script patch set in `browser/context.py` (same behavior, no plugin dep). Real Chrome via `channel="chrome"` preferred; stealth Chromium as fallback.
- **FastAPI + uvicorn[standard] + Jinja2**, Tailwind via CDN (no build step) for the dashboard. Streamlit rejected: keeps one runtime, full control over layout/polling, and no extra heavy dependency.
- **SQLite** via **SQLAlchemy 2.x ORM** (WAL mode, `PRAGMA foreign_keys=ON`), no Alembic in v1 (`create_all` + a tiny migration script if needed).
- **Typer** for the CLI; **Pydantic v2** for config validation; **python-dotenv**; **PyYAML**.
- Logging: stdlib `logging` to rotating files under `logs/`; the queryable record of truth is the `event_logs` DB table.

---

## 4. Proposed File / Folder Structure

```
account-warmer/                        # repo root (this directory)
├── SPEC.md                            # this file
├── STATE.md                           # state snapshot (updated each step)
├── README.md                          # quickstart (Step 2+)
├── pyproject.toml                     # deps + console_scripts entry: warmer = account_warmer.main:app
├── .env.example                       # copy to .env; secrets & paths only
├── .gitignore                         # data/, logs/, .env, __pycache__ …
│
├── config/                            # YAML tuning (committed defaults)
│   ├── warmer.yaml                    # global: windows, duration ranges, probabilities, caps, ramp profiles, idle ranges
│   ├── instagram.yaml                 # IG-specific: feed URLs/modes, scroll params, ad text patterns extra list
│   └── tiktok.yaml                    # TikTok-specific: same shape
│
├── selectors/                         # UI selector configs (the "anti-UI-change" layer)
│   ├── instagram_selectors.json       # see §7 schema
│   └── tiktok_selectors.json
│
├── comments/                          # comment pools / templates
│   ├── ig_comments.yaml               # tagged lists + {placeholders}; per-language sections optional
│   └── tt_comments.yaml
│
├── data/                              # runtime (gitignored)
│   ├── warmer.db                      # SQLite (WAL)
│   ├── run.lock                       # supervisor lock file
│   └── profiles/<account_id>_<platform>/   # Playwright user-data-dir per account+platform
│
├── logs/                              # rotating app log files (gitignored)
│   ├── warmer.log / warmer.log.1 …
│   └── screenshots/                   # on warning/error captures
│
├── src/account_warmer/
│   ├── __init__.py
│   ├── main.py                        # Typer app root: wires subcommands, `run`, `dashboard`
│   ├── cli/                           # thin CLI wrappers over services (no business logic here)
│   │   ├── accounts.py                # add/list/show/pause/resume/remove
│   │   ├── login.py                   # manual login flow, check-login
│   │   ├── run.py                     # run warmer now / start scheduler loop
│   │   ├── stats.py                   # stats/logs display
│   │   └── check_selectors.py         # smoke test: verify selector groups resolve on live pages
│   ├── core/
│   │   ├── config.py                  # .env + YAML loading, Pydantic validation, defaults merge
│   │   ├── db.py                      # engine (WAL), session factory, create_all + tiny column migration
│   │   ├── models.py                  # SQLAlchemy ORM: Account, Session, WatchedVideo, Action, CommentHistory, EventLog, AppSetting
│   │   ├── repositories.py            # query helpers incl. dedupe checks, daily counters, heartbeat/stop flags
│   │   ├── scheduler.py               # session planning (start time, duration, quick-checks, video budget), backoff
│   │   ├── behavior.py                # distributions: dwell/scroll/idle sampling, interaction decisions, caps+ramp enforcement, GestureParams
│   │   ├── settings_overrides.py      # live app_settings → validated WarmerConfig patches (whitelisted paths)
│   │   ├── bootstrap.py               # runtime dirs + .env seeding + DB init (backs `warmer init`)
│   │   └── fingerprint.py             # device presets (android-chrome-pixel8 default; iphone preset flagged) + context option builder + hash
│   ├── adapters/
│   │   ├── base.py                    # PlatformAdapter ABC + dataclasses (AdInfo, BotSignal, VideoRef, GestureParams)
│   │   ├── dom.py                     # candidate resolution (first-visible-wins) + SelectorHealth drift tally
│   │   ├── scrolling.py               # config-driven swipe / over-scroll-bounce / back-scroll-rewatch (shared by browser impls)
│   │   ├── instagram.py               # browser impl: reels/home feeds, ad skip, actions, signal scan
│   │   ├── tiktok.py                  # browser impl: FYP, ad skip, actions, signal scan
│   │   └── selector_loader.py         # JSON loading, candidate resolution order, match-index health logging
│   ├── browser/
│   │   ├── context.py                 # launch_persistent_context factory (channel choice, stealth apply)
│   │   ├── stealth_patches.py         # fallback init-script patches if plugin unavailable/incompatible
│   │   ├── proxy.py                   # proxy URL validation → Playwright dict
│   │   └── human_input.py             # multi-step pointer/touch movement w/ jitter & offset, typing delays
│   ├── services/                      # shared by CLI and API
│   │   ├── accounts_service.py        # CRUD + status transitions
│   │   ├── login_service.py           # open page → wait for manual login (selector-verified) → persist state
│   │   ├── warmer_service.py          # orchestrates one session (pseudocode in §3)
│   │   └── stats_service.py           # aggregates: per-account/day counters, warnings, selector health
│   ├── api/
│   │   ├── app.py                     # FastAPI factory, static/templates mount
│   │   ├── routes_accounts.py         # GET list/detail, POST create, PATCH status/proxy/caps
│   │   ├── routes_sessions.py         # GET sessions (live + history), stop session
│   │   ├── routes_stats.py            # aggregates for charts
│   │   ├── routes_logs.py             # filtered event log query + tail
│   │   └── templates/                 # Jinja2, Tailwind CDN, light polling (no build step)
│   │       ├── base.html
│   │       ├── dashboard.html         # overview: active sessions, 24h warnings, today's totals vs caps
│   │       ├── accounts.html          # table w/ status filters; add-account form
│   │       ├── account_detail.html    # profile edit (proxy/tz/caps/window), stats, recent actions/logs
│   │       ├── sessions.html
│   │       └── logs.html              # level/category/account filters, auto-refresh
│   └── utils/
│       ├── logging_setup.py           # rotating file + console handlers
│       └── timeutil.py                # tz-aware "now", window parsing (HH:MM), backoff helpers
│
└── tests/                             # pytest; unit-heavy in v1 (no live-browser CI)
    ├── test_behavior.py               # distributions, caps, ramp, dedupe decisions, topic-tagged pools (seeded RNG)
    ├── test_scheduler.py              # start-time sampling within window, quick-check probability, video budget
    ├── test_repositories.py           # dedupe + daily counter queries, heartbeat/stop flags (tmp sqlite)
    ├── test_selector_loader.py        # JSON schema validation, candidate ordering, health logging
    ├── test_settings_overrides.py     # live override whitelist, validation, GestureParams from config
    ├── test_warmer_service.py         # browser-free session control paths (daily-cap skip)
    ├── test_api.py                    # settings CRUD + validation, drift, stop (graceful/stalled), HTML pages
    ├── test_config.py / test_fingerprint.py / test_proxy.py
    └── conftest.py                    # real repo root + isolated tmp SQLite per test
```

**Dependency rule of thumb:** adapters know *nothing* about the DB or config files except through injected selector objects; services own orchestration and persistence; CLI/API are dumb wrappers. This is what makes a future mobile adapter drop-in: implement `PlatformAdapter`, nothing else changes.

---

## 5. Data Model / Database Schema (draft)

SQLite, WAL mode, `PRAGMA foreign_keys=ON`. All timestamps stored as UTC ISO-8601 text; display layer converts to account tz.

```sql
CREATE TABLE accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    platform       TEXT NOT NULL CHECK (platform IN ('instagram','tiktok')),
    handle         TEXT NOT NULL,                 -- username without @
    label          TEXT,                          -- friendly name for dashboard/CLI
    status         TEXT NOT NULL DEFAULT 'new'
                   CHECK (status IN ('new','needs_login','active','paused','flagged','error')),
    proxy_url      TEXT,                          -- http(s):// or socks5:// with user:pass; NULL = global default/none
    timezone_id    TEXT NOT NULL DEFAULT 'UTC',   -- IANA, e.g. "America/New_York" — keep stable per account!
    locale         TEXT NOT NULL DEFAULT 'en-US', -- BCP-47
    device_profile TEXT NOT NULL DEFAULT 'android-chrome-pixel8',  -- key into fingerprint presets
    window_start   TEXT,                          -- HH:MM (account-local); NULL = global default from warmer.yaml
    window_end     TEXT,
    daily_video_cap   INTEGER,                    -- NULL = global/ramp default
    daily_like_cap    INTEGER,
    daily_comment_cap INTEGER,
    daily_save_cap    INTEGER,
    daily_follow_cap  INTEGER,
    ramp_profile     TEXT NOT NULL DEFAULT 'default',   -- key into warmer.yaml ramp_profiles
    comment_tags     TEXT,                             -- JSON list of pool tags from comments/*.yaml (SPEC §2.10); NULL = general only
    last_login_at    TEXT,
    last_session_at  TEXT,
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (platform, handle)
);

CREATE TABLE sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'scheduled'
                  CHECK (status IN ('scheduled','running','completed','aborted','error')),
    stop_reason   TEXT,                           -- 'duration_reached' | 'bot_signal:<type>' | 'proxy_down' | 'user_stop' …
    started_at    TEXT, ended_at TEXT,
    planned_duration_s INTEGER, actual_duration_s INTEGER,
    proxy_used    TEXT,                           -- resolved value actually used (redact password in logs)
    fingerprint_hash TEXT,                        -- sha1(ua|tz|locale|device_profile) — identity audit
    videos_watched INTEGER NOT NULL DEFAULT 0, ads_skipped INTEGER NOT NULL DEFAULT 0,
    likes_given   INTEGER NOT NULL DEFAULT 0, comments_made INTEGER NOT NULL DEFAULT 0,
    saves_done    INTEGER NOT NULL DEFAULT 0, follows_done  INTEGER NOT NULL DEFAULT 0,
    warnings_count INTEGER NOT NULL DEFAULT 0, errors_count  INTEGER NOT NULL DEFAULT 0,
    heartbeat_at  TEXT,                           -- stamped each loop iteration while running (R21 stall visibility)
    stop_requested INTEGER NOT NULL DEFAULT 0,    -- dashboard stop button sets this; loop polls it (§2.17)
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_sessions_account_time ON sessions(account_id, started_at DESC);

CREATE TABLE watched_videos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    account_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    video_key      TEXT NOT NULL,                 -- stable id: IG reel shortcode / TikTok aweme id or url slug
    author_handle  TEXT,
    caption_snippet TEXT,                         -- first ~200 chars; context only
    watched_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    watch_seconds  REAL NOT NULL DEFAULT 0,       -- actual dwell on this video (variable by design)
    was_ad         INTEGER NOT NULL DEFAULT 0,
    ad_skip_method TEXT CHECK (ad_skip_method IN ('close_button','text_pattern','min_watch'))
);
CREATE INDEX idx_watched_account_video ON watched_videos(account_id, video_key);
CREATE INDEX idx_watched_session       ON watched_videos(session_id);

-- Non-comment actions. Dedupe: one like/save per account+video; follow per target author.
CREATE TABLE actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    video_key     TEXT,                           -- NULL for 'follow'
    target_handle TEXT,                           -- set for 'follow' (author handle)
    action_type   TEXT NOT NULL CHECK (action_type IN ('like','save','follow')),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (account_id, video_key, action_type)
);
CREATE UNIQUE INDEX idx_actions_follow ON actions(account_id, target_handle) WHERE action_type = 'follow';

-- Comments kept in their own table: full text + template provenance for review.
CREATE TABLE comments_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    video_key     TEXT NOT NULL,
    author_handle TEXT,
    comment_text  TEXT NOT NULL,
    template_key  TEXT,                           -- pool/template id that generated it (NULL = manual)
    posted_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (account_id, video_key)                -- max one comment per account+video
);

-- Unified log of app events + errors + bot-detection warnings.
CREATE TABLE event_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,   -- NULL = global/app-level
    session_id  INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    level       TEXT NOT NULL CHECK (level IN ('debug','info','warning','error')),
    category    TEXT NOT NULL DEFAULT 'app'
                CHECK (category IN ('app','login','bot_signal','ad','selector','proxy','network','limit')),
    message     TEXT NOT NULL,
    detail      TEXT,                             -- JSON: failed selector candidates + matched index, url, screenshot path…
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_events_account_time ON event_logs(account_id, created_at DESC);
CREATE INDEX idx_events_level_cat    ON event_logs(level, category);

-- Optional: dashboard-editable overrides (avoids hand-editing YAML while running).
CREATE TABLE app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                            -- JSON-encoded; e.g. {"global_daily_like_cap": 15}
);
```

**Dedupe & caps enforcement:** services query `actions`/`comments_history` before acting (cheap indexed lookups) *and* rely on the UNIQUE constraints as a race-safe backstop (catch `IntegrityError` → count as "already acted", not error). Daily counters are computed from these tables per UTC-local day of the account's timezone; ramp profile + caps decide what the behavior planner is allowed to attempt.

**Storage note:** one row in `watched_videos` per video-view is fine at warming volumes (~50–150 rows/account/day); optional monthly prune/archive job later, not v1.

---

## 6. Anti-Bot Strategy

### 6.1 Browser identity
| Measure | Detail |
|---|---|
| **Persistent context per account** | `launch_persistent_context(user_data_dir=data/profiles/<id>_<platform>)`. Same "device" across all sessions: cookies, storage, cache persist → platform sees a returning visitor, not a fresh profile. |
| **No shared profiles** | One user-data-dir strictly per (account, platform). Two accounts never share one even if owned by the same human. |
| **Real Chrome channel first** | `channel="chrome"` when Google Chrome is installed → real binary's TLS/HTTP2 fingerprint, `window.chrome` present, fewer CDP tells. Fallback: Chromium + stealth patches. |
| **Stealth layer** | playwright-extra `StealthPlugin`; if version-incompatible (known risk), equivalent manual init-script patches in `browser/stealth_patches.py`: remove `navigator.webdriver`, patch `plugins/languages/chrome`, permissions API, WebGL vendor string, iframe contentWindow check, outerWidth/Height. |
| **Headless avoidance** | Prefer headed mode on the operator's machine (visible window). If backgrounded: new headless of real Chrome (`channel="chrome"`) or Xvfb; never "old-style" chromium headless as a silent default — config `HEADLESS` makes it explicit, dashboard shows which mode is active. |

### 6.2 Emulation consistency
- **Primary preset (default): Android + Google Chrome** — e.g., Pixel 8 / Galaxy A54 class: mobile UA of *Chrome* (matches the real Blink binary we launch), `isMobile=true`, `hasTouch=true`, matching viewport/screen, DPR ~2.7–3. This is the most internally-consistent combo available in Playwright.
- **iPhone preset (optional)** — noted caveat: claims Safari/WebKit UA while running Blink; fine for light checks, riskier under deep fingerprinting. Default off; per-account opt-in via `device_profile`.
- **Stable identity:** UA / timezone_id / locale / device profile are stored on the account and never re-randomized between sessions (randomizing identity is itself a bot tell). Fingerprint hash logged per session for audit.
- **tz ↔ proxy coherence rule (documented, operator's job):** account `timezone_id` should plausibly match the exit IP's region of its proxy; mismatch → warning in dashboard hints list (we can't geo-lookup IPs without an API in v1).

### 6.3 Network
- Per-account proxies: `http(s)://user:pass@host:port`, `socks5://…`; stored on account row, global fallback in `.env` (`DEFAULT_PROXY`). Residential/mobile strongly recommended for warming; datacenter IPs are the #1 shared tell across accounts.
- WebRTC leak mitigation via stealth patch (disable/mask RTC); socks5 preferred when available so DNS also exits through proxy.

### 6.4 Behavioral entropy (the biggest lever)
- **Session timing:** start time sampled inside daily window with a mild "natural hours" bias; duration randomized within configured range; ~10–20% of active days get only one short "quick check" session (~5 min) instead of the full one.
- **Non-uniform delays everywhere:** all waits/dwells/typing speeds sampled from skewed distributions (log-normal/gamma-ish), never fixed constants; jitter on scroll increments and tap positions (never exact element center).
- **Watch-time realism:** dwell time correlated with intent — videos we interact with are watched longer; occasional very short glances (<2 s) and occasional long watches (30–60 s + re-watch/scroll-back).
- **Idle bursts:** random "phone down" pauses 30 s–5 min mid-session; session ends abruptly sometimes ("picked up phone, saw something else") rather than always a clean finish.
- **Interaction realism:** likes/comments/saves/follows at low probabilities (defaults: like ~10–20%, comment ~2–5%, save ~3%, follow ~0–1% of viewed), comments use human typing speed with the occasional mid-typing pause; never act on every Nth video.
- **Ramp-up:** new accounts start at ~8–15 videos/day, 1 session/day; ramp schedule (configurable) raises volume over 2–4 weeks before "heavy" usage. This is the single most important anti-detection feature for fresh accounts.
- **Feed navigation variety:** mostly forward scroll; occasional back-scroll to rewatch, tap into a video's detail page then back (IG), profile peek on an author occasionally.

### 6.5 Bot-signal detection & response
Adapters scan the DOM each loop iteration for text/selector indicators:
- Captcha / slider challenge
- "Check your phone number" / "Confirm your identity" / login wall redirect
- "Unusual activity detected", rate-limit toasts, cookie-consent walls

On hit → `event_logs` row with `level='warning'`, `category='bot_signal'`, screenshot saved, session stopped with `stop_reason='bot_signal:<type>'`, account optionally auto-paused (`flagged`) per config (default: pause on 2 warnings in 48 h). Dashboard surfaces these prominently — **operator review is part of the design**, not an afterthought.

---

## 7. Selector Management Strategy

**Principle:** Python code references *logical keys* only; every concrete selector lives in `selectors/<platform>_selectors.json`. When TikTok/IG ship a UI change, the fix is a config edit (or a one-line JSON append), not a code release.

### 7.1 File schema (common to both platforms)

```jsonc
{
  "platform": "instagram",
  "schema_version": 2,
  "updated_at": "2026-09-11",
  "notes": "Starting points — verify with `warmer check-selectors` per account before relying on them.",

  "base_urls": {
    "reels": "https://www.instagram.com/reels/",
    "home":  "https://www.instagram.com/"
  },

  // Every functional group = ordered candidate list (try in order, first match wins)
  // + optional extra verification (text/aria hints). Selector engines: CSS default;
  // also support Playwright's text= / aria / role syntax inside candidates.
  "feed_container":    { "candidates": ["main", "[role='feed']", "div[role='application']"] },
  "scroll_target":     { "candidates": ["main", "html"] },          // element that receives scroll/touch gestures

  "video_card":        { "candidates": ["article[data-ig-root] [data-testid^='react-reel']", "article", "div[role='listitem']"] },
  "video_key_source":  { "url_pattern": "/reels/([A-Za-z0-9_-]+)", "note": "extract shortcode from active video URL" },

  "like_button":       { "candidates": ["button[data-testid='like']", "[aria-label*='Like' i]", "[data-icn-type='like']"] ,
                         "liked_state_indicator": {"candidates": ["[data-testid='unlike']", "[aria-label*='Unlike' i]"], "note": "detect already-liked to avoid unlike"} },
  "save_button":       { "candidates": ["button[data-testid='save']", "[aria-label*='Save' i]"] },

  "comment_open":      { "candidates": ["[data-testid='comment']","[aria-label*='Comment' i]"] },
  "comment_input":     { "candidates": ["[contenteditable][role='textbox']", "[placeholder*='Comment' i]", "textarea"] },
  "comment_send":      { "candidates": ["button[data-testid='comment_send_button']", "[aria-label*='Post' i]", "[aria-label*='Send' i]"] },

  // Ads: combine structural selectors with text patterns (OR logic)
  "ad_indicators": {
    "selectors": ["a[href*='/ad/']", "[data-icn-type='ads']", "[class*='sponsored' i]"],
    "text_patterns": ["Sponsored", "^Ad$", "Promoted"]
  },
  "ad_close_button": { "candidates": ["[aria-label*='Close' i]", "[data-testid^='story_ads_top_right']", "svg[data-svg='close']"] },

  // Session identity checks (run on open + periodically)
  "login_indicator":   { "url_contains": ["/accounts/login", "/accounts/logged_out"],
                         "selectors": ["[data-testid='login-redirect-button']", "[aria-label*='Log In' i]"] },
  "logged_in_indicator": { "selectors": ["[data-testid='side_nav_container']", "[aria-label*='Your profile' i]"] },

  // Bot-detection signals (text patterns + optional selectors) → warning events
  "bot_signals": {
    "captcha":            { "text_patterns": ["Enter your phone number", "unusual activity detected", "verify you are human"], "selectors": [] },
    "check_phone_email":  { "text_patterns": ["Check your phone number", "Confirm your identity", "Check your email"] , "selectors": [] },
    "rate_limit":         { "text_patterns": ["slow down", "limit reached"], "selectors": [] }
  }
}
```

TikTok file uses the same shape with its own candidates (e.g., `feed_container` → `[data-e2e='feed-list']`, cards → `[data-e2e='feed-item']`, like → `[data-e2e='like-button']`/`button[aria-label*='Like' i]`, comment input → `[contenteditable][role='textbox']` + send button, ad indicators incl. `div[data-e2e='ad-container']` and "Sponsored" text). *All concrete values are starting points to be validated in Step 2 via the smoke test — this is deliberate: we do not claim selector truth before seeing live pages.*

### 7.2 Resolution & health rules
1. **Ordered candidates** — loader tries each candidate (with a short timeout, e.g., 3 s) until one resolves; the *matched index* is recorded in `event_logs` (`category='selector'`, debug level).
2. **Drift alarm:** if a group's primary candidate fails and a fallback matches ≥ N times in M sessions (default: 5 of last 10), dashboard shows "selectors may be stale — update file"; repeated total failure → session error + account `error` status with the failing group named in `detail`.
3. **Text patterns** for ads/bot signals are case-insensitive regex, matched against visible text of cards/overlays only (scoped, not whole-page) to reduce false positives from nav chrome.
4. **Smoke test:** `warmer check-selectors --account <id>` opens the logged-in feed in that account's real profile and verifies every group resolves on live DOM; reports pass/fail per group with the matched candidate. Run after any selector-file edit, and after a suspected platform UI change.
5. **Versioning:** `schema_version` + `updated_at` + git history of the JSON = our changelog. No selector logic in code except engine support (CSS/text/aria) in `selector_loader.py`.

---

## 8. Risk List

### Bot detection / fingerprint
| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **Shared datacenter IP across accounts** — the classic multi-account tell | High (if operator is lazy) | High | Per-account residential/mobile proxies *required by design* (proxy field on account; dashboard flags accounts still on shared/default proxy); documented in README as top rule |
| R2 | TLS/HTTP2/JA3 fingerprinting beyond UA | Medium | Medium | Real Chrome channel first; stealth patches as floor; accept residual risk, monitor via bot-signal warnings |
| R3 | WebRTC leaks real IP past http proxy | Medium | High | socks5 preferred; stealth RTC patch; note in config comments |
| R4 | iPhone preset UA (Safari/WebKit) vs Blink engine mismatch under deep checks | Medium (if used) | Medium | Default preset = Android+Chrome; iPhone opt-in with documented caveat |
| R5 | Timezone ↔ exit-IP region mismatch | Medium | Medium | Stored per account, dashboard hint list; operator guidance |
| R6 | Behavior too uniform / volume ramp too aggressive for account age | High (config-dependent) | High | Distributions everywhere; ramp profiles as first-class config; caps + "quick check" sessions; defaults tuned conservative |
| R7 | Playwright/CDP runtime detection evolving | Medium | High | Real Chrome channel, stealth plugin *or* maintained init-script fallback in-repo (we control the patches), upgrade cadence noted in README |

### Login / session lifecycle
| # | Risk | Mitigation |
|---|---|---|
| R8 | First login triggers "new device" → phone/email verification | Manual one-time login is *designed* to be human-operated; `login` CLI waits indefinitely until selectors confirm logged-in state; account stays `needs_login` otherwise. Document: do the first login from the same proxy/IP you'll warm with. |
| R9 | Sessions expire / platform forces re-login after inactivity | Periodic `logged_in_indicator` check at session start + mid-session redirect detection → auto status `needs_login`, dashboard banner, no blind scrolling on a login wall |
| R10 | 2FA/backup-code flows vary per account | Out of scope v1 (manual); adapter just waits and verifies end-state |

### Ads & feed content
| # | Risk | Mitigation |
|---|---|---|
| R11 | Ad formats change (stories ads, carousel ads without close buttons) | Layered detection (selectors + text patterns), skip-method ladder: close button → min-watch fallback; every skip logged with method so new ad types are visible in stats (`ads_skipped` + `ad_skip_method`) |
| R12 | Ad false positives (creator labeled "sponsored" ≠ skippable) / false negatives counted as real content | Text patterns scoped to card overlays; conservative defaults; per-platform extra pattern lists in `config/<platform>.yaml` for tuning without touching JSON |

### Selectors & UI drift
| # | Risk | Mitigation |
|---|---|---|
| R13 | IG/TikTok ship breaking DOM changes (frequent!) | External JSON + ordered candidates + text/aria fallbacks; config-only fixes (§7) |
| R14 | **A/B-tested layouts** — selectors that work for one account fail on another | Multi-candidate lists, `check-selectors` smoke test per account before enabling it; drift alarms per group |
| R15 | Selector silently matches wrong element (e.g., "Like" hits share button) | State-verification after action where cheap (liked-state indicator, comment visibility check); mismatches logged as warnings |

### Rate limits & platform policy
| # | Risk | Mitigation |
|---|---|---|
| R16 | Soft daily/weekly caps on likes/comments ("unusual activity") | Per-account daily caps + ramp profiles; defaults deliberately low for new accounts; `category='limit'` warning logs when platform toasts appear |
| R17 | IP-level throttling when many accounts share one proxy | Operator guidance: distinct residential exit per account (same rule as R1); session backoff on repeated network warnings |
| R18 | Shadowban / temp restriction without explicit message | Hard to detect directly; proxies are: sudden drop in engagement + bot-signal history. Dashboard warning timeline helps operator correlate; pause-on-warnings setting limits blast radius |

### Operational
| # | Risk | Mitigation |
|---|---|---|
| R19 | playwright-extra lags Playwright releases (breaks on upgrade) | Pinned versions in `pyproject.toml`; in-repo init-script fallback (§6.1); upgrade = deliberate step with smoke tests |
| R20 | Unattended long runs accumulate small errors (network blips, stale pages) | Per-iteration error containment: retry scroll once → log + continue; session-level budget of tolerated errors before abort; structured `event_logs` for post-mortem |
| R21 | Machine sleep/lock mid-session kills "human" timing plausibly | Documented recommendation (disable sleep or run on always-on box); dashboard shows last heartbeat per running session so stalls are visible |

---

## 9. CLI Reference (planned commands)

```
warmer init                                   # create data/, logs/, copy .env.example, init DB schema
warmer accounts add --platform instagram --handle myhandle [--label "Brand IG"] \
                    [--tz America/New_York] [--locale en-US] [--device android-chrome-pixel8] \
                    [--proxy socks5://user:pass@host:port] [--window 07:30-23:00] \
                    [--ramp default] [--comment-tags food,travel] \
                    [--likes-per-day 12] [--comments-per-day 2] ...
warmer accounts list | show <id> | pause <id> | resume <id> | remove <id>
warmer login --account <id> [--timeout 900]   # open real browser on profile, human logs in, verify & persist state
warmer check-login --account <id>             # quick session-state probe
warmer run --all [--with-dashboard]           # start supervisor: scheduler loop (+ FastAPI dashboard)
warmer run --account <id> --now               # single warming session immediately (duration sampled per config)
warmer stop                                   # graceful stop of running sessions/supervisor (signal/pidfile)
warmer stats  [--account <id>] [--days 7]     # counters vs caps, warnings count, selector health
warmer logs   [--level warning] [--category bot_signal] [--account <id>] [--tail 50]
warmer check-selectors --account <id>         # live smoke test of all selector groups on that account's session
```

## 10. Configuration Reference (planned keys)

**`.env`** — secrets/paths only, no tuning:
`WARMER_DATA_DIR`, `WARMER_DB_PATH`, `LOG_LEVEL`, `DASHBOARD_HOST=127.0.0.1`, `DASHBOARD_PORT=8321`, `PLAYWRIGHT_CHANNEL=chrome|chromium|auto`, `HEADLESS=false`, `DEFAULT_PROXY=` (fallback only).

**`config/warmer.yaml`** — global tuning defaults:
- `defaults.window {start, end}`, `session_duration_s {min, max}`, `quick_check_probability`
- `behavior {like_p, comment_p, save_p, follow_p, idle_range_s [30,300], scroll_steps...}` (distributions + parameters)
- `caps.daily_default {videos, likes, comments, saves, follows}`
- `ramp_profiles.default[]`: rows of `{days_from, days_to, videos_per_session, like_p, comment_p, sessions_per_day}`
- `warnings.pause_after {count: 2, within_hours: 48}`, `screenshots_on_warning: true`

**`config/<platform>.yaml`** — per-platform extras (feed modes enabled, extra ad text patterns, scroll gesture params).

Per-account values in the DB override these defaults; dashboard "Settings" page can patch a subset live via `app_settings`.

**`app_settings` live overrides** — keys are dotted paths into the validated `warmer.yaml`
shape; a run picks them up on its next session (no restart). Writes are validated against
this whitelist + re-run through the config validators, so an invalid key/value is rejected
rather than silently ignored. Enforced by `core/settings_overrides.py`:

- `defaults.window.start|end`, `defaults.session_duration_s.min|max`, `defaults.quick_check_probability`
- `session.quick_check_duration_s`, `session.max_errors_before_abort`
- `behavior.like_p|comment_p|save_p|follow_p`
- `caps.daily_default.videos|likes|comments|saves|follows`
- `warnings.pause_after.count|within_hours`, `warnings.screenshots_on_warning`

**Enforcement notes (v1):**
- `caps.daily_default.videos` / `accounts.daily_video_cap` + `ramp_profiles[].videos_per_session`
  bound each session's non-ad video count (`scheduler.compute_video_budget`); a session that
  would start at 0 budget is skipped with `stop_reason='daily_video_cap_reached'`.
- `behavior.scroll.*` + `behavior.typing.*` + platform `scroll_gesture` reach the adapters via
  `GestureParams` (built in `behavior.build_gesture_params`) — no hardcoded gesture constants.
- `ramp_profiles[].sessions_per_day` splits the daily video cap across the day's sessions.

