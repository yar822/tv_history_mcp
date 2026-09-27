"""Brief startup progress, kept off the MCP stdio protocol stream."""
import sys


def startup_message(message: str) -> None:
    print(f"[tv-history startup] {message}", file=sys.stderr, flush=True)
