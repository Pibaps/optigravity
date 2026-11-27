"""Deterministic Fake `agy` CLI used by E2E TTY tests (`tests/test_e2e_tty.py`, Lot 3.5.7).

Records:
- stdin/stdout/stderr `isatty()` status
- V3.1 env vars (`OGY_ACTIVE`, `OGY_TASK_ID`, `AGY_CLI_DISABLE_AUTO_UPDATE`,
  `OGY_GEMINI_HOME`, `OGY_REAL_HOME`, `USERPROFILE`, `HOME`, `OGY_DB_PATH`)
- CLI arguments (`sys.argv[1:]`, including `--settings`)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    argv = sys.argv[1:]
    settings_val: str | None = None
    if "--settings" in argv:
        idx = argv.index("--settings")
        if idx + 1 < len(argv):
            settings_val = argv[idx + 1]

    report = {
        "stdin_isatty": bool(sys.stdin.isatty()),
        "stdout_isatty": bool(sys.stdout.isatty()),
        "stderr_isatty": bool(sys.stderr.isatty()),
        "argv": argv,
        "has_settings": "--settings" in argv,
        "settings_path": settings_val,
        "OGY_ACTIVE": os.environ.get("OGY_ACTIVE"),
        "OGY_TASK_ID": os.environ.get("OGY_TASK_ID"),
        "AGY_CLI_DISABLE_AUTO_UPDATE": os.environ.get("AGY_CLI_DISABLE_AUTO_UPDATE"),
        "OGY_GEMINI_HOME": os.environ.get("OGY_GEMINI_HOME"),
        "OGY_REAL_HOME": os.environ.get("OGY_REAL_HOME"),
        "USERPROFILE": os.environ.get("USERPROFILE"),
        "HOME": os.environ.get("HOME"),
        "OGY_DB_PATH": os.environ.get("OGY_DB_PATH"),
    }

    out_file = os.environ.get("FAKE_AGY_OUT_FILE", "").strip()
    if out_file:
        Path(out_file).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print("FAKE_AGY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
