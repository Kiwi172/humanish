# STATE

```
STATE SNAPSHOT v8
Project: Instagram/TikTok Account Warmer
Current step completed: Step 8 - Spec-vs-code audit + gap closure (13 "declared but not wired" fixes)
Overall status: v1 complete; 132/132 tests green; 50/50 modules import clean; all CLI + dashboard endpoints verified
Completed (Step 8):
- Audit of SPEC.md vs the codebase found 13 gaps where a feature was specified/declared but not
  actually wired. All 13 fixed + covered by new tests (92 -> 132).
- 1. Rotating file logging: utils/logging_setup.init_logging() now called from the Typer root
  callback, create_app(), and Supervisor.run(). logs/warmer.log is created + written (was never called).
- 2. Daily video cap + ramp enforcement: scheduler.compute_video_budget() bounds each session's
  non-ad videos by min(ramp videos_per_session, remaining daily cap split across sessions_per_day);
  warmer_service skips a session with stop_reason='daily_video_cap_reached' before launching a browser.
  (Previously caps["videos"] was stripped and videos_per_session was never read.)
- 3. Live app_settings overrides: new core/settings_overrides.py applies whitelisted dotted-path
  overrides onto the validated WarmerConfig; services load via load_config_with_overrides().
  /settings PUT now validates key + value (rejects unknown keys / invalid / inconsistent values);
  /settings/schema + settings.html list the real overridable keys.
- 4. Dashboard stop button actually stops: sessions.stop_requested flag polled by the warming loop
  (graceful exit at next video); stalled sessions (no heartbeat) are closed directly.
- 5. Mid-session login-wall re-check (R9): loop re-runs check_login_state() on an interval, marks
  account needs_login + aborts. Also fixed a pre-existing fall-through bug where a pre-session login
  wall still opened the feed and ran the loop.
- 6. Scroll/typing config reaches adapters: adapters/base.GestureParams + adapters/scrolling.py
  (swipe / over-scroll-bounce / back-scroll-rewatch); behavior.build_gesture_params() maps
  behavior.scroll + behavior.typing + platform scroll_gesture. No hardcoded gesture/typing constants.
  rewatch_previous() added to the PlatformAdapter ABC + both impls + wired into the loop.
- 7. Topic-tagged comment pools per account (SPEC 2.10): accounts.comment_tags column (JSON list);
  pick_comment(tags=...) chooses among the account's pools; CLI --comment-tags validates against the pool file.
- 8. Dashboard browser-mode card (channel + fallback + headless) and Operator hints list
  (stats_service.compute_hints): R1 no-proxy, R17 shared proxy exit, R5 tz/proxy reminder,
  R21 stalled session, selector drift.
- 9. Session heartbeat (sessions.heartbeat_at) stamped each loop iteration; sessions page shows it
  + a stalled flag (R21).
- 10. text_pattern ad-skip method now reachable (skip_ad returns it when an ad matched by text only,
  no close button) - all three ad_skip_method CHECK values are used.
- 11. Supervisor plans each account once per local day (cached DayPlan) instead of re-sampling the
  start time every 30s tick; run.lock is now an flock single-instance guard (AlreadyRunningError).
- Schema: added sessions.heartbeat_at, sessions.stop_requested, accounts.comment_tags via a tiny
  idempotent ALTER-TABLE migration in core/db._ensure_columns (runs from make_engine, so no manual step).
- Config: WindowCfg now validates end > start (a reversed window would break the scheduler).
- Latent bug fixed: warmer_service._execute_action used session_scope/pick_comment that were only
  imported inside _run_one_session -> NameError on any comment action. Now imported in the function.
Files created/changed (Step 8):
- src/account_warmer/utils/logging_setup.py (init_logging)
- src/account_warmer/main.py (root callback enables logging)
- src/account_warmer/api/app.py (create_app logging; dashboard hints/browser mode; sessions heartbeat rows; settings schema)
- src/account_warmer/core/scheduler.py (compute_video_budget, SessionPlan.video_budget, plan args)
- src/account_warmer/core/settings_overrides.py (NEW)
- src/account_warmer/core/db.py (_ensure_columns migration)
- src/account_warmer/core/models.py (heartbeat_at, stop_requested, comment_tags)
- src/account_warmer/core/repositories.py (touch_heartbeat, request_stop, stop_requested, account_comment_tags, _decode_setting)
- src/account_warmer/core/behavior.py (build_gesture_params, pick_comment tags=)
- src/account_warmer/core/config.py (WindowCfg order validation)
- src/account_warmer/adapters/base.py (GestureParams, rewatch_previous, params injection)
- src/account_warmer/adapters/scrolling.py (NEW)
- src/account_warmer/adapters/instagram.py, tiktok.py (config-driven gestures, text_pattern skip, rewatch, _tap)
- src/account_warmer/services/warmer_service.py (budget/stop/heartbeat/login re-check, gesture params, _execute_action imports)
- src/account_warmer/services/supervisor.py (per-day plan cache, flock lock, config reload per tick)
- src/account_warmer/services/stats_service.py (browser_mode, compute_hints)
- src/account_warmer/api/routes_sessions.py (graceful vs stalled stop, heartbeat/stalled fields)
- src/account_warmer/api/routes_settings.py (key/value validation, /settings/schema)
- src/account_warmer/api/routes_accounts.py (comment_tags, load_config_with_overrides)
- src/account_warmer/api/templates/dashboard.html, sessions.html, settings.html (browser mode, hints, heartbeat, key dropdown)
- src/account_warmer/cli/accounts.py (--comment-tags), run.py (AlreadyRunningError), stats.py (overrides)
- tests/test_settings_overrides.py (NEW), tests/test_warmer_service.py (NEW), + expanded test_api/test_scheduler/test_behavior/test_repositories
- SPEC.md (schema columns, CLI --comment-tags, §10 override keys + enforcement notes, §3/§4 components + tests)
Key decisions (Step 8):
- Overrides are whitelisted dotted paths validated by re-validating the whole config, so a bad
  dashboard edit can never corrupt a run.
- Stop is cooperative (flag polled at video boundaries) so counters flush cleanly; only stalled
  sessions are force-closed.
- GestureParams keeps the SPEC §4 dependency rule: adapters receive config as data, never read files.
Known issues / risks:
- All concrete selector values remain unverified until live check-selectors per account (A/B layout risk, SPEC R14)
- playwright-extra still missing on this PyPI mirror -> stealth relies on init-script path unless [stealth] installs elsewhere
- No git repo initialized yet at project root; data/, logs/, .env, .venv/ gitignored
- Dashboard polling is coarse (30s); acceptable for v1 local tool
- R5 tz<->proxy coherence is a reminder list, not an automated geo verdict (no IP geo API in v1, as specified)
Next step:
- v1 is feature-complete + refined. Remaining work is purely operational:
  1. Run `warmer check-selectors --account <id>` against real IG/TikTok pages to validate/fix selector JSON values
  2. End-to-end test: add account -> login -> run one full warming session with real browser
  3. Optional: git init + first commit; add CI (pytest on push)
```

```
STATE SNAPSHOT v7
Project: Instagram/TikTok Account Warmer
Current step completed: Step 7 - Final audit & polish (module import fix, stats cleanup, test coverage expansion)
Overall status: v1 complete; 92/92 tests green; 48/48 modules import clean; all CLI + dashboard endpoints verified
Completed (Step 7):
- Module import check fixed: __main__.py now guards app() with `if __name__ == "__main__"` so pkgutil.walk_packages no longer triggers Typer's help output. Import check now reports cleanly: 48/48 modules OK.
- routes_stats.py cleanup: replaced `__import__("sqlalchemy")` hack in /stats/overview with proper top-level imports (func, Session).
- Test coverage expanded from 67 → 92 tests across two audit rounds:
  - Round 1: check-login CLI fix (was swallowing probe result); /settings-page HTML route + template; selector drift warnings on account detail API + HTML; test_api.py with 25 new tests (settings CRUD, drift positive/negative, session stop running/404/completed, all HTML pages render, accounts list/create/dup/status, stats overview/daily, logs).
  - Round 2: check-selectors CLI bug fix (base_urls dict access); README updated (Settings page, drift warnings, stop button, test count).
- All endpoints verified via TestClient; all routes registered.
Files created/changed (Step 7):
- src/account_warmer/__main__.py (guard app() with __name__ check)
- src/account_warmer/api/routes_stats.py (proper imports for /stats/overview)
- src/account_warmer/cli/login.py (check-login now displays probe result green/red)
- src/account_warmer/api/app.py (added /settings-page HTML route)
- src/account_warmer/api/templates/settings.html (NEW: settings CRUD UI)
- src/account_warmer/api/templates/base.html (Settings link in nav)
- src/account_warmer/api/routes_accounts.py (selector_drift_warnings in account detail response)
- src/account_warmer/api/templates/account_detail.html (drift warning banner + JS)
- src/account_warmer/cli/check_selectors.py (fixed base_urls dict access bug)
- tests/test_api.py (NEW: 25 tests covering settings, drift, session stop, HTML pages, accounts/stats/logs APIs)
- README.md (updated dashboard section, test count 92)
Key decisions (Step 7):
- __main__.py guard pattern keeps `python -m account_warmer` working as CLI while allowing safe import during module walks
- Stats overview uses direct model imports rather than dynamic __import__ for clarity and type-checker friendliness
Known issues / risks:
- All concrete selector values remain unverified until live check-selectors per account (A/B layout risk, SPEC R14)
- playwright-extra still missing on this PyPI mirror → stealth relies on init-script path unless [stealth] installs elsewhere
- No git repo initialized yet at project root; data/, logs/, .env, .venv/ gitignored
- Dashboard polling is coarse (30s); acceptable for v1 local tool
Next step:
- v1 is feature-complete + refined. Remaining work is purely operational:
  1. Run `warmer check-selectors --account <id>` against real IG/TikTok pages to validate/fix selector JSON values
  2. End-to-end test: add account → login → run one full warming session with real browser
  3. Optional: git init + first commit; add CI (pytest on push)
```
