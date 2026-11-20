"""Service layer — business logic shared by CLI and API (SPEC §3).

Dependency rule (SPEC §4): services own orchestration and persistence; CLI/API are dumb wrappers.
Step 2 wires the thin, already-implemented paths (accounts CRUD, stats/logs queries); session
orchestration (login flow, warming sessions) lands in Step 3/4 behind these same entry points.
"""
