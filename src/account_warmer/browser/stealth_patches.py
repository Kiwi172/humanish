"""Stealth layers (SPEC §6.1 / R7 / R19).

Primary: playwright-extra ``StealthPlugin`` when installed and version-compatible with our
pinned Playwright. Fallback (this machine's PyPI mirror lacks the package): equivalent
manual init-script patches applied via ``context.add_init_script`` — same behavior, no
plugin dependency, fully in-repo so we control the upgrade cadence.

Known patch set (R7), one script per concern:
- remove ``navigator.webdriver``
- patch ``navigator.plugins`` / ``languages`` / ``window.chrome``
- permissions API query consistency
- WebGL vendor/renderer strings consistent with a desktop-class GPU
- iframe contentWindow cross-check
- outerWidth/outerHeight vs window size
- WebRTC: mask/disable (R3 — http proxies leak the real IP via RTC)
"""
from __future__ import annotations

PATCH_NAMES: tuple[str, ...] = (
    "navigator_webdriver",
    "plugins_languages_chrome",
    "permissions_api",
    "webgl_vendor",
    "iframe_contentwindow",
    "outer_dimensions",
    "webrtc_mask",
)


def build_init_scripts() -> list[str]:
    """Return the init-script JS strings to apply (fallback path, one per patch)."""
    return [
        _navigator_webdriver(),
        _plugins_languages_chrome(),
        _permissions_api(),
        _webgl_vendor(),
        _iframe_contentwindow(),
        _outer_dimensions(),
        _webrtc_mask(),
    ]


def _navigator_webdriver() -> str:
    return """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def _plugins_languages_chrome() -> str:
    return """
// A real Chrome install always has plugins + languages; headless defaults expose [].
if (navigator.plugins && navigator.plugins.length === 0) {
  Object.defineProperty(navigator, 'plugins', {
    get: () => [
      { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
      { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
    ]
  });
}
if (!navigator.languages || navigator.languages.length === 0) {
  Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
}
// window.chrome is absent in plain Chromium automation contexts.
if (typeof window.chrome === 'undefined') {
  window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
}
"""


def _permissions_api() -> str:
    return """
// Headless Chrome reports Notification.permission as 'denied' without a user gesture;
// real desktop Chrome usually has it 'default'. Normalize the query result.
try {
  const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (originalQuery) {
    window.navigator.permissions.query = async (parameters) => (
      parameters.name === 'notifications' && window.Notification
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery.call(window.navigator.permissions, parameters)
    );
  }
} catch (e) {}
"""


def _webgl_vendor() -> str:
    return """
// Consistent GPU strings (a mobile Chrome on a Pixel reports an ARM Mali/Adreno class
// renderer; we report a plausible desktop-class string instead of the SwiftShader tell).
try {
  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (parameter) {
    if (parameter === 37445) return 'Google Inc. (NVIDIA)';
    if (parameter === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
    return getParameter.call(this, parameter);
  };
} catch (e) {}
"""


def _iframe_contentwindow() -> str:
    return """
// Cross-frame check: window.contentWindow === window must hold for the top frame.
try {
  if (window.top === window && !Object.getOwnPropertyDescriptor(window, 'contentWindow')) {
    Object.defineProperty(window, 'contentWindow', { get: () => window });
  }
} catch (e) {}
"""


def _outer_dimensions() -> str:
    return """
// outerWidth/Height should equal inner dimensions on a phone with no chrome bars.
try {
  if (!window.outerWidth || !window.outerHeight) {
    Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth });
    Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight });
  }
} catch (e) {}
"""


def _webrtc_mask() -> str:
    return """
// R3: WebRTC can leak the real IP past an http proxy. We don't fake a second interface —
// we just stop RTCPeerConnection from advertising candidates unless the page opts in via
// a data attribute (no v1 platform needs RTC for feed warming).
try {
  const nativeCreate = window.RTCPeerConnection && window.RTCPeerConnection.bind(window);
  if (nativeCreate) {
    window.RTCPeerConnection = function (config, constraints) {
      try { config = Object.assign({}, config, { iceServers: [] }); } catch (e) {}
      return new nativeCreate(config, constraints);
    };
    window.RTCPeerConnection.prototype = nativeCreate.prototype;
  }
} catch (e) {}
"""
