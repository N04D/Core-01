from __future__ import annotations
import subprocess
import sys

def test_doctor_reports_required_sections():
    result = subprocess.run([sys.executable, "scripts/deployment_doctor.py", "--db", "/tmp/does-not-exist.db"], capture_output=True, text=True, check=True)
    for label in ("architecture:", "python:", "CORE_HOME:", "CORE_DATA:", "database:", "worker:", "GPU (optional):", "local LLM (optional):", "Playwright (optional):"):
        assert label in result.stdout
