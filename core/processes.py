#!/usr/bin/env python3
"""Process-group lifecycle helpers for plugins, browsers, and local models."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Sequence
from typing import Any


def popen_process_group(command: Sequence[str], **kwargs: Any) -> subprocess.Popen[str]:
    """Start a subprocess as leader of an isolated POSIX process group."""
    if os.name != "posix":
        raise RuntimeError("process-group isolation requires a POSIX platform")
    if "preexec_fn" in kwargs or "start_new_session" in kwargs:
        raise ValueError("process-group settings are managed centrally")
    return subprocess.Popen(command, preexec_fn=os.setsid, **kwargs)


def terminate_process_group(
    process: subprocess.Popen[str], grace_seconds: float = 5.0
) -> None:
    """Terminate a process group and escalate to SIGKILL after a grace period."""
    if grace_seconds < 0:
        raise ValueError("grace_seconds must be non-negative")
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=max(1.0, grace_seconds))
