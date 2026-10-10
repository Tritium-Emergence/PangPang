# -*- coding: utf-8 -*-
"""web_search provider chain (1.0 keyless fallback): Tavily preferred when its key
is set, then keyless Bing (cn.bing.com) and DuckDuckGo, mutually redundant. The
fixtures below double as the parser regression anchor: a provider markup change
must fail here first, not silently in production."""
import base64
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang.service import Service

_B64 = base64.urlsafe_b64encode(b"https://example.com/a").decode().rstrip("=")
BING_PAGE = (
    "<html><body>"
    '<li class="b_algo b_algoBigWiki"><h2>'
    '<a href="https://cn.bing.com/ck/a?!&&amp;p=x&u=a1' + _B64 + '&ntzb=1">Title <b>One</b></a>'
    '</h2><p class="b_lineclamp">Snippet <b>one</b> text</p></li>'
    '<li class="b_algo"><h2><a href="https://direct.example/b">Title Two</a></h2>'
    '<p>Snippet two</p></li>'
    "</body></html>").encode()
DDG_PAGE = (
    "<html><body>"
    '<div class="result results_links">'
    '<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fd&rut=x">DDG <b>One</b></a>'
    '<a class="result__snippet" href="z">Snippet <b>ddg</b></a></div>'
    '<div class="result results_links">'
    '<a class="result__a" href="https://example.com/e">DDG Two</a>'
    '<a class="result__snippet" href="z">Snippet e</a></div>'
    "</body></html>").encode()
TAVILY_BODY = json.dumps({"answer": "42", "results": [
    {"title": "t", "url": "https://t.example", "content": "c"}]}).encode()


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def read(self, n=-1):
        return self._data if n is None or n < 0 or n >= len(self._data) else self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ProviderChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _fake_urlopen(self, routes):
        calls = []

        def fake(req, timeout=None):
            calls.append(req.full_url)
            for needle, action in routes:
                if needle in req.full_url:
                    if isinstance(action, Exception):
                        raise action
                    return FakeResponse(action)
            raise AssertionError("unexpected url " + req.full_url)

        return fake, calls

    def _search(self, routes, env=None, query="测试 查询"):
        fake, calls = self._fake_urlopen(routes)
        base = {k: v for k, v in os.environ.items()
                if k not in {"TAVILY_API_KEY", "PANGPANG_SEARCH_PROVIDER"}}
        base.update(env or {})
        with patch.dict(os.environ, base, clear=True), patch("urllib.request.urlopen", fake):
            return self.app.web_search(query), calls

    def test_no_key_bing_first_and_only_when_it_succeeds(self):
        result, calls = self._search([("cn.bing.com", BING_PAGE)])
        self.assertEqual(result["provider"], "bing")
        self.assertEqual(result["results"][0]["url"], "https://example.com/a")  # /ck/a decoded
        self.assertEqual(result["results"][0]["title"], "Title One")
        self.assertEqual(result["results"][1]["url"], "https://direct.example/b")
        self.assertIn("notice", result)
        self.assertEqual(len(calls), 1)  # ddg not tried

    def test_bing_failure_falls_to_duckduckgo(self):
        result, calls = self._search([("cn.bing.com", urllib.error.URLError("blocked")),
                                      ("duckduckgo", DDG_PAGE)])
        self.assertEqual(result["provider"], "duckduckgo")
        self.assertEqual(result["results"][0]["url"], "https://example.com/d")  # uddg decoded
        self.assertEqual(len(calls), 2)

    def test_bing_empty_page_falls_through(self):
        result, calls = self._search([("cn.bing.com", b"<html><body>consent wall</body></html>"),
                                      ("duckduckgo", DDG_PAGE)])
        self.assertEqual(result["provider"], "duckduckgo")
        self.assertEqual(len(calls), 2)

    def test_all_fail_is_structured_error(self):
        result, _ = self._search([("cn.bing.com", urllib.error.URLError("dns")),
                                  ("duckduckgo", TimeoutError("slow"))])
        self.assertEqual(result["error"], "search_unavailable")
        self.assertIn("bing: ", result["detail"])
        self.assertIn("dns", result["detail"])
        self.assertIn("duckduckgo: slow", result["detail"])

    def test_tavily_preferred_when_key_set(self):
        result, calls = self._search([("api.tavily.com", TAVILY_BODY)],
                                     env={"TAVILY_API_KEY": "tvly-test"})
        self.assertEqual(result["provider"], "tavily")
        self.assertEqual(result["answer"], "42")
        self.assertEqual(len(calls), 1)

    def test_tavily_failure_falls_to_bing(self):
        result, calls = self._search([("api.tavily.com", urllib.error.HTTPError(
                                          "https://api.tavily.com/search", 429, "rate", None, None)),
                                      ("cn.bing.com", BING_PAGE)],
                                     env={"TAVILY_API_KEY": "tvly-test"})
        self.assertEqual(result["provider"], "bing")
        self.assertEqual(len(calls), 2)

    def test_forced_provider_pins_the_chain(self):
        result, calls = self._search([("duckduckgo", DDG_PAGE)],
                                     env={"PANGPANG_SEARCH_PROVIDER": "duckduckgo"})
        self.assertEqual(result["provider"], "duckduckgo")
        self.assertEqual(len(calls), 1)

    def test_off_disables_keyless_egress(self):
        result, calls = self._search([], env={"PANGPANG_SEARCH_PROVIDER": "off"})
        self.assertEqual(result["error"], "search_unavailable")
        self.assertEqual(calls, [])

    def test_empty_query(self):
        self.assertEqual(self.app.web_search("  ")["error"], "empty_query")


if __name__ == "__main__":
    unittest.main()
