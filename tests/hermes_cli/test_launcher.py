"""Tests for the top-level `./hermes` launcher script."""

from pathlib import Path


def test_launcher_delegates_to_argparse_entrypoint():
    """`./hermes` should use the argparse entrypoint, not the legacy Fire wrapper."""
    launcher_path = Path(__file__).resolve().parents[2] / "hermes"
    launcher = launcher_path.read_text(encoding="utf-8")

    assert launcher.startswith("#!/bin/sh")
    assert 'exec "$PYTHON" -m hermes_cli.main "$@"' in launcher
    assert "fire" not in launcher
    assert "cli.py" not in launcher
