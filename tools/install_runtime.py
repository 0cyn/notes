"""Run with Binary Ninja's Python to install dependencies inside this checkout."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from src.markdown import runtime_directory

    target = runtime_directory()
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
         "--no-cache-dir", "--upgrade", "--target", str(target),
         "-r", str(root / "requirements.txt")],
        check=True,
    )
    print(f"Installed runtime dependencies in {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
