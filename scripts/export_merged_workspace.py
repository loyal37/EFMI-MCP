"""Command-line entry point for the offline LoyalTools bridge."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from efmi_mcp.loyal_bridge import main

if __name__ == "__main__":
    raise SystemExit(main())
