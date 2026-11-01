"""Compatibility imports for the workflow coordinator and file helpers."""
from zhishi.workflows.service import Workflows
from zhishi.workflows.files import digest, encoded, scoped
from zhishi.core.clock import CN, timestamp
from zhishi.config import excel_node

NODE = excel_node()
__all__ = ["Workflows", "digest", "encoded", "scoped", "timestamp", "CN", "NODE"]
