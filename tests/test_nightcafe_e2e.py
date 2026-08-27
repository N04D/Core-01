#!/usr/bin/env python3
"""Local Chromium integration test for the NightCafe navigation/prompt flow.

This deliberately uses a local fixture instead of NightCafe, Cloudflare, or real
credentials. It verifies the browser mechanics deterministically.
"""

from __future__ import annotations

import http.server
import threading
import unittest
from argparse import Namespace

from playwright.sync_api import sync_playwright

from plugins.media.nightcafe_automation import connect_cdp_with_retry, navigate_to_create_surface


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"""<!doctype html><html><body>
        <div class='community-support' style='position:fixed;inset:0;z-index:5'>
          <button aria-label='Close'>Close</button>
        </div>
        <main><button aria-label='Create'>Create</button></main>
        <script>
          document.querySelector('[aria-label=Close]').onclick=()=>document.querySelector('.community-support').remove();
          document.querySelector('[aria-label=Create]').onclick=()=>{
            document.querySelector('main').innerHTML='<textarea aria-label="Prompt" placeholder="Enter your prompt"></textarea>';
          };
        </script></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class NightCafeE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_real_chromium_handles_overlay_create_and_prompt(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            # Override the production home URL only for this local fixture.
            import os
            old = os.environ.get("NIGHTCAFE_HOME_URL")
            os.environ["NIGHTCAFE_HOME_URL"] = self.url
            try:
                navigate_to_create_surface(page, 5_000)
                self.assertTrue(page.get_by_role("textbox", name="Prompt").is_visible())
            finally:
                if old is None:
                    os.environ.pop("NIGHTCAFE_HOME_URL", None)
                else:
                    os.environ["NIGHTCAFE_HOME_URL"] = old
                browser.close()

    def test_cdp_retry_fails_with_clear_timeout(self):
        class Chromium:
            def connect_over_cdp(self, _url):
                raise ConnectionError("not listening")
        with self.assertRaises(TimeoutError):
            connect_cdp_with_retry(Chromium(), "http://127.0.0.1:1", 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
