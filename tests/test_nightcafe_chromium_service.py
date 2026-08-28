#!/usr/bin/env python3
"""Static validation for the NightCafe user service and CDP health scripts."""

from __future__ import annotations

import stat
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NightCafeChromiumServiceTests(unittest.TestCase):
    def test_unit_has_local_cdp_profile_and_health_gate(self) -> None:
        unit = (ROOT / "deploy/nightcafe-chromium.service").read_text(encoding="utf-8")
        self.assertIn('"--remote-debugging-port=${CDP_PORT}"', (ROOT / "scripts/start_nightcafe_chromium.sh").read_text())
        self.assertIn("/home/infra/dev/vault/chrome_profile", unit)
        self.assertIn("NIGHTCAFE_START_URL=https://creator.nightcafe.studio/", unit)
        self.assertIn("ExecStartPost=/home/infra/dev/scripts/check_nightcafe_chromium.sh", unit)
        self.assertIn("KillMode=control-group", unit)
        launcher = (ROOT / "scripts/start_nightcafe_chromium.sh").read_text(encoding="utf-8")
        self.assertIn("--enable-gpu", launcher)
        self.assertIn("--headless=new", launcher)
        self.assertIn("NIGHTCAFE_NO_SANDBOX", launcher)
        self.assertIn("https://creator.nightcafe.studio/", launcher)

    def test_scripts_are_executable_and_health_endpoint_is_local(self) -> None:
        for path in (ROOT / "scripts/start_nightcafe_chromium.sh", ROOT / "scripts/check_nightcafe_chromium.sh", ROOT / "deploy/install_nightcafe_chromium_user.sh"):
            self.assertTrue(path.stat().st_mode & stat.S_IXUSR, path)
        health = (ROOT / "scripts/check_nightcafe_chromium.sh").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1", health)
        self.assertIn("/json/version", health)


if __name__ == "__main__":
    unittest.main(verbosity=2)
