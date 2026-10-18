"""Does the probe fill its slot cache by itself? (probe 0.1.7, records keyed by content from 0.1.8)

While the game runs, the probe looks at one frame in so many, records the indexed draws it has not
seen before (index buffer region hash, range, shaders, what sits in the vertex and pixel shader
resource slots, output formats) and appends them to draws.jsonl in a folder per game version next
to the DLL, together with the bytecode of their shaders. A record stands for a mesh by its content
(index buffer region hash and count) with its shaders and slot shapes, wherever the game placed the
mesh: two of the sandbox's draws draw the same index data with the same shaders and share a record.

With XXMI loaded, as in the game:
  - by default only skinned draws are recorded; the sandbox draws nothing skinned and XXMI's overlay
    (the notification text) is not skinned either, so nothing is recorded;
  - with skinned_only off, every indexed draw of the sandbox in an ordinary full capture has a
    record that agrees with it: index buffer hash, range, base vertex, instances, shaders, layout,
    the slots of the shader resources with their kind, size and format, and the output formats; the
    folder has version.json, no key twice, and the bytecode of each shader, whose 3Dmigoto hash is
    the shader hash. The overlay is recorded too: its quads come from a ring buffer at a different
    place in most frames and XXMI recreates its buffers, which is why the game's characters (drawn
    with BLENDINDICES in their input layout) are what is recorded by default.
Without XXMI, where every draw is the sandbox's own and the counts are exact:
  - each draw is recorded once, and frames looked at later bring nothing back, not even as a
    duplicate that the writer drops;
  - another folder (with max_new=1, so new draws are spread over several frames) receives the same
    records, and going back to the first one adds nothing to it;
  - after a restart the records already in the folder are recognised, nothing is written again, and
    a line the last session left unfinished does not run into the next one; records of the older
    format (probe 0.1.7, no "fmt") are moved to draws-fmt1.jsonl instead of being mixed in.

Only folders this test creates next to the probe DLL are written, and they are removed at the end;
no key is pressed.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp.hashes import fnv64  # noqa: E402
from efmi_mcp.probe_client import ProbeClient, ProbeError  # noqa: E402
from contents_test import connect  # noqa: E402
from selftest import PROBE, Sandbox  # noqa: E402

SUB_X = "slot-cache-test-xxmi"
SUB_A = "slot-cache-test-a"
SUB_B = "slot-cache-test-b"
CUT = '{"frame":0,"key":"cut-off'

failures: list[str] = []
passed = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name} {detail[:700]}")
    return bool(cond)


def status(client: ProbeClient) -> dict:
    reply, _ = client.request("status")
    return reply["slot_cache"]


def configure(client: ProbeClient, **fields) -> dict:
    reply, _ = client.request("slot_cache", **fields)
    return reply


def wait_looked_at(client: ProbeClient, count: int, timeout: float = 15.0) -> dict:
    """Waits until `count` more frames have been looked at, then until everything is on disk."""
    start = status(client)["frames_looked_at"]
    deadline = time.time() + timeout
    st = status(client)
    while st["frames_looked_at"] < start + count and time.time() < deadline:
        time.sleep(0.2)
        st = status(client)
    return configure(client, flush=True, timeout_ms=5000)["slot_cache"]


def frozen(client: ProbeClient) -> dict:
    """Stops looking at frames and waits until what was looked at is on disk, so that the status and
    the files can be compared. What was seen is kept."""
    configure(client, enable=False)
    time.sleep(0.5)                 # frames recorded before are finished within a few frames
    return configure(client, flush=True, timeout_ms=5000)["slot_cache"]


def records(folder: Path) -> list[dict]:
    out = []
    path = folder / "draws.jsonl"
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and line != CUT:
            out.append(json.loads(line))
    return out


def keys(recs: list[dict], origin: str | None = None) -> set[str]:
    return {r["key"] for r in recs if origin is None or r["origin"] == origin}


def shader_files(folder: Path) -> set[str]:
    return {p.name for p in (folder / "shaders").glob("*.bin")}


def folder_of(st: dict) -> Path:
    return Path(st["folder"])


def start(xxmi: bool) -> tuple[Sandbox, ProbeClient | None]:
    sb = Sandbox(["--seconds", "90"], xxmi=xxmi)
    if not check("the sandbox started", sb.ready.wait(30)):
        return sb, None
    client = connect(sb.pid)
    check("connected to the probe", client is not None)
    return sb, client


def draw_identity(origin: str, ib: str | None, count: int, vs: str | None, ps: str | None) -> tuple:
    """What a record stands for: a mesh by its index data, drawn with these shaders."""
    return (origin, ib, count, vs, ps)


def game_identities(cap: Capture) -> set[tuple]:
    return {draw_identity(d["origin"], d["ib"]["hash"], d["args"]["index_count"], d["shaders"].get("vs"),
                          d["shaders"].get("ps"))
            for d in cap.draws() if "ib" in d and d["args"].get("index_count") and d["origin"] == "game"}


def compare_with_capture(cap: Capture, recs: list[dict]) -> None:
    by_id: dict[tuple, list[dict]] = {}
    for r in recs:
        by_id.setdefault(draw_identity(r["origin"], r["ib"], r["index_count"], r["vs"], r["ps"]), []).append(r)
    indexed = [d for d in cap.draws() if "ib" in d and d["args"].get("index_count") and d["origin"] == "game"]
    check("the full capture has the sandbox's indexed draws to compare with", len(indexed) >= 4, str(len(indexed)))
    check("two of them draw the same index data with the same shaders", len(game_identities(cap)) < len(indexed),
          str(sorted(game_identities(cap))))
    seen_ids = set()
    for d in indexed:
        a = d["args"]
        ident = draw_identity(d["origin"], d["ib"]["hash"], a["index_count"], d["shaders"].get("vs"),
                              d["shaders"].get("ps"))
        seen_ids.add(ident)
        found = by_id.get(ident) or []
        name = f"draw {d['index']} ({d['call']}, ib {d['ib']['hash']}, {a.get('first_index', 0)}+{a['index_count']})"
        if not check(f"{name}: one record", len(found) == 1, f"{len(found)} records; ids {sorted(by_id)[:6]}"):
            continue
        r = found[0]
        problems = []
        if r.get("fmt") != 2:
            problems.append(f"fmt {r.get('fmt')}")
        if r["ib_state"] not in ("ok", "cached"):
            problems.append(f"ib_state {r['ib_state']}")
        if r.get("ib_resource") != d["ib"]["resource_hash"]:
            problems.append(f"ib_resource {r.get('ib_resource')} != {d['ib']['resource_hash']}")
        # Where the mesh lay and how it was drawn belong to the draw that was recorded.
        if (r["first_index"], r["base_vertex"]) == (a.get("first_index", 0), a.get("first_vertex", 0)):
            if r["ib_offset"] != d["ib"]["offset"]:
                problems.append(f"ib_offset {r['ib_offset']} != {d['ib']['offset']}")
            if r["instances"] != max(1, a.get("instance_count", 1)):
                problems.append(f"instances {r['instances']} != {a.get('instance_count')}")
        layout = cap.layout(d["layout"]) or []
        want_layout = [f"{e['semantic']}{e['index']}" for e in layout]
        if r.get("layout") != want_layout:
            problems.append(f"layout {r.get('layout')} != {want_layout}")
        if r["skinned"] != any(e["semantic"].upper() == "BLENDINDICES" for e in layout):
            problems.append("skinned")
        for stage in ("vs", "ps"):
            want = {}
            for key, t in (d.get("tex") or {}).items():
                if not key.startswith(stage + "-t"):
                    continue
                res = cap.resource(t["res"]) or {}
                entry = {"kind": res.get("kind"), "hash": res.get("hash") if res.get("known") else None}
                if res.get("kind") == "buffer":
                    entry["size"] = res.get("size")
                else:
                    entry.update(w=res.get("w"), h=res.get("h"), mips=res.get("mips"), format=res.get("fmt_name"))
                view = t.get("view") or {}
                if view.get("format"):
                    entry["view_format"] = view["format"]
                want[int(key.split("-t")[1])] = entry
            got = {}
            for v in r[f"{stage}_srv"]:
                entry = {"kind": v.get("kind"), "hash": v.get("hash")}
                if v.get("kind") == "buffer":
                    entry["size"] = v.get("size")
                else:
                    entry.update(w=v.get("w"), h=v.get("h"), mips=v.get("mips"), format=v.get("format"))
                if "view_format" in want.get(v["slot"], {}):
                    entry["view_format"] = v.get("view_format")
                got[v["slot"]] = entry
            if got != want:
                problems.append(f"{stage} slots {got} != {want}")
        want_rt = []
        want_ds = None
        for key, o in sorted((d.get("out") or {}).items()):
            fmt = (o.get("view") or {}).get("format")
            if key == "oD":
                want_ds = fmt
            elif key.startswith("o"):
                want_rt.append(fmt)
        if r["rt"] != want_rt or r["ds"] != want_ds:
            problems.append(f"outputs {r['rt']}/{r['ds']} != {want_rt}/{want_ds}")
        check(f"{name}: the record agrees with the capture", not problems, "; ".join(problems))
    # The sandbox's own scene is the same in every frame: a record of it that the capture does not
    # have would be a wrong record.
    extra = [k for k in by_id if k not in seen_ids and k[0] == "game"]
    check("no record of the sandbox's draws without a draw in the capture", not extra, str(extra[:4]))


def check_folder(folder: Path, st: dict, recs: list[dict]) -> None:
    version = json.loads((folder / "version.json").read_text(encoding="utf-8"))
    check("version.json names the folder", version.get("key") == folder.name == st["version"],
          f"{version.get('key')} / {folder.name} / {st['version']}")
    check("the sandbox has no Unity data folder: the label says so",
          version.get("label") == "unknown" and folder.name.startswith("unknown+")
          and version.get("unity_data_folder") is False, json.dumps(version)[:300])
    check("the fingerprint inputs name the program", any(i.get("file") == "efmi_sandbox.exe"
                                                         for i in version.get("fingerprint_inputs", [])),
          json.dumps(version.get("fingerprint_inputs"))[:300])
    lines = [line for line in (folder / "draws.jsonl").read_text(encoding="utf-8").splitlines() if line]
    check("every line starts with the format", all(line.startswith('{"fmt":2,') for line in lines),
          str([line[:20] for line in lines[:3]]))
    all_keys = [r["key"] for r in recs]
    check("no key twice in draws.jsonl", len(all_keys) == len(set(all_keys)), str(len(all_keys) - len(set(all_keys))))
    hashes = {h for r in recs for h in (r["vs"], r["ps"]) if h}
    files = {p.name.split("-")[0]: p for p in (folder / "shaders").glob("*.bin")}
    check("one bytecode file per shader", set(files) == hashes, f"{sorted(files)} vs {sorted(hashes)}")
    for h, p in sorted(files.items()):
        code = p.read_bytes()
        kind = p.stem.split("-", 1)[1]
        check(f"shader {h}: DXBC whose 3Dmigoto hash is its name ({kind})",
              code[:4] == b"DXBC" and f"{fnv64(code):016x}" == h and kind in ("vs", "ps"),
              f"{code[:4]!r} {fnv64(code):016x}")


# ---------------------------------------------------------------- with XXMI
def with_xxmi(base: Path) -> None:
    sb, client = start(xxmi=True)
    try:
        if not client:
            return
        hello, _ = client.request("status")
        st = status(client)
        check("XXMI is loaded", hello.get("xxmi", {}).get("present") is True, json.dumps(hello.get("xxmi")))
        check("the probe offers the slot cache", "slot_cache" in hello.get("features", []),
              json.dumps(hello.get("features")))
        check("the defaults: on, one frame in 30, skinned draws only",
              st["enabled"] and st["every"] == 30 and st["skinned_only"] and st["subdir"] == "slot-cache",
              json.dumps(st))

        configure(client, every=2)
        st = wait_looked_at(client, 6)
        check("skinned draws only: frames are looked at, nothing is recorded (nothing is skinned)",
              st["frames_looked_at"] >= 6 and st["new_draws"] == 0 and st["records_written"] == 0, json.dumps(st))

        try:
            configure(client, subdir="..\\elsewhere")
            check("a folder that is not a plain name is refused", False)
        except ProbeError as e:
            check("a folder that is not a plain name is refused", "subdir" in str(e) or "plain" in str(e), str(e))
        check("a refused folder leaves the settings alone", status(client)["subdir"] == "slot-cache")

        reply = configure(client, skinned_only=False, subdir=SUB_X)
        check("the slot_cache command answers with the settings",
              reply["slot_cache"]["subdir"] == SUB_X and not reply["slot_cache"]["skinned_only"], json.dumps(reply))
        wait_looked_at(client, 8)
        st = frozen(client)
        folder = folder_of(st)
        check("the records go to a version folder below the chosen one", folder.parent == base / SUB_X, str(folder))
        recs = records(folder)
        game = keys(recs, "game")
        print(f"  {len(recs)} records, {len(game)} of them the sandbox's draws; {st['frames_looked_at']} frames "
              f"looked at, {st['record_ms_total']:.2f} ms in all, last {st['record_ms_last']:.3f} ms")
        check("every new draw is on disk or recognised as one already there",
              st["records_written"] + st["duplicates"] == st["new_draws"]
              and st["records_written"] == len(recs) == st["records_in_folder"]
              and st["pending"] == 0 and "error" not in st, json.dumps(st))
        doc, blob = client.capture(detail="full", timeout_ms=8000)
        cap = Capture(doc, blob)
        compare_with_capture(cap, recs)
        check_folder(folder, st, recs)
        # The caller of a draw is worked out only for the draws that are recorded, from further down
        # the probe's stack: XXMI's own draws have to come out as XXMI's.
        xxmi_shaders = {(d["shaders"].get("vs"), d["shaders"].get("ps")) for d in cap.draws()
                        if d["origin"] == "xxmi" and "ib" in d}
        overlay = [r for r in recs if (r["vs"], r["ps"]) in xxmi_shaders]
        check("XXMI's own draws are recorded with origin xxmi", bool(overlay)
              and all(r["origin"] == "xxmi" for r in overlay)
              and all(r["origin"] == "game" for r in recs if (r["vs"], r["ps"]) not in xxmi_shaders),
              f"{len(overlay)} records with XXMI's shaders; origins {sorted({r['origin'] for r in recs})}")

        configure(client, enable=True)
        wait_looked_at(client, 10)
        frozen(client)
        later = records(folder)
        check("later frames do not add the sandbox's draws again",
              keys(later, "game") == game and sum(r["origin"] == "game" for r in later) == len(game))
    finally:
        sb.quit()


# ---------------------------------------------------------------- without XXMI
def strict(base: Path) -> tuple[Path, set[str]] | None:
    sb, client = start(xxmi=False)
    try:
        if not client:
            return None
        configure(client, every=2, skinned_only=False, subdir=SUB_A)
        wait_looked_at(client, 8)
        st = frozen(client)
        folder = folder_of(st)
        recs = records(folder)
        game = keys(recs)
        print(f"  {len(recs)} records; {st['frames_looked_at']} frames looked at, {st['record_ms_total']:.2f} ms "
              f"in all, last {st['record_ms_last']:.3f} ms")
        doc, blob = client.capture(detail="full", timeout_ms=8000)
        cap = Capture(doc, blob)
        n_draws = sum(1 for d in cap.draws() if "ib" in d and d["args"].get("index_count") and d["origin"] == "game")
        check("one record per mesh and shaders; the second draw of the same mesh is recognised",
              st["records_written"] == len(recs) == len(game) == st["records_in_folder"] == len(game_identities(cap))
              and st["new_draws"] == n_draws and st["duplicates"] == n_draws - len(game)
              and keys(recs, "game") == game, f"{json.dumps(st)}; {n_draws} draws")
        compare_with_capture(cap, recs)
        check_folder(folder, st, recs)

        configure(client, enable=True)
        before = st
        wait_looked_at(client, 10)
        st = frozen(client)
        check("frames looked at later bring nothing back, not even as a duplicate",
              st["frames_looked_at"] >= before["frames_looked_at"] + 10 and st["new_draws"] == before["new_draws"]
              and st["duplicates"] == before["duplicates"] and st["records_written"] == before["records_written"]
              and keys(records(folder)) == game, f"{json.dumps(before)} -> {json.dumps(st)}")
        check("a frame without new draws costs little", st["record_ms_last"] < 5.0, str(st["record_ms_last"]))

        print("\n== another folder, one new draw per frame")
        configure(client, enable=True, subdir=SUB_B, max_new=1)
        folder_b = base / SUB_B / folder.name
        recs_b: list[dict] = []
        deadline = time.time() + 20
        while time.time() < deadline:
            wait_looked_at(client, 2)
            recs_b = records(folder_b)
            if keys(recs_b) >= game:
                break
        st = frozen(client)
        recs_b = records(folder_b)
        check("the other folder receives the same records",
              folder_of(st) == folder_b and keys(recs_b) == game and len(recs_b) == len(game),
              f"{st['folder']}: {sorted(keys(recs_b))[:3]} vs {sorted(game)[:3]}")
        check("with max_new=1 the new draws were spread over several frames",
              st["deferred"] >= n_draws - 1 and st["new_draws"] == before["new_draws"] + n_draws, json.dumps(st))
        check("the other folder has the same shaders", shader_files(folder_b) == shader_files(folder),
              f"{shader_files(folder_b)} vs {shader_files(folder)}")

        before = st
        configure(client, enable=True, subdir=SUB_A, max_new=256)
        wait_looked_at(client, 6)
        st = frozen(client)
        check("back in the first folder: everything is recognised, nothing is added",
              folder_of(st) == folder and st["records_written"] == before["records_written"]
              and st["duplicates"] == before["duplicates"] + n_draws and keys(records(folder)) == game
              and len(records(folder)) == len(game), f"{json.dumps(before)} -> {json.dumps(st)}")
        return folder, game, n_draws
    finally:
        sb.quit()


def restart(folder: Path, game: set[str], n_draws: int) -> None:
    with open(folder / "draws.jsonl", "a", encoding="utf-8", newline="") as f:
        f.write(CUT)            # as if the last session had stopped in the middle of a line
    text_before = (folder / "draws.jsonl").read_text(encoding="utf-8")
    shaders_before = shader_files(folder)
    # The other folder gets a file as probe 0.1.7 wrote them.
    folder_b = PROBE.parent / SUB_B / folder.name
    old = '{"frame":1,"origin":"game","ib":"00000000","index_count":3,"key":"game-00000000-00000000-0-3-0"}\n'
    (folder_b / "draws.jsonl").write_text(old, encoding="utf-8", newline="")
    sb, client = start(xxmi=False)
    try:
        if not client:
            return
        configure(client, every=2, skinned_only=False, subdir=SUB_A)
        wait_looked_at(client, 8)
        st = frozen(client)
        check("the same version folder", folder_of(st) == folder, st["folder"])
        check("after a restart the records in the folder are recognised, nothing is written again",
              st["records_written"] == 0 and st["duplicates"] == n_draws == st["new_draws"]
              and st["records_in_folder"] == len(game) and st["shaders_written"] == 0
              and shader_files(folder) == shaders_before, json.dumps(st))
        text_after = (folder / "draws.jsonl").read_text(encoding="utf-8")
        check("an unfinished last line is ended before anything else is appended", text_after == text_before + "\n",
              repr(text_after[len(text_before) - 40:]))

        configure(client, enable=True, subdir=SUB_B)
        wait_looked_at(client, 6)
        st = frozen(client)
        moved = folder_b / "draws-fmt1.jsonl"
        check("records of the older format are moved aside, not mixed in",
              moved.exists() and moved.read_text(encoding="utf-8") == old and keys(records(folder_b)) == game
              and all(r.get("fmt") == 2 for r in records(folder_b)) and "error" not in st,
              f"{sorted(p.name for p in folder_b.iterdir())} {json.dumps(st)}")
    finally:
        sb.quit()


def main() -> int:
    if not PROBE.exists():
        print(f"no probe at {PROBE}")
        return 1
    base = PROBE.parent
    folders = [base / SUB_X, base / SUB_A, base / SUB_B]
    for f in folders:
        if f.exists():
            shutil.rmtree(f)
    print(f"probe {PROBE}")
    try:
        print("\n== with XXMI")
        with_xxmi(base)
        print("\n== without XXMI")
        got = strict(base)
        if got:
            print("\n== without XXMI, after a restart")
            restart(*got)
        else:
            check("the session without XXMI ran", False)
    finally:
        for f in folders:
            if f.exists():
                shutil.rmtree(f)
    print(f"\n{passed} passed, {len(failures)} failed")
    for f in failures:
        print(f"  FAIL {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
