"""Runs every check against the sandbox. Use after rebuilding, or after XXMI was updated.

  1. refresh sandbox/run from the EFMI folder (a private copy; the original is only read)
  2. parity with XXMI's own frame analysis, in three load orders
  3. functional self-test of the probe
  4. multiple-device regression (with and without the private XXMI runtime)
  5. end-to-end test of the MCP server
  6. reload workflow/input unit tests (fake input; real F10 test is opt-in)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(title: str, args: list[str]) -> bool:
    print(f"\n######## {title}")
    r = subprocess.run([sys.executable] + args, cwd=str(HERE.parent))
    return r.returncode == 0


def main() -> int:
    extra = sys.argv[1:]
    steps = [
        ("refresh the sandbox copy of the XXMI runtime", ["sandbox/prepare_run.py"] + extra),
        ("parity with XXMI (XXMI loaded first)", ["sandbox/parity_test.py"]),
        ("parity with XXMI (probe loaded first)", ["sandbox/parity_test.py", "--probe-first"]),
        ("parity with XXMI (system d3d11.dll already loaded)", ["sandbox/parity_test.py", "--preload-runtime"]),
        ("probe self-test", ["sandbox/selftest.py"]),
        ("multiple-device regression", ["sandbox/multidevice_test.py"]),
        ("multiple-device regression with XXMI", ["sandbox/multidevice_test.py", "--xxmi"]),
        ("MCP server end to end", ["sandbox/mcp_test.py"]),
        ("reload input guards (no OS input)", ["sandbox/reload_input_test.py"]),
        ("reload workflow MCP contract (fake probe/input)", ["sandbox/reload_workflow_test.py"]),
    ]
    results = [(title, run(title, args)) for title, args in steps]
    print("\n======== summary")
    for title, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {title}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
