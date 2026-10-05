"""Starts the EFMI MCP server (stdio). Point an MCP host at this file:

    python "F:\\EFMI MCP\\server\\run_server.py" [--efmi-root PATH] [--out PATH]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from efmi_mcp.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
