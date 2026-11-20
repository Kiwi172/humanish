"""Device-fingerprint presets → concrete Playwright context options + audit hash (SPEC §6.2).

Design rules:
- **Primary preset = Android + Google Chrome** — a mobile *Chrome* UA matches the real
  Blink binary we launch (most internally-consistent combo available in Playwright).
- The iPhone preset is *flagged*: it claims Safari/WebKit while running Blink; opt-in only.
- Identity values (UA/tz/locale/profile) are stored per account and **never re-randomized**
  between sessions — randomizing identity is itself a bot tell (R4, SPEC §6.2).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DevicePreset:
    key: str
    user_agent: str
    viewport_width: int
    viewport_height: int
    device_scale_factor: float = 3.0
    is_mobile: bool = True
    has_touch: bool = True
    flagged: bool = False
    note: str = ""


FINGERPRINT_PRESETS: dict[str, DevicePreset] = {
    "android-chrome-pixel8": DevicePreset(
        key="android-chrome-pixel8",
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.6668.80 Mobile Safari/537.36"
        ),
        viewport_width=412,
        viewport_height=915,
        device_scale_factor=2.625,
    ),
    "android-galaxy-a54": DevicePreset(
        key="android-galaxy-a54",
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14; SM-S511B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.6668.80 Mobile Safari/537.36"
        ),
        viewport_width=360,
        viewport_height=780,
        device_scale_factor=3.0,
    ),
    # Flagged: Safari UA on a Blink engine (R4). Fine for light checks; riskier under deep
    # fingerprinting. Default off — per-account opt-in via accounts.device_profile.
    "iphone-15-safari": DevicePreset(
        key="iphone-15-safari",
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
        ),
        viewport_width=393,
        viewport_height=852,
        device_scale_factor=3.0,
        flagged=True,
        note="Safari/WebKit UA while running Blink — opt-in only (SPEC R4)",
    ),
}


def get_preset(name: str) -> DevicePreset:
    try:
        return FINGERPRINT_PRESETS[name]
    except KeyError:
        valid = ", ".join(sorted(FINGERPRINT_PRESETS))
        raise ValueError(f"unknown device_profile {name!r}; available: {valid}") from None


@dataclass(frozen=True)
class FingerprintOptions:
    """Concrete context options for one account session (injected into launch_persistent_context)."""

    user_agent: str
    timezone_id: str          # IANA — from the account row (stable identity!)
    locale: str               # BCP-47 — from the account row
    device_profile: str       # preset key used
    viewport: dict = field(default_factory=dict)   # {"width": .., "height": ..}
    screen: dict = field(default_factory=dict)     # same as viewport for phones (no desktop bar)
    device_scale_factor: float = 3.0
    is_mobile: bool = True
    has_touch: bool = True
    browser_channel: str = "chrome"   # real Chrome channel preferred; "chromium" fallback
    fingerprint_hash: str = ""        # sha1(ua|tz|locale|profile) — per-session audit (SPEC §2.15)


def build_fingerprint(account, *, channel: str = "chrome") -> FingerprintOptions:
    """Resolve an account row → :class:`FingerprintOptions`.

    ``account`` must expose ``timezone_id``, ``locale`` and ``device_profile`` attributes
    (the ORM model does; tests can pass simple objects).
    """
    preset = get_preset(account.device_profile)
    viewport = {"width": preset.viewport_width, "height": preset.viewport_height}
    identity = "|".join(
        [preset.user_agent, account.timezone_id, account.locale, account.device_profile]
    )
    return FingerprintOptions(
        user_agent=preset.user_agent,
        timezone_id=account.timezone_id,
        locale=account.locale,
        device_profile=account.device_profile,
        viewport=viewport,
        screen=dict(viewport),  # phones: screen == viewport
        device_scale_factor=preset.device_scale_factor,
        is_mobile=preset.is_mobile,
        has_touch=preset.has_touch,
        browser_channel=channel,
        fingerprint_hash=hashlib.sha1(identity.encode("utf-8")).hexdigest(),
    )
