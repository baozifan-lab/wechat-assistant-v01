"""Shared project paths and environment-backed configuration."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "web"
DATA_ROOT = ROOT / "data"
OWNER = "pilot-owner"


def model_aliases():
    return [value.strip() for value in os.environ.get("MODEL_ALIASES", "reminder-fast,planner-strong").split(",") if value.strip()]


def excel_node():
    default = Path.home()/".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    return Path(os.environ.get("EXCEL_WORKER_NODE", str(default)))
