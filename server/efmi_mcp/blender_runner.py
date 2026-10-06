"""Runs a script in background Blender, away from the MCP server's own stdio.

Blender prints a lot; all of it goes to a log file. A script reports back
through ``NAME=value`` lines, which are returned as markers.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

# The add-on is installed for this Blender on the machine the project was built on.
KNOWN_BLENDER_PATHS = (r"D:\blender4,5\blender.exe",)
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


class BlenderError(RuntimeError):
    pass


class BlenderCancelled(BlenderError):
    pass


def find_blender(explicit: str | Path = "") -> Path:
    """The Blender to use: as given, else EFMI_BLENDER, else a known install.

    A Blender that was named has to exist: another install is never used in
    its place, since it may carry a different version of the add-on.
    """
    named = explicit or os.environ.get("EFMI_BLENDER", "")
    if named:
        if Path(named).is_file():
            return Path(named)
        raise BlenderError(f"Blender executable not found: {named}")
    for candidate in KNOWN_BLENDER_PATHS:
        if Path(candidate).is_file():
            return Path(candidate)
    raise BlenderError("Blender was not found. Pass blender_path or set EFMI_BLENDER to the blender.exe "
                       "that has the LoyalTools add-on installed.")


_VERSIONS: dict[str, str] = {}


def blender_version(blender: Path) -> str:
    """'4.5' for a Blender 4.5.x executable; '' when it cannot be asked."""
    key = str(blender)
    if key not in _VERSIONS:
        try:
            output = subprocess.run([key, "--version"], capture_output=True, text=True, timeout=120,
                                    stdin=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except (OSError, subprocess.SubprocessError):
            output = ""
        match = re.search(r"Blender (\d+)\.(\d+)", output)
        _VERSIONS[key] = f"{match.group(1)}.{match.group(2)}" if match else ""
    return _VERSIONS[key]


def find_addon(blender: Path, module: str = "LoyalTools") -> Path | None:
    """The folder of an add-on as installed for this Blender, or None.

    This is the copy Blender itself loads: using it outside Blender as well
    keeps every step on the version the user has installed.
    """
    version = blender_version(blender)
    if not version:
        return None
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        user = Path(appdata) / "Blender Foundation" / "Blender" / version
        candidates.append(user / "scripts" / "addons" / module)
        if (user / "extensions").is_dir():
            candidates += sorted((user / "extensions").glob("*/" + module))
    bundled = Path(blender).parent / version / "scripts"
    candidates += [bundled / "addons" / module, bundled / "addons_core" / module]
    return next((path for path in candidates if (path / "__init__.py").is_file()), None)


def run_script(blender: Path, script: Path, arguments: list[str], log_path: Path,
               timeout_s: float = 900, cancelled=None, on_process=None) -> dict:
    """Run ``script`` with ``arguments``; returns exit code, markers and timing.

    ``cancelled()`` is polled; when it turns true the process is stopped and
    BlenderCancelled is raised. ``on_process(proc)`` receives the process so a
    job can be cancelled from another thread.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [str(blender), "--background", "--factory-startup", "--python-exit-code", "1",
               "--python", str(script), "--", *map(str, arguments)]
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    started = time.monotonic()
    with open(log_path, "wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if on_process:
            on_process(process)
        try:
            while True:
                try:
                    code = process.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if cancelled and cancelled():
                        raise BlenderCancelled("cancelled while Blender was running")
                    if time.monotonic() - started > timeout_s:
                        raise BlenderError(f"Blender did not finish within {timeout_s:.0f} s; see {log_path}")
        except BaseException:
            process.kill()
            process.wait(timeout=10)
            raise
        finally:
            if on_process:
                on_process(None)
    markers = {}
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.isupper() and name.replace("_", "").isalpha():
            markers[name] = value.strip()
    return {"exit_code": code, "markers": markers, "seconds": round(time.monotonic() - started, 2),
            "log": str(log_path), "command": command}


def tail(log_path: str | Path, lines: int = 25) -> list[str]:
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()][-lines:]
