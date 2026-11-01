"""Compatibility entry point. Implementations live in the zhishi package."""
from zhishi.config import OWNER, ROOT
from zhishi.core.clock import deadline, now, stamp
from zhishi.service import Pilot
from zhishi.web.handler import Handler
from zhishi.runtime import main

__all__ = ["Pilot", "Handler", "ROOT", "OWNER", "now", "stamp", "deadline", "main"]

if __name__ == "__main__":
    main()
