"""Local-only container health probe; namespace follows the loaded deployment config."""

import sys
from pathlib import Path

from .config import load_settings
from .observability import read_status


def main():
    try:
        settings = load_settings(Path(sys.argv[1] if len(sys.argv) > 1 else "/app/config.toml"))
        healthy = read_status(settings.data_dir, settings.namespace)["healthy"]
    except (OSError, ValueError, KeyError, TypeError):
        healthy = False
    raise SystemExit(0 if healthy else 1)
