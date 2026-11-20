"""Proxy URL parsing → Playwright dict + redaction for logs."""
from __future__ import annotations

import pytest

from account_warmer.browser.proxy import ProxyError, parse_proxy, redact_proxy


class TestParse:
    def test_socks5_with_auth(self):
        assert parse_proxy("socks5://user:pass@proxy.example:1080") == {
            "server": "proxy.example:1080",
            "username": "user",
            "password": "pass",
        }

    def test_http_without_auth(self):
        assert parse_proxy("http://host.local:3128") == {"server": "host.local:3128"}

    def test_empty_and_none_mean_no_proxy(self):
        assert parse_proxy(None) is None
        assert parse_proxy("") is None
        assert parse_proxy("   ") is None

    def test_unsupported_scheme(self):
        with pytest.raises(ProxyError, match="scheme"):
            parse_proxy("ftp://user:pass@host:21")

    def test_missing_port_rejected(self):
        """Playwright needs an explicit port — 'http://host' must fail loud."""
        with pytest.raises(ProxyError, match="port"):
            parse_proxy("http://host.local")


class TestRedact:
    def test_password_masked(self):
        assert redact_proxy("socks5://user:pass@proxy.example:1080") == (
            "socks5://user:***@proxy.example:1080"
        )

    def test_username_only_kept(self):
        assert redact_proxy("http://user@host:3128") == "http://user@host:3128"

    def test_none_and_unparseable(self):
        assert redact_proxy(None) is None
        assert redact_proxy("not a url at all") == "***"  # don't leak anything
