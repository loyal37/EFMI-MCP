"""Runs every check against the sandbox. Use after rebuilding, or after XXMI was updated.

  1. refresh sandbox/run from the EFMI folder (a private copy; the original is only read)
  2. parity with XXMI's own frame analysis, in three load orders
  3. functional self-test of the probe
  4. multiple-device regression (with and without the private XXMI runtime)
  5. end-to-end test of the MCP server
  6. reload workflow/input unit tests (fake input; real F10 test is opt-in)
  7. model workflow unit tests (no game, Blender or LoyalTools; model_offline_integration.py is opt-in)
  8. mods switch unit tests (fake probe; the real F11 test mods_toggle_integration_test.py is opt-in)
  9. draw output and pipeline state unit tests (fake probe)
 10. contents: what each call saw, against bytes the sandbox is known to have written; byte for byte
     against XXMI's own frame analysis of the same calls; and the tools on top (fake probe)
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
        ("contents: what each call saw", ["sandbox/contents_test.py"]),
        ("contents: byte for byte against XXMI's frame analysis", ["sandbox/contents_parity_test.py"]),
        ("multiple-device regression", ["sandbox/multidevice_test.py"]),
        ("multiple-device regression with XXMI", ["sandbox/multidevice_test.py", "--xxmi"]),
        ("MCP server end to end", ["sandbox/mcp_test.py"]),
        ("reload input guards (no OS input)", ["sandbox/reload_input_test.py"]),
        ("reload workflow MCP contract (fake probe/input)", ["sandbox/reload_workflow_test.py"]),
        ("model workflow logic (export checks, deployment, draw comparison, jobs)", ["sandbox/model_workflow_test.py"]),
        ("mods switch MCP contract (fake probe)", ["sandbox/mods_toggle_test.py"]),
        ("draw outputs and pipeline state (fake probe)", ["sandbox/draw_outputs_test.py"]),
        ("contents: slots, tools and the folder writer (fake probe)", ["sandbox/contents_unit_test.py"]),
        ("texture slots, shader evidence and memory cache (fake probe)", ["sandbox/texture_slots_test.py"]),
    ]
    results = [(title, run(title, args)) for title, args in steps]
    print("\n======== summary")
    for title, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {title}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
