"""Run native Qt tests in a bounded subprocess, using the project's Qt install."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    artifacts = root / ".test-artifacts"
    artifacts.mkdir(exist_ok=True)
    env = os.environ.copy()
    # Do not mix Binary Ninja's Qt libraries/plugins with the test environment.
    for name in ("PYTHONPATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        env.pop(name, None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    process = subprocess.Popen(
        [sys.executable, "-m", "pytest", "--basetemp", str(artifacts / "pytest"),
         *sys.argv[1:]],
        cwd=root,
        env=env,
        start_new_session=os.name != "nt",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate(timeout=5)
        print(output, end="")
        print("Tests exceeded the 90-second watchdog; the test process was terminated.")
        return 124
    print(output, end="")
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
