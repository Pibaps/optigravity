# Cross-platform task runner for Optigravity V3.5 (Windows PowerShell + Linux/macOS)
set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

# Default: incremental local loop via pytest-testmon
default: dev

# Run only tests impacted by recent edits + previous failures
dev:
    uv run -m pytest --testmon -q --tb=short

# Run headless TUI Pilot + Fake ACP Agent stdio suite
tui:
    uv run -m pytest tests/test_tui_app.py tests/test_acp_client.py -q --tb=short

# Run V3.1 isolation, OGY_ACTIVE, and backup/restore contracts
v31:
    uv run -m pytest -k "isolated or backup or restore or ogy_active" -q --tb=short

# Regenerate Textual SVG snapshots (authoritative on Linux / ubuntu-latest)
snap-update:
    uv run -m pytest -m snapshot --snapshot-update

# Run full parallel test suite with 30s per-test timeout
all:
    uv run -m pytest -n auto --timeout=30 -q --tb=short
