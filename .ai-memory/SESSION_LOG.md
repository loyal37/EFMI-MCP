# EFMI MCP — Development log

> A short summary is appended at the end of each round, recording what was actually done, the scope of verification, open problems and where to pick up next time; no chat and no complete logs are pasted.

## 2026-10-06: Plan document for model extraction, export and refresh acceptance

The user asked for the three-sample / LoyalTools workspace / Blender flow to be wrapped as a proper MCP; made clear that export includes generating game INI / BUF after editing, follows LoyalTools' complete native logic, that Blender connects only the diffuse, and added a refresh after export to look at the draw state of the target mod.

`docs/model-extraction-export-plan.md` was created, listing interfaces, the three samples, main model / LOD, material sources, native pre- and post-processing, the list of output files, deployment into a dedicated folder, one normal F10, comparison of the actual components and resources, coverage results and M1–M6. It records the false success of the native operator, the behaviour of keeping CPU-posed components, and the difference between the import check and the export check after editing. T-008 was marked as to be implemented.

That round changed documents only; no probe connection, no input sent, no extraction or deployment of a mod. The user added that this plan was to be delivered locally only and not committed to the repository; the staging of the files was undone, and no commit or push was created. The document was reviewed independently; the next step is implementing M1 according to the plan.

## 2026-10-06: Trying the automatic F10 after the user started the game

The feature commit `ab6f4a5` was pushed to loyal37/EFMI-MCP/main. In that round the user started the game and asked for a refresh to be tried; the dynamic tool search interface returned an internal error, so the production MCP server 0.1.2 was started directly and the tool called through real stdio, connecting to the only probe, Endfield PID 41976 (0.1.1). The first single F10 down/up was accepted by the API with the exact window focused and no Numpad0; waiting and capture completed in 2328 ms, original frame 57703 → barrier 57971 → 57972, 323 calls in all (203 game / 120 xxmi), 188 resources, 176 shaders and a CB snapshot of 9,523,456 bytes. IB/VB0 read-back was normal, the old cache was invalidated, and the MCP child process exited with 0. Evidence `captures/verify-reload-41976/result.json`; this proves only API acceptance and the subsequent capture.

The user then moved the Yvonne mod out and authorized another try. The directory scan went from 7 to 6 mods; one focus_refused refused before the key was sent; after the user brought the game to the foreground manually, the second ordinary VK request was accepted by the API and gave frame 84113, evidence `captures/verify-reload-41976-mod-removed-foreground/result.json`. The user explicitly confirmed that the mod was still shown and the refresh had not succeeded, and the screenshot agrees with the old mesh being drawn; XXMI calls going from 139 to 120 is a change in the additional passes of the same mesh, not evidence of unloading. The automatic refresh in the real game failed at that point, and it can no longer be written as "awaiting the user's confirmation" or "not yet verified in the real game".

The scan-code diagnosis is saved in `captures/verify-reload-scancode-41976-ready/result.json`: SendInput returned 1 with error 0 for down/up, the foreground was PID 41976 throughout with no modifier keys, but the asynchronous high bit of F10 was false during the whole hold; the IBs and counts of XXMI's DrawIndexed in frames 160361→160664 are the same. `assessment.json` states that the actual refresh failed, and the pass of the raw report only means that the diagnostic call / capture completed. Scan codes are not a fix and the cause was still unknown; Python being medium and reading the game's token giving access denied do not allow a conclusion about the target's rights or about security software blocking.

On this basis the server went to 0.1.3, adding the threshold of at least one GetAsyncKeyState high bit during the hold and separating `key_down_sent` from `key_down_observed`. When the press is not observed, the input stage returns `keydown_not_observed`, releases in `finally`, does not capture and does not resend; `reload_confirmed=false` is unchanged. The official v1.2.2 input does use that high bit, and user.ini is saved only when dirty, so the unchanged mtime of that round is not evidence of failure. No supported external reload interface was found; Core/auto_update.xcmd belongs to the launcher's update/start script and cannot serve as a run-time substitute.

In that round the 21 input checks + 32 workflow checks, the 39 checks of the MCP stdio private sandbox and compileall passed; the real-F10 private sandbox was not re-run because the user's game was running, and old sandbox results count only as historical evidence. What that round completed is the detection of the failure; the real input problem was still unsolved and the goal of T-007 stayed open. EFMI/launcher/Core/mods and the probe were not changed, no automatic elevation, no further input sent, and the game kept running. After syncing the records, 0.1.3 was committed and pushed under the existing authorization; the actual git result is authoritative for commit and push.

## 2026-10-06: Capture after an automatic F10, and the push agreement

The user asked for the MCP to do the refresh and the new capture after a mod change automatically. The server went to 0.1.2 with the new `reload_input.py`, a fixed F10 / foreground / PID / modifier key input module, and `reload_workflow.py`, the capture flow after a reload under the same pipe and render target; the probe stayed at 0.1.1, only the MCP server has to be restarted, with no re-injection and no hunting mode. The original double F10 binding is read and EFMI files are not edited directly; a normal F10 may save persistent variables, and the documents keep this side effect explicit.

The workflow stops live mode, releases and invalidates old captures / the mod index, waits the default 2 seconds after the single F10, then accepts a non-empty frame strictly later than the barrier; it does not reconnect to another process, does not resend F10 automatically, and does not resume live mode automatically. `reload_requested` is separate from `reload_confirmed`, which is always false since there is no receipt from XXMI. The dedicated tests found that an unknown input exception had been wrongly reported as not sent, corrected to null; when checking a live recording, live mode is stopped first and must not be mistaken for another unfinished single-frame request.

In the end the 19 input checks and 30 workflow/MCP boundary checks, the existing 39 MCP checks and a real F10 integration in the private sandbox all passed. The first two groups use fake input and a fake probe and are part of `run_all.py`, without system input; the real integration is run separately with `reload_integration_test.py`, which really focuses and presses F10 and refuses when it finds another probe.

Integration evidence `sandbox/build/reload-xuxnsq73/reload-evidence.json`: with `hunting=0` and without pressing Numpad0, a new private INI with `checktextureoverride=ib` for the VS plus the IB `hash=64e97b80 / match_index_count=864 / handling=skip` took effect through a real F10. The count of the target IB went 1→0, and the others, `3c980a7d:2` and `3970063e:1`, were kept entirely; tool frame 5 → barrier 209 → new frame 210 (10 draws, 6 game / 4 xxmi). Clearing of the old cache, the new mod index, stopping of live mode and the read-back of the kept IBs pass, and the sandbox process exited with 0. This is a verification in the private sandbox; no F10 was performed on the user's game, and no claim is made that reloading a real character passed.

The first integration run exposed that the sandbox's default `SC_KEYMENU` enters a modal loop, and the handling of the test window was corrected; then the IB rule did not match because `match_index_count` was missing, and passed once it was added. Neither showed that the product depends on hunting mode. It was also observed that XXMI 1.2.2 re-creates its key registrations during a single F10 hold of about 200 ms and can reload internally several times; the tool guarantees a single down/up pulse without automatic resending and cannot guarantee that XXMI reloads internally only once.

In that round the user also explicitly asked for pushing to `https://github.com/loyal37/EFMI-MCP` after completion and in later rounds; AGENTS and D-007 were updated. "No push" in the old log keeps the context of its time; the round was completed in the order verify, memory, diff review, commit, push, without adding screenshots, captures or ignored build output to the source commit.

## 2026-10-06: Reducing the time to read real data

The user reported that capturing was slow. The complete archive on PID 29988 was profiled first, then the following were optimised outside the game: CRC32C, PNG compression / shared decoding, JSON encoding, the useless VB hashes of the identity check, and the repeated buffer reads of the offline bridge. A new independent `server/native/build.bat` builds the CRC helper without changing the probe; when it is missing / the CPU does not support it / the self-test fails, Python is used automatically again, keeping the incremental seed and the semantics for all kinds of buffers. The MCP test hides, in its child process, the probes that existed before the test started, so it can be verified safely while the user's game is running.

Three interleaved old/new capture rounds in the real game: median 1.843 → 1.220 seconds (33.8% less), with the complete buffers of 41,967,616 bytes, 24 DDS / 45,616,608 bytes and 86 geometry CRCs all matching in every round; the merged export of the new frames 313285/313640/313988 still passes the three-sample gate. A 16 MiB CRC alone went from 1.754 seconds to 1.897 milliseconds. PNG pixels are unchanged, the sample sizes grew by 52%–75%. The offline bridge: median of three rounds 1.890 → 1.662 seconds, BUF reads 1134 → 9, the comparison of 928 output files passes (ignoring only output paths / log seconds); the 6 rejection thresholds, 6 NumPy read semantics and the cache release checks pass, the peak of the whole-buffer cache is about 80 MiB and it is emptied on return. Boundary tests corrected the exception semantics of a NumPy offset beyond the tail, without deleting error checks to pass.

The related regressions selftest 83 / MCP 39, the 8 CRC groups / 6 image groups all pass, compileall / the diff check pass, and an independent review found nothing blocking. Evidence `captures/performance/` and `captures/bridge-performance-1fac750a2f/results.json`; these times do not include the Blender import and do not represent the cost of GPU rendering frames. All changes are inside this project only, the SHA of the probe DLL is still b0fee912...8fa8a, the game PID 29988 kept running, and EFMI / LoyalTools / the finished output on drive E were not modified. After syncing the memory a local commit was made, without automatic push. A new Python capture benefits at once; an old MCP server has to be restarted to pick the CRC backend, and the game needs no re-injection.

## 2026-10-05: Memory set up, `0.1.1` tool fix and local verification completed

**The user's goal**: set up a project memory for the current tool following `E:\loyal tools\.ai-memory`, then fix the problem that it was injected but could not read the current character's data; change only this tool, do not touch EFMI.

**What was done**:

1. Read the seven-file structure for reference only, checked this project's README, the probe/MCP sources and the logs, and created the seven memory documents and the root `AGENTS.md`.
2. Fixed the problem of an auxiliary device created later overwriting `g_dev/g_ctx` while `g_swap` is kept: device hooking and target selection are separated, the first swap chain found binds the device/context it belongs to and holds references; capabilities are stored per context, and the GPU writer is limited to the selected context and swap chain.
3. Probe/server went to `0.1.1`; the status shows target pointers / the draw count and warns on an empty capture; live mode got a generation that stops old frames from being back-filled after a stop / restart / disconnect.

**Evidence from failure to fix**: the old game PID `39000` could be injected and connected to, but frame `22964` had `0` draws / resources / shaders, and live mode was empty. The old version's scenario with the auxiliary device created later showed `6` passed / `7` failed, recording only the auxiliary triangle. The first self-test showed `71` passed / `2` failed, which located the back-filling of in-flight live frames; after the generation fix and `10` new checks the self-test passes completely.

**Correction of the test scene**: the posed VB occasionally has `39` floats changing by `1 ULP` (at most `5.96e-8`); the old probe without XXMI reproduces it too, and each time the hash agrees with the CRC of the read-back, so it is not a probe regression of that round. The test shader got HLSL `precise`, keeping strict assertions; with and without XXMI the `11616` bytes of `12` consecutive first-frame captures each are identical, CRC `1b4be723`. The exact optimisation mechanism is still an inference.

**Final verification**: `sandbox/build/verification-0.1.1.log` exit code `0`, all eight stages `PASS`. `69` hash parity items in each of three load orders with `0` mismatches, and the IB/VB hash consistency of the second capture is `True` everywhere; self-test `83`, multi-device without/with XXMI `96` each, MCP `39`, all pass. `git diff --check` and Python `compileall` pass.

**Delivery and protection**: `dist/efmi_probe.dll` is `355840` bytes, SHA256 `b0fee912263763c9db28adc21798505845c9c803606bbf3969227a2bc948fa8a`; the old version is backed up in `dist/backup-before-multidevice/`. Only this project was changed; final SHA256 of the `56` files of the original EFMI directory `changed=[]`. The launcher configuration was not modified, no user process was ended, and the game was not running at the end.

**Open boundaries**: capture of a character in the real game with the new version was not verified yet. Automatic recognition of the main picture among several swap chains is not supported; a replacement swap chain across devices is refused with a hint to restart; the integrity of captures across the re-creation of a swap chain on the same device is not verified.

**Next time**: read `CURRENT_STATE.md` / `TASKS.md` first. T-001, the local tool fix, and T-002, the memory, are done; after the user starts the game the usual way, carry out T-003: confirm that `0.1.1` is really loaded, then capture a full frame, live mode and character-related resources.

## 2026-10-05: Capture verification on Yvonne in the real game, and the commit agreement

The user injected again, and PID `38252` was confirmed to run `0.1.1`. Frame `9113` has `285` draws / `170` resources / `184` shaders; 15 consecutive summaries are all non-empty. The probe's backbuffer screenshot confirms that the current character is Yvonne; 30 draws and 6 IBs match the hash/index_count of the Yvonne INI.

Frame `14377` (286 draws) was captured in addition, and the complete IB/VB0/1/2 of three mesh groups, 12 buffers in all, were read with every CRC32C matching the capture hashes; a CB snapshot of 13,979,968 bytes from the time of the draws was kept, and 5 textures and VS/PS bytecode with disassembly were read. The evidence is in `captures/verify-game-38252/`. After capturing, live mode was stopped and held resources released, without changing EFMI / the game / the launcher; no claim was made of having reconstructed meshes or a skeleton.

The user explicitly asked for the code to be committed after every change from then on, which was written into the root `AGENTS.md` and D-007. That round made a local Git commit of the previous round's verified code fix together with the memory (the commit number can be looked up with `git log`); capture files and build output stay ignored, and there was no automatic push.

## 2026-10-05/06: Mod verification in the real game and the model extraction that followed

The verification of the Yvonne mod is complete: 93 replacement draws, 18 parameter combinations, 72 region CRCs agree; 16 complete buffers of 29,824,876 bytes and the 2K textures of 4,194,304 bytes are byte-identical to the mod's source files, and screenshots and 12 live frames are saved. The evidence is in captures/verify-mod-38252; at the end live mode was stopped / resources released, and EFMI stayed read-only.

The user then asked for the character currently on screen to be extracted to E:\Endfield mods\liimo merge, following LoyalTools' merged-skeleton method. On 2026-10-06, frame 56883 of the new process 29988 confirmed that the current character is Liino, and the output directory was empty. The previously verified fix and the memory were committed first, then the extraction task was implemented; the user explicitly authorized that output path, and LoyalTools/EFMI are not modified.

## 2026-10-06: Delivery of Liino's merged-skeleton workspace

The earlier fix was committed as 6278b07. New: capture_merged_source, loyal_bridge, the export CLI and the Blender check script, which refer to LoyalTools' original code without changing its sources. The offset of the captured bone pool is dynamic, and the first strict same-frame attempt did not pass; following the user's goal of static editing, verification was changed to three independent data sets, without lowering it to "non-zero passes". The complete geometry of 3,769,210 bytes of the three frames 147189/147435/147679 is identical, and the canonical sources agree; the 682 bone matrices in use all change in the other two samples, and 170 weighted bones across components / 34,736 channels merge stably.

Delivered to E:\Endfield mods\liimo merge: workplace, liimo merge.blend, the verification JSON, preview.png and extraction notes. 18 components / 79044 vertices / 94051 triangles, 696 private group slots to 512 canonical; the byte check of the 72 output IB/VB passes. A real import with Blender 4.5.7 + LoyalTools 1.6.7 gives errors=[]; in the first run the material option for normals, off by default, made the verification fail, and the script passed after explicitly enabling the normal map in that temporary scene and selecting the diffuse node for the Solid texture display. Source geometry and the user's original directory are not affected by these verification fixes.

The preview check is complete; effects such as the rocket boots do not rebuild the game's shader, and 2 small components have no automatic texture; the data is kept in all cases and the boundaries are written down. No LOD that was not captured is included, and neither the pose of some frame nor a named hierarchical rig is claimed. Finally the memory was synced and a local commit made, without push.

## Evening of 2026-10-06: Cause of the F10 failure located; the probe presses the key instead (Claude session)

The user reported that the automatic F10 implemented by Codex had no effect in the real game. It was found that the manifest of `Endfield.exe` requires administrator rights while the MCP server has ordinary rights, so UIPI drops its key presses and `SendInput` returns success as usual. Implemented `reload_key` of probe 0.1.2 (a fixed in-process F10) and the reload evidence of server 0.1.4; see the first section of CURRENT_STATE and D-012.

In the first sandbox round the key arrived but XXMI did not reload: `SendInput` was held up for a few milliseconds by the system keyboard hooks, and the Presents during that time were counted as hold frames. After counting frames from the moment the pressed state is read, it passed; then the release condition was set to "the second Present, or the first Present later than 10 ms", and in the end one key press gives exactly one reload. While the game held the foreground the sandbox could not take it, and the key-press tests finished only after the user switched away from the game.

The final DLL (SHA `a1ba4a93…2668`) passed the full suite and was installed as `dist/efmi_probe.dll`; the old 0.1.1 is kept in `dist/backup-0.1.1/`. The game PID 41976 was still running and still 0.1.1; EFMI/the launcher were not changed and no key was sent to the user's game.

The user then restarted the game, moved the Yvonne mod out and asked for a refresh. It passed at the first attempt in the real game: PID 43816 reports probe 0.1.2, process elevated (integrity 0x3000). After the user moved the Yvonne mod out, the tool reloaded: the key was pressed inside the process and was read, held for 1 frame (21696→21697), and the reload frame took 92.7 ms (about 8 ms normally); of 367 draws before the reload 120 were XXMI's (93 DrawIndexed + 27 Dispatch), after the reload all 277 came from the game, XXMI's 93 draws / 18 meshes were gone and the game's original 30 draws / 6 meshes appeared; `reload_confirmed=true` (XXMI created, after the key press, 27 shaders that the new frame uses). The game kept running. Evidence `captures/verify-reload-inprocess-43816/`. The game was in the foreground at the time.

The user confirmed that this and later Claude sessions commit and push per D-007 as well.

When the user asked for a refresh again the game was in the background. Second real-game run the same day at 20:51, with the game in the background (the Claude window in front): the probe took the foreground by the `direct` method, the key was held for 1 frame (91844→91845, reload frame 79.8 ms), then the foreground was given back to the original window (the same hwnd before and after); the Yvonne mod that had been moved out disappeared likewise (XXMI draws 120→0, the game's original 30 draws appear), `reload_confirmed=true`. The fallback path for taking the foreground, `attach_input`, was not used in the real game and is verified only in the sandbox. Committed and pushed after syncing the documents.

**Next time**: no verification item is left; an MCP server started by a new session is 0.1.4.

## 2026-10-07: Integration of model extraction, LOD matching, native export and the deployment check (Claude session)

The user asked for the model workflow to be integrated following the plan document, with the description of LOD matching added (by frame analysis directories, likewise for data captured by the agent, and the JSON matching newly added to LoyalTools); stressed midway that it should mainly be an integration, so that an update of LoyalTools does not require changing the MCP, and asked for commits both locally and to GitHub when done.

Implemented the seven tools of server 0.1.5 and the corresponding modules; see the first section of CURRENT_STATE, D-014, and sections 2.1, 5 and 13 of the plan document. Following the "integration" requirement, the add-on installed in Blender is used by default, success or failure of an export is decided by the output, and the in-game check follows the INI references to the files. The offline part was verified with the real LoyalTools 1.6.8 and Blender 4.5.7 on a copy of an existing workspace; the game was not running in that round, so live extraction and the deployment check were not verified, which the documents state everywhere.

Regression: all 11 steps of `sandbox/run_all.py` pass (probe self-test 83, multi-device 96 + 96, MCP end to end 39, reload 27 + 46, model workflow 37); `sandbox/model_offline_integration.py` passes (a copy of the Yvonne workspace, 77 exported files including the LOD entry points); "export again after editing" was added afterwards, 33 checks in all: after moving all meshes only 10 vertex buffers change, and all of them are translated by the same vector. On the restarted MCP server 0.1.5, offline calls of the project query and the export job work.

Findings along the way: when the specified Blender does not exist, a known installation used to be substituted quietly, which is now an error; the add-on's export log is saved without the emoji, so it cannot be judged by the tick / cross marks; the job thread redirecting stdout affects print calls of other threads in the same process (the server's main loop is not affected, and the test scripts now write to the saved console stream); LoyalTools names its blueprint and INI after the workspace root directory, which is why the project directory dropped the `project/` level.

**Next time**: when the game is running and the target character is not replaced by a mod, do the real-game verification listed under T-008 in TASKS; after that assess writing the probe data in the format of a frame analysis directory to remove the internal dependency of the archive adapter.

## 2026-10-07 (continued): Offline verification of exporting again after editing; F11 switch for all mods

Continuing the previous entry: the offline integration test gained "export again after moving all meshes in Blender"; on the Yvonne copy only 10 vertex buffers change, all translated by the same vector (commit `c673447`). On the restarted MCP server 0.1.5, offline calls of the project query and the export job work.

The user then asked for F11 to be added so that the agent can enable or disable all mods at any time to compare the original draws with the mods' draws. EFMI turned out to have its own `[KeyToggleMods]` (F11 flips `$enable_mods`), so the probe presses that key: probe 0.1.3 adds `mods_key`, server 0.1.6 adds `efmi_toggle_mods`, and the state is read back from EFMI's notification text. See the first section of CURRENT_STATE, D-015 and LIMIT-005.

Verification: 22 unit tests; 25 checks with real key presses in the private copy of XXMI + the EFMI core pass (including the probe's own two refusals concerning bindings); the 12 steps of `run_all.py` and the real-F10 sandbox test pass for 0.1.3. The new DLL is installed in `dist` (the game was not running at the time; 0.1.2 is kept as a backup). The real game was not running, so neither F11 nor the real-game part of the model workflow was verified.

**Next time**: after the user restarts the game and the MCP server, do the real-game check of T-009 first, then, with mods off, the real-game extraction, export and deployment check of T-008; switch the mods back on at the end.

## Noon of 2026-10-07: F11 and the model workflow verified in the real game; the plan document moved out of the repository

After restarting the game the user asked for verification, and asked that the plan document not be uploaded to GitHub. The plan document was removed from the repository and ignored (`02d116f`), and the README now carries its own project layout and LOD description; the file is still in the history of earlier commits, and history was not rewritten.

F11 passed in the real game. The verification script pressing 4 times in a row made the user ask about it, after which "no key press when the state is known and the picture agrees" was added. The user named `E:\Endfield mods\yvonne merge` as the extraction target; the extraction was stopped by three checks in turn (the dynamic vertex buffer of the CPU-posed component, the floating device that moves, the last-digit noise in bone merging), each located and corrected, after which it passed in one go, with a bone structure matching the user's old project. The half-finished directories left by the failures were moved from the user's mods directory to `captures/verify-model-18776/failed-extraction-1`.

The user then asked: disable their existing Yvonne mod, deploy the export, refresh and look, remove it and switch back if there is no problem, and solve any problem first. Three rounds in all (each restored afterwards): the first was undecided because the frame was taken too early, and the timing of taking the frame was corrected; the second passed but the hash of one position buffer did not match and was not judged, so deciding by content was added; in the third all 40 buffers are identical. No problem was found with the export. The symptom of the second round points to the probe's region hash cache returning an old value when an address is reused; it was not reproduced and was recorded as T-010. See the first section of CURRENT_STATE and D-016.

`run_all.py` was to be re-run after the game was closed; the MCP server of the desktop app was still the old version and had to be restarted.

**Next time**: T-010 (reproduce in the sandbox first); the remaining items of T-008 (LOD distance, a modified model).

## Afternoon of 2026-10-07: The probe's region hash cache reproduced and fixed (T-010, probe 0.1.4)

The user asked for the suspected probe problem noted in the morning to be dealt with. Reading the code first confirmed that the cache is keyed by address and that equal write counts on address reuse make an entry wrongly valid; the sandbox program got an option "destroy the buffer periodically and re-create it with the original size" with a matching self-test, and on 0.1.3, 114 of 180 comparisons gave the old hash, so the reproduction holds. The fix makes cache entries carry the object serial, and on 0.1.4 everything agrees; in addition a feature flag, a server hint for old probes, and deciding by content for index buffers in the deployment check.

The 12 steps of `run_all.py` pass (in the first run one parity test failed because the foreground was the game window and the key press was dropped; it passed when re-run). The new DLL was installed into `dist` by renaming while the game was still running 0.1.3; asked whether the game needed a restart, the answer was that a restart after installing loads it. In the real game two reload comparisons were made with an unchanged mod; the results agree but do not constitute verification. See the first section of CURRENT_STATE, BUG-005 and D-017.

**Next time**: confirm after a game restart that 0.1.4 is loaded (done at 17:20 the same day, see the next section); the remaining items of T-008 (LOD distance, a modified model).

## 2026-10-07 17:20: The restarted game loads probe 0.1.4

After restarting the game the user asked for a try. The user restarted the game at 17:17 on 2026-10-07, and the new process (PID 28408) loaded 0.1.4: the status carries `region_hash_per_object` and no longer has the hash hint of an old probe. At 17:20 a script checked through the current server code: one full capture, then two F10 reloads triggered by the tool (the game in the background, the probe taking the foreground and giving it back, both receipts ok); in each of the three frames the hashes recorded for 270 XXMI buffer regions (46.4 MB read back per frame) all equal the CRC of the bytes read back, and no buffer was rewritten in between; both reloads have `reload_confirmed=true` and the results carry no `hash_caution`. An unchanged mod was reloaded, so old and new buffers have the same content; this only shows that the new probe captures, presses keys and hashes normally in the real game, and cannot show the difference between the old and the new probe; the basis for the defect being fixed remains the sandbox reproduction (114/180 → 0/180). Evidence `captures/verify-hashes-28408/result.json`.

After the game restart, `dist\efmi_probe-0.1.3-loaded-by-running-game.dll`, left by the renaming, was deleted. That round changed documents only, no code.

**Next time**: the remaining items of T-008 (entry points at LOD distance, a modified model); whether to rewrite history to remove the plan document from old commits was still waiting for the user's answer and would not be done without consent (the user then answered that it need not be removed, see the next section).

## 2026-10-07 17:39: Reload after a change of content verified in the real game (T-010), with a check of the modified model in passing (T-008)

The user answered that the experiment should be added and that complete removal is not needed: do the real-game verification; the plan document in old commits stays (D-018).

Two exports of the same size with different content were prepared on a copy of the workspace (A as is, B with the vertices moved as a whole in Blender). After a pre-check without key presses confirmed that the character was in view, the user was told that F10 would be pressed 3 times; then: disable the user's Yvonne mod → deploy A → deploy B → remove and restore. Both deployment checks passed, and the content of the user's mod folder is the same before and after. The hashes recorded in the four frames all agree with the content read back; B's Position buffer of `c0bb7cd6` was created at the address of a buffer of the same size that belonged to the user's mod at the start of the connection, with the same three read ranges and different content, and the hashes are correct: the trigger condition of the old defect occurred in the real game and was handled correctly by 0.1.4. There was no address reuse between neighbouring stages; this instance was found only by replaying all four frames by "address + range". See the first section of CURRENT_STATE and BUG-005.

Two comments saying "usually returns to the original address" were corrected according to the real-game data; the probe was rebuilt into `dist\candidate` and passed the 86 self-test checks, was not installed, and the game still has the same 0.1.4; the 49 unit tests of the reload flow pass.

**Next time**: of T-008 only the entry points at LOD distance and `efmi_map_lod method=capture` remain (needs the user to move the character to LOD distance).

## Evening of 2026-10-07: Real-game check at LOD distance (T-008 complete), server 0.1.7

The user moved Yvonne to LOD distance (open world). The first `efmi_map_lod method=capture` failed for the whole frame because of dynamic batching in the open world; it was changed to archive only objects with GPU-skinned meshes, to read rewritten vertex data back in one go, and to hand the probe back after capturing, and "a probe archive as the LOD side" was first run through offline. The second real-game attempt succeeded at once: mods off for 5.3 seconds, LoyalTools recognised `Character 36804`, all 11 components matched; compared with the old mapping in the user's project the LOD entries of 10 components are the same, the LOD mesh of the CPU-posed component is additional, and 7 items of the bone index remap differ (5 of them not equivalent, left for the user to judge).

The deployment check of the LOD entry points was carried out after the user agreed on the spot (asked twice: whether to disable their mod; how to continue after one F10 was stopped by `focus_refused`). The exports with both mappings pass: the game's LOD mesh is replaced, and the `Texcoord-LOD1` and `BlendRemap` buffers are used with matching content. The user's mod was restored as it was. In passing, the problem of the reload confirmation taking its frame too early in the deployment check was fixed. See the first section of CURRENT_STATE, D-019, LIMIT-003 / 004.

Verification: unit tests 56 + 50 + 27 + 25 and the 41 MCP end-to-end checks pass; the probe was not changed.

**Next time**: nothing blocks the model workflow. Optional: levels farther than LOD1, a three-sample extraction in the open world, the look of a manually modified model; the MCP server of the desktop app has to be restarted to be 0.1.7.

## Late night of 2026-10-07: Where the two LOD mapping differences come from

The user asked whether the differences lie in their mod or in the extracted model. Read-only examination of their installed mod (it uses no remap table and does not touch the components in question) and of the old project `yvonne test` (which has the LOD entry of the CPU-posed component), a reading of LoyalTools' rules for re-using mappings, and one skinning comparison (the close-view mesh skinned with the LOD bones under both mappings, compared with the game's LOD mesh). Conclusion: neither; the remap difference has no practical effect, and the missing entry was removed by a LoyalTools check when re-using by JSON. Only documents were changed. See the first section of CURRENT_STATE and LIMIT-004.

**Next time**: nothing pending. The user then explained that CPU components are never modified (deleted, the game's own are used), so the missing LOD entry has no effect and is not added; written into `AGENTS.md`.

The user also asked whether losing this entry in JSON matching is a problem of this tool or of LoyalTools: it is LoyalTools' re-use check (the vertex group range item of `_lod_entry_problem`). Verified with the add-on's own `apply_lod_mapping` on in-memory copies (LoyalTools 1.6.8): applying the mapping of the old project `yvonne test` back to its own archive with the LODs removed likewise matches only 10 / 11 with the same message; after emptying the 40 vertex group mappings in that LOD record and applying again, all 11 / 11 are kept. So it does not depend on who captured the data. Script and output are in `captures/verify-lod-3468/remap-check/json_reuse_experiment.py`, `json-reuse-result.txt`.

## 2026-10-08: Items 1, 4 and 5 of the improvement list (probe 0.1.5 / server 0.1.8)

The user asked what else would be worth improving for an agent obtaining draw data, then asked for items 1, 4 and 5 to be done; in between also asked how to restart the MCP server of the desktop app (the README said: `/mcp` → `efmi` → Reconnect, or a new session) and whether item 1 meant the tool could capture only one frame (no: the reload tool takes only the first frame after the reload).

Done: looking at several more frames after a reload and returning the frame in which the mods are drawn again (`37db7ad`); a full capture records fixed pipeline state, samplers, view parameters and indirect draw arguments, and `efmi_get_draw` shows them; the new tool `efmi_capture_draw_outputs` copies the render target once before and once after each selected draw and reports the changes. Before installing, the probe's new code was reviewed once more and changed in five places (recognising state objects by content, depth-only passes taking no quota, budget in pairs, release after reading, the hint that index data has changed).

Verification: all 13 steps of `run_all.py` pass (self-test 143, end to end 68, 38 new offline unit tests), and a performance re-test shows no regression. Probe 0.1.5 is installed in `dist` (the game was not running at the time), 0.1.4 is backed up in `dist\backup-0.1.4\`.

**No verification in the real game**: the game has to be restarted to load 0.1.5. **Next time**: check the four items in the real game as listed under T-011 in TASKS; look at `efmi_status` first to confirm the probe version and `features`.

Correction (same day): following "`/mcp` → `efmi` → Reconnect", the user found that the MCP servers dialog of the desktop app shows `efmi` as connected with no reconnect button (a screenshot is the evidence; the connection state of the session shows `efmi` still with 26 tools, that is, old code). That is the menu of the terminal version's `/mcp`; the desktop app starts a server with new code only in a new session. The installation section of the README was changed, and what the previous commit `70c0827` said is void.

## 2026-10-08 (continued): Real-game check of the three improvements, server 0.1.9

After the user restarted the game (PID 9736, probe 0.1.5), checked through the MCP tools: pipeline state and `efmi_capture_draw_outputs` pass on an open-world screen with 4 modded characters; the reload with one F10 press works and is confirmed, but the waiting logic was not triggered. Numbers and details are in the first section of CURRENT_STATE.

After seeing it in the real game, several presentations in the server were changed (a picture of the main depth buffer, results grouped by draw, a DDS fallback, the record time, merged blend state, notes about off-screen targets being upside down and the cost of `depth=true`), version 0.1.9; 41 offline unit tests and the 68 end-to-end checks pass. The probe was not changed.

**Next time**: nothing to do. T-011 lists two things to look at in passing some time and the items of the list that were not done. The desktop app needs a new session to use 0.1.9.

## 2026-10-08 (continued again): A second F10 and the change of the wait criterion after a reload, server 0.1.10

The user agreed to one more F10 to verify "looking at several more frames". With `settle_ms` set to 250 the wait was still not triggered, but in the frame obtained XXMI drew only 272 times (508 once stable): the old criterion took a frame with "only half back" for the result. It was therefore changed to wait until "the number of newly created objects in use has stopped growing", and the result gained `mods_settled`.

Verification: 60 unit tests of the reload flow, the other offline suites pass, and the real-F10 integration test in the sandbox passes (`frames_looked_at=3`, `mods_settled=true`).

**Not done**: verification of the new criterion in the real game (the MCP server of that session ran old code and held the probe pipe). **Next time**: do it once in a new session as listed under T-011 in TASKS; it needs one F10 press, so tell the user first.

## Afternoon of 2026-10-08: The user asked whether the texture slots of a component can be obtained exactly, and whether RabbitFX-style mods can be changed to slot style

Answers only; no code was changed and no mod touched. The game was not running at the time, so nothing was captured on the spot; the basis was read-only inspection of RabbitFX 2.7, the INI files of the mods, `CommandListOverrideTextures` of the EFMI core, the real-game captures of that morning, and `TextureSlots.json` in the Yvonne project extracted on 10-07. The facts found and the three points of non-equivalence to watch in a conversion are written under T-012 in TASKS.

Main points of the answer: the slots can be obtained exactly (per draw, per pass), and Yvonne's slot table is readily available; the texture binding part can be converted and checked in the game; the G-buffer pass, Liino's FX effect, and slots changing after a game update are the three points where it is not equivalent to RabbitFX.

**Next time**: wait for the user to decide whether to convert, and which one first. Confirming the new wait criterion after a reload in the real game (T-011) was still to do.

## 2026-10-08 15:08–15:40: TangTang's INI changed from RabbitFX to slots at the user's request

The user named TangTang and asked for the original INI to be backed up and the INI changed directly. The slots come from the fallback slots in the shaders RabbitFX rewrote (component 0: t13 / t14; component 2: t16 / t17 / t18); the original INI is backed up in the same folder. Check after one F10 reload: the two lit passes sample exactly the three textures that used to be handed to RabbitFX through t70–t72. The G-buffer pass and the outline of component 2 now use the game's own textures, which is a difference inherent in the slot style.

Not finished: the game had been closed normally before the step of capturing the picture after the change (the log shows a normal exit, not a crash), so there is no before/after picture comparison; the LOD distance was not checked either. Details and what remains to be done are in the first section of CURRENT_STATE and under T-012 in TASKS.

In passing: this reload ran the new wait criterion of 0.1.10 (3 frames, `mods_settled=true`); there is now real-game data for the record time of a frame with render target copies. `AGENTS.md` gained the rule "a file of the user's mod may be modified only when the user names it, after a backup".

**Next time**: with the game open and TangTang in view, do the two remaining checks of T-012; if the user wants other characters converted, the method is the same.

## Early evening of 2026-10-08: Picture comparison and scope-of-effect check for the TangTang change

The user reopened the game (PID 22552). Two things were done without a key press: a side-by-side comparison of the pictures of each pass before and after the change (no visible difference in the lit result; in the G-buffer base colour one patch of the body changed to the game's original skin texture); the mod textures left on slots such as t13 / t16 are not sampled by other draws (no game draw of the lit and outline passes still has them bound, and the scene shaders of the G-buffer pass that read t13 / t14 bind their own textures).

**Next time**: only the LOD distance remains (needs the user to move the character far away).

## Evening of 2026-10-08: Audit of how far live capture covers all data of a FrameAnalysis

The user asked for the current tool to be reviewed, with the goal that all data of the given `FrameAnalysis-2026-10-07-123937` can be obtained live. Probe, server and the actual directory were reviewed in parallel: 776 calls (715 draws / 61 dispatches), 18,667 files at the top level, 2,767 files in deduped; the log has 50 kinds of D3D API, 10,953 call records and XXMI's internal command / variable information. Counts were checked with the production offline tool and an independent listing.

Conclusion: `full` at that time mainly recorded bindings, and live keeps only aggregated summaries; ordinary resources are read late, the optional CB snapshot covers only the bound window, and indirect arguments and the RTV/DSV of selected draws have content from that moment. Missing: a general MCP entry for SRV/UAV buffers, versions of dynamic resources, graphics OM UAVs, complete events / the internal log, a whole-frame archive and completeness acceptance. In the actual dump the same OM u0 of #550–552 has three different BUFs, and the same o0 of #481/482 differs too, which proves that a late read cannot replace content from the moment of the event. The dump itself has 810 failed TXT creations, a skipped built-in t120 and lossy JPGs, so acceptance has to distinguish kinds of content. Also recorded: BUG-006 (contents_captured can be wrong when a CB snapshot fails), the outdated SRV view description, and the correction of "all viewports" in the architecture document.

The re-check with the production offline tool read 776 calls correctly but recognised only 18,509 resource files; an independent enumeration found 157 resource files with the `!S!` marker skipped by the regular expression, recorded as BUG-007. After review the retention scope was also made precise: CB / indirect-argument bytes that an old capture has already fetched stay readable, and only ordinary resources that depend on the probe are limited to the latest capture.

The local server 0.1.10 provides 27 tools; the probe sources are 0.1.5. After the AgentDock search failed, the check went through the local production tool entry point; no probe / game was running, and that round captured no new frame, pressed no key, and changed neither run-time code nor EFMI. Evidence and audit scripts are only in the ignored `captures/audit-realtime-20261008/`. The README got the coverage matrix, and T-013 records that the goal is not reached and the suggestions for closing the gaps. The documents passed an independent review and `git diff --check`; the audit commit is `e68014d`. Two normal pushes failed: the first connection was reset, and on a retry with HTTP/1.1 the connection to GitHub 443 failed; nothing was force-pushed. The commits stayed local and had to be pushed once the network was back.

**Next time**: close the gaps item by item according to the data coverage settled in T-013; keep the performance distinction between "all data of one frame on demand" and "sending full data every frame continuously", and do not take hash parity for acceptance of all data.

## Late night of 2026-10-08: First step of T-013 (server 0.1.11)

The user asked for the gaps of the audit to be closed, with the goal of getting essentially every type of data in a frame analysis directory. The part that needs no game restart came first: `efmi_read_buffer` supports SRV / UAV buffer slots and whole-buffer reads, and the result carries the source and whether it is exact; BUG-006, BUG-007 and two outdated texts were corrected. The 74 checks of the sandbox `mcp_test.py` and the offline unit tests pass; the game was not open, so no real-game verification and no key press. This push also carried the two audit commits another agent had left local.

**Next time**: probe 0.1.6 (snapshots of contents at the time of the call and so on, see T-013).

## Late night of 2026-10-08: The main body of T-013 (probe 0.1.6 / server 0.1.12)

Implemented "contents as they were at the call": while recording, the probe only registers versions and stores old content just before a resource is rewritten (for a buffer the stretch that is overwritten, for a texture the subresource), and on read-back it pieces together the bytes each call saw; added OM UAVs, all viewports, UAVs after a Dispatch, 3D and multisampled read-back, and the write list; the server got the `contents` capture option, `efmi_list_writes` and `efmi_dump_frame`. The sandbox got a contents scene and three new test groups; the byte-for-byte comparison with the dump of the private XXMI copy shows all 404 files identical. The 16 steps of `run_all.py` pass. Probe 0.1.6 is installed in `dist\` (SHA256 of the last installation `FFFA1315B5F16B2E…`, which has the copy counters in the status in addition to commit `288ba31`), 0.1.5 is backed up in `dist\backup-0.1.5\`. A real-game check script is prepared in `captures\t013-contents-20261008\live_check.py` and has been run against the sandbox.

Midway the user asked for test windows to open on the secondary screen: the sandbox was changed, and this was written into `AGENTS.md`.

The game was not open: no real-game verification, no key press.

**Next time**: do the real-game check of T-013 after the user starts the game (the first section of CURRENT_STATE lists the items); the MCP server has to be restarted to use 0.1.12.

## Early morning of 2026-10-09: Server 0.1.13 (list of rebinds, a level that keeps buffers only)

Server side only: `contents=buffers`; `efmi_list_writes bindings=true` and the `bind` lines of the export log, which list what each call has bound differently from the previous call of its kind (obtained by comparison, not recorded Set* calls). 33 offline checks, 102 sandbox end-to-end checks and all 16 steps of the full suite pass. The game was not open: no real-game verification, no key press.

**Next time**: after the user starts the game, run `captures\t013-contents-20261008\live_check.py` for the real-game check of T-013.

## Morning of 2026-10-09: The remaining write paths covered in the sandbox; real-game check of T-013

The contents scene of the sandbox got `ClearView`, `DiscardView`, `DiscardResource`, `UpdateSubresource1`, `CopySubresourceRegion1` and two kinds of UAV clears (37 calls, 73 checks in `contents_test.py`); the comparison with XXMI's dump now covers 42 calls, 546 buffer / texture files and 223 text files, all identical (commit `5116ff9`).

The user opened the game at 08:00 (it loaded probe 0.1.6). Read-only and without key presses, the real-game check of T-013 was done: UAVs of graphics draws are visible, the same `u0` is rewritten by 12 consecutive draws and the content of each is kept; the levels `buffers` / `inputs` copy about 206 / 624 MiB each with no visible change of the frame rate, and in the first 0.25 seconds after `outputs` it drops from about 70 to 60 frames; the buffers of a skinned draw, an 8192² texture and IniParams all read back as from the time of the call; the write list has 688 entries; exporting 6 calls takes 0.5 seconds. Results in `captures\t013-contents-20261008\`.

**Next time**: T-013 owes nothing any more (a byte-for-byte comparison with XXMI's dump in the real game needs its frame analysis key, which is not authorized; done in the sandbox). TangTang's LOD distance is still to be looked at when the user moves the character far away.

## 2026-10-09 08:29–08:32: The user asked for a live test of the latest capture capabilities

Reconnected to the real game PID 27452; the probe actually is 0.1.6, and the server used the production handlers of the current 0.1.13 on disk. Evidence was written to the new `captures/live-capture-20261009-0828/` without reusing the morning's results. A screenshot confirms an open-world scene with characters; throughout no input, no window / focus operation, no injection / restart, and EFMI and mods read-only.

The three capture levels with delayed read-back, the outputs of each graphics UAV draw, an 8192² texture, IniParams, writes / rebinds, the export of 6 calls and the release of resources all pass. The kept copy volume of `buffers` / `inputs` / `outputs` of six calls is stable at 203.4 / 609.3 / 663.0 MiB, the record time 4.99 / 5.15 / 5.59 ms, with no content lost. The limited scope of the outputs and the fluctuation of the scene have to be kept in mind; this is not a conclusion about the performance of outputs for a whole frame.

In addition frame 132830 was captured and complete buffers were read through the bindings of real XXMI draws: 76 resources of four character mods (16 IB, 60 VB, 104,470,324 bytes) are byte-identical to the read-only source BUFs, all `source=contents` / exact. The character bone pool of 379,392 bytes read back twice one second apart is unchanged, while the late-read content has changed, which directly confirms that the content of the old frame is kept. Results in `character-report.json`; pipeline state and VS / PS disassembly were saved as well.

The temporary acceptance script stopped once because it misjudged the type of the export range field; it was corrected and continued from the saved evidence, and the report of the first run is kept; no product problem and no code fix. In the end 16 kinds of capture checks pass, and the export directory was read back by this project's reader with 6 calls / 84 slots. Finally everything was released and disconnected, and the game kept running. Not done: a comparison with native XXMI frame analysis in the real game, an export of all resources of a whole frame, or special resource scenarios; the scope of the TangTang LOD task is unchanged.

Current state, tasks and this log were synced; that round committed / pushed only test records, with captures and game assets ignored and the plan document left out. Before pushing, an unpushed animation commit `824079e` was found on the local `main` and left as it was; the records of that round were pushed on a separate branch `codex/live-capture-verification-20261009` created from `origin/main`, to avoid publishing the old commit along with them. The actual commit / push result is authoritative for the Git state.

## Afternoon of 2026-10-09: Thick hair outline on characters with RabbitFX-style mods

The user reported that the hair of characters whose textures are bound through RabbitFX has a thick outline layer, and that the character on screen (`Typhoeus Luxuria 1.2 alpha`) had it. Read-only investigation: captures, the bindings and state of each pass of the hair, shader disassembly, the patterns of RabbitFX.ini. The hair outline is an extruded hull; the prepass writes depth and black, and the colour pass covers it at equal depth with "diffuse × 0.45". The main pattern of RabbitFX 2.7 takes the first sample of the colour shader (the mask of the alpha test) for the diffuse and the second (the diffuse) for the normal map, so with RabbitFX textures bound the alpha test uses the red channel of the diffuse and the colour uses the normal map; most of the hull is not coloured and black remains.

Verification: sampling the textures at the vertex UVs gives only 15.4% passing; measured on the picture, only 825 (16%) of 5140 hull pixels are coloured; with mods off through F11 (the same hair, the same shaders, `t70` not bound) it is 4642 / 4569. F11 was pressed 2 times in all, announced beforehand, and mods were on at the end. Replaying RabbitFX's pattern offline on the shaders from before the rewrite, which the probe keeps, reproduced the two wrongly assigned lines; a candidate change (touching only the `head` group) assigns the two hair outline shaders correctly and leaves 8 other shaders unchanged. Not verified in the real game; neither RabbitFX.ini nor a mod was touched.

Found along the way: the hash after rewriting that the probe reports can be mapped back to XXMI's original hash through the FNV of `ShaderCache\<original hash>-ps_regex.bin`, and the original shaders can be had from the probe too. Recorded in KNOWN_ISSUES and T-014, not implemented.

Evidence in the ignored `captures/rabbitfx-hair-outline-20261009/` (scripts, comparison pictures) and `captures/shaders/`. That round only updated records. The local `main` still carried the unrelated unpushed commit `824079e`, so the records were pushed separately as in the previous round, without publishing it; the actual commit / push result is authoritative for Git.

## Early evening of 2026-10-09: The hair outline fixed inside the Typhoeus mod

The user's decision: do not change RabbitFX, change the mod's INI, and put this part of the code into an INI of its own. `Typhoeus.ini` was backed up first, a new `RabbitFX_HairOutlineFix.ini` was added to the mod folder, and in the main INI only the line of the hair section that calls RabbitFX's `SetTextures` was changed to call the command list of the same name in the new INI.

The approach rests on one fact: the `filter_index` of an explicit `[ShaderOverride]` beats the tag of a ShaderRegex (looked up in 3DMigoto's `ShaderRegex.cpp`; a comment in this mod's `Typhoeus_Interaction.ini` mentions it too). With numbers of their own for the two wrongly assigned shaders, RabbitFX itself no longer puts textures into that pass; the new command list then binds the diffuse to `t72`, where the rewritten shader reads the outline colour, and clears it after the draw. The approach first imagined, recognising the pass by the vertex shader, cannot separate the two colour variants and was not used.

Measured after one F10 (announced beforehand): in the colour pass `t70` is empty and `t72` is the hair diffuse, cleared by the next draw; 5039 of 5100 hull pixels are coloured; the other passes are as before; the hair diffuse does not leak into other draws. Not tested live: the second variant, changing the hair colour, LOD, other characters. This INI also acts on the hair outline of other RabbitFX characters (no longer black, colour from the game's original texture), which the user was told.

Only records were updated; the product code was not changed. Git as in the previous section: the local `main` still carried the unrelated unpushed commit, and the records were pushed to a branch separately.

## Early evening of 2026-10-09: The unrelated commit removed from the local `main`; normal pushing restored

The user's decision: remove it from the local main and then push normally. The local `main` consisted of `acb0ad4` followed by the unrelated `824079e` (a pelican animation) and three record commits; the remote branch `claude/rabbitfx-hair-outline-20261009` had the same three record commits without `824079e`, and the files of the two differed only by `pelican-bicycle.html`. The local `main` was pointed at the tip of that line, `1188552` (the files of the working tree were not touched, and `pelican-bicycle.html`, formerly "deleted, not staged", no longer shows up), then pushed normally; `origin/main` fast-forwarded from `acb0ad4` to `1188552`, without a force push. `824079e` is on no branch any more.

The contents of the remote `codex/live-capture-verification-20261009` and `claude/rabbitfx-hair-outline-20261009` are all in `main`; the user then said to delete them, and both remote branches and the local `codex/…` branch of the same name were deleted (after confirming that both were ancestors of `origin/main`); the remote now has only `main`. From here on, commits and pushes go to `main` as usual.

## Evening of 2026-10-09: Answering whether this MCP can be used for Zenless Zone Zero / ZZMI

Read-only examination of `D:\XXMI-Launcher-Portable-v2.1.6\ZZMI`, named by the user. The two XXMI DLLs have exactly the same version and SHA256, and the probe has no restriction to the Endfield process name, so basic D3D11 capture can be expected to be reusable; but there is no test in Zenless Zone Zero so far. ZenlessZoneZero PID 18692 was seen, and the list of probe pipes was empty; nothing was injected, no key pressed, nothing restarted and no installation configuration changed.

The production code was used for a read-only mod scan and the key configuration pre-check: the scan rules work (0 active mods), and the two F10 bindings meet the requirement; the mod switch refuses because the code fixes EFMI's paths / F11, while ZZMI actually uses F6 / $costume_mods. Also established: the difference between track_region_hashes=0 and the tool's default presentation / matching by region hash; the LoyalTools model workflow still depends on EFMI's layout and the merged-skeleton project. Conclusions and scope were synced to CURRENT_STATE / TASKS / KNOWN_ISSUES. The user only asked about the capability, and no adaptation was implemented. Only the project memory was updated; the existing untracked `pelican-bicycle.html` was not touched.

The user then asked what a region hash is: `index_region` / `vertex_region` of `hashes.py` and the range clipping of the probe were checked, and it was explained as "one stretch of data in a shared large buffer", as distinct from an identifier of the whole resource; the IB range is exact, and the hash range of an indexed VB follows XXMI's estimation rule. The upstream release notes on XXMI region hashes and the ZZMI configuration were consulted; no game operation and no code / configuration change.

## Ardelia: the ribbons at both sides made translucent (2026-10-10; the mod changed at the user's request, with automatic refresh)

The user pointed at the black ribbons on screen and asked the agent to refresh and look at the effect itself. A matching HLSL / INI pair was added to the Ardelia mod, and the main INI, after a backup that is not loaded, was hooked in only three places. Capturing draw by draw showed that the background is drawn after the clothes, so it was changed to render with a private material and composite after the scene copy; all five normal F10 presses were confirmed, and the pixel / depth / motion / state restore checks at 50% and the comparison with the unchanged original at 100% pass. After the user pointed out that 50% was too transparent it was set to 85%, and the screenshot of the final frame 134483 was looked at. Evidence, boundaries and how to restore are in the first section of CURRENT_STATE and in the notes in the mod's Ribbons folder. The MCP product code is unchanged; game assets and captures do not go into the repository.

## 2026-10-10: Ardelia's arm accessories made translucent as well

The user asked for the accessories at both sides shown in a picture to be translucent in the same way. After isolating draw by draw in the running game, the upper-arm frills 63336/275721 and the wrist bows 70608/339057 were selected, each containing the left and right side. The main INI got two more hook points, and the transparency INI was extended with the selected meshes and display conditions, sharing the 85% default and Ctrl+T; the HLSL needed no change, and the original display switches are kept. Backups that are not loaded were made of both INI files and the notes first. One F10 in that round, reload confirmed / settled, and the actual screenshot of the equipment screen was looked at; the independent frame 201641 confirms the shading of both groups, zero modification outside the coverage, unchanged depth / motion, and restored temporary slots. Evidence in captures/transparency-20261010/hand-accessories/; the notes include how to undo only the accessory extension. The MCP product code was not changed; the open world / LOD / several instances are not verified.

## 2026-10-10: The first GitHub release with DLLs published

The user asked for a version with DLLs to be published on GitHub. The release installation notes of the README were committed as `77c1dfd` and synced to main, and the official Latest `v0.1.13` was published with the tag pointing exactly at that commit: [Release](https://github.com/loyal37/EFMI-MCP/releases/tag/v0.1.13). Server 0.1.13 / probe 0.1.6; the DLLs verified in the real game before are reused with identical checksums, with no change to product code or the probe build.

The assets are the complete Windows x64 package (with the Python server / four run-time helper scripts / two DLLs / a quick-start guide / BUILD_INFO / internal checksums), the separate `efmi_probe.dll`, `efmi_crc32c.dll` and `SHA256SUMS.txt`. The package was built from the sources of a definite Git commit and contains no uncommitted content of the working tree, no captures, no game assets and not the local plan document. With the staged probe, the regressions selftest 143 / contents 73 / MCP 102 and the 8 CRC checks were completed; after unpacking, the checksum verification, Python compilation and stdio initialisation / 29 tools / ping pass. After publication the four downloaded assets are byte-identical to the local ones, and the GitHub digests, manifest, tag, Latest and release state were checked. Evidence `captures/release-v0.1.13-20261010/release-verification.json`, with the other test logs in the same directory.

Git transport failed, so exactly the same Git objects were uploaded with the Git Data API and main fast-forwarded with `force=false`; remote history was not overwritten. The Ardelia memory changes that existed at the start of that round were kept, and only the release records of that round were staged. The user's game and installation directories were not operated on, and no new real-game verification was added; the release notes state that ZZMI is not adapted / tested yet. The release is complete.

## 2026-10-11: Repository documents rewritten

At the user's request the README, `AGENTS.md` and the seven memory files were rewritten in their entirety, keeping every fact, number, hash, path and date; `AGENTS.md` now states that everything in the repository is written in English, and that a string which has to match text another tool produces in a different script is spelled with `\u` escapes. Limits are now numbered `LIMIT-00N`. The uncommitted record edits of 2026-10-10 about the Ardelia mod were in the working tree and went in with the rest.

Three source files were adjusted accordingly without changing behaviour (`scripts/export_mod_blender.py`, `sandbox/mods_toggle_test.py`, one help text in `server/efmi_mcp/tools.py`). Compileall, six offline unit test scripts (25 + 56 + 33 + 41 + 60 + 27 checks) and `sandbox/mcp_test.py` (102 checks) pass. The probe was not rebuilt; the real game was not involved and no key was pressed.

The notes of release v0.1.13 on GitHub were rewritten as well; its assets were not touched. Earlier commits and the local-only plan document stay as they are.

**Next time**: nothing pending from this round. Open items are unchanged: the LOD distance for TangTang (T-012), the other RabbitFX-style mods and the hash mapping for rewritten shaders (T-014), and ZZMI adaptation if the user asks for it.

## 2026-10-16: Batch texture slots and in-memory shader queries

The user observed agents repeatedly writing helper files to obtain texture slots. Implemented server 0.1.14's `efmi_get_texture_slots`: filtered/paged draw batches grouped by shader, material bindings and targets, with captured texture metadata and static sample/load/query evidence. A per-connection bounded shader LRU serves both slot inspection and full assembly paging. `efmi_get_shader` writes nothing by default and exports only with `save=true`; the MCP instructions direct agents to the batch tool. Probe 0.1.6 and the protocol are unchanged. README, architecture, D-023 and the standard test runner were updated.

Validation: dedicated tests 25, contents 33, draw outputs 41 and private-sandbox stdio MCP 106 all pass; 48 server/sandbox Python files compile. New code/test strings/comments are ASCII English. Live stdio connected to the existing Endfield PID 35796, captured frame 92208 and queried 125 mod draws / 28 pixel shaders / 31 groups; 237 binding hash/format comparisons with the existing draw tool pass. New-tool cold query 156.922 ms, five warm queries median 27.696 ms with identical results; zero output files, including subsequent shader paging. This measures new cold/warm behavior, not a baseline of historical agent scripts. No visual judgement, new injection, input/reload/toggle, restart, mod edit or installation write. Evidence is under `captures/texture-slot-query-20261016/`, ignored by Git. Earlier uncommitted material/conversion memory edits were preserved; only this task's additions are staged. Code is ready after restarting the MCP server; no game restart is needed.

## 2026-10-18: Probe slot cache (probe 0.1.7)

The user wants slot-style texture bindings to survive game updates with a general tool, without texture hashes, and proposed a slot cache that fills itself while characters are switched. Built the probe part: one frame in 30 is looked at, every skinned indexed draw not seen before is appended to `slot-cache\<label>+<fingerprint>\draws.jsonl` next to the DLL (IB hash and range, VS/PS, slots with kind/size/format, output formats), and shader bytecode is kept once. The version key combines Unity's client version with a fingerprint of both resource indexes and the client files, because the 10-15 slot move came with a resource update. Settings in `[SlotCache]` of `efmi_probe.ini` and the probe command `slot_cache`; no MCP tool yet.

Self-review fixes: cut-off last line, folder change without new draws, stack walk only for new draws. Verification: `slot_cache_test.py` 65, full `run_all.py` 18/18 on the final build (one transient missing XXMI dump on the previous build), and slot cache / self-test / MCP again on the installed DLL. No measurable cost in the sandbox at about 3000 draws per frame. Installed 0.1.7 in `dist` with 0.1.6 in `dist\backup-0.1.6\`; the game was closed. Not verified in the real game. Next: check the cache after the next game start, then step 2 of T-016 (reading and comparing versions). Other sessions' uncommitted memory edits were preserved; only this round's additions are staged.

## 2026-10-18: First real-game run of the slot cache; records keyed by content (probe 0.1.8)

The user started the game with 0.1.7 and thought the cache was not growing; it had written 4881 records in 3.6 minutes into `dist\slot-cache\1.6.3+5e73da7cad5e\` (version recognised correctly) and grew in steps with scene and character changes. Analysis of the records and one read-only capture (no key press) showed that the game places meshes at changing offsets of shared buffers and that screen-sized textures follow the dynamic resolution, so position and sizes in the key made duplicates of known passes. 0.1.8 keys records by content (IB region hash, count, shaders, slot kinds/formats, output formats), drops the base vertex from the seen-draw key, marks records `"fmt":2` and moves older files to `draws-fmt1.jsonl`. Replaying the real records: 1952 instead of 4881. Tests: slot cache 70, self-test 143, MCP 106 (parity skipped while the game had the foreground). Installed by moving the loaded 0.1.7 to `dist\backup-0.1.7\`; the game loads 0.1.8 at its next start. Next: confirm 0.1.8 in the game, then T-016 step 2.

## 2026-10-18: Slot cache 0.1.8 checked in the game

The user restarted the game (0.1.8 loaded) and went through every character with the mods switched off. Analysis only, no code change and no key press: one read-only capture showed 0 XXMI draws and Yvonne drawn natively. The folder holds 2153 records (809 meshes, 222 pixel shaders, 314 bytecode files) and stopped growing when the browsing ended; 0.2 s of probe time over 602 looked-at frames. The 20 installed mods name 294 components; 164 have native records, the gaps being LOD components and hashes the game no longer draws. The earlier version folder had been removed before this start, so the move of 0.1.7 records was not exercised in the game. The mods were still off at the end of the round; the user was reminded to switch them back on.

## 2026-10-18: Slot-style mods checked against the slot cache

At the user's request the installed mods' `ps-tN` bindings were checked against the native records of the current version (read-only; LOD components skipped). 51 of the 64 slot-style components with native records are consistent; one texture is encoded as sRGB where the game expects linear (Ardelia's light map), and three RabbitFX components set to null a global HDR texture one slot above the character's fourth texture. Findings in CURRENT_STATE; nothing in the mods was changed. The check is a script in `captures/slot-mod-check-20261018/`; turning it into a tool belongs to T-016.

## 2026-10-18: Three RabbitFX nulls moved one slot lower (user's mods, at the user's request)

Laevatain (`ps-t21` -> `ps-t20`) and Perlica (`ps-t20` -> `ps-t19`) changed after same-folder backups (`*.ini.bak-20261018-null`); Estella was already at `ps-t19`, saved outside this session minutes earlier, and was left alone. Offline re-check passes; no F10 was sent, so the game has not loaded the change.

## 2026-10-18: Resume slot-table work and expose cached MCP queries

Restored CURRENT_STATE/TASKS and reviewed the untracked table/test draft plus existing decisions. Completed an offline native slot table with per-shader static references, view formats, pipeline variants, uncertain role labels, mod IB/count naming and explicit report exports. Removed texture-hash-based classification; unknown/conflicting evidence withholds roles. Added file-free efmi_get_slot_table with filters, paging, one-version memory cache and invalidation on cache/shader changes. Server 0.1.15; no probe change.

Tests: 19 slot-table and 25 existing texture-slot tests passed. Full server registration exposed 31 tools. Fixed real-cache snapshot: 5286 native records, 3885 meshes, 20 mod names, 5215 analyzed passes, all returned slots consistent with static references. No query file writes. First build final run 2273 ms, warm 24-57 ms; earlier cold 4517 ms. First attempt against a growing live cache was stopped (only the project's checker process); fixed snapshot used for repeatable verification. Reports in captures/slot-table-check-20261018.json and captures/slot-table-snapshot-20261018/slot-table.md. No game keys, capture, visual validation or installation edits. Shader migration/fixer remains pending. Reviewed changes and staged only this round's memory additions, preserving prior unrelated edits.

## 2026-10-18: Correct primary slot selection after user review

User identified low-numbered primary slots and incorrect role ordering. Reproduced 3498 low-slot selections in the old snapshot, including 16 mod-named meshes; Yvonne IB510debd5 used unrelated t6 sRGB and IB119b1b29 only had auxiliary t3. Fixed primary selection and format triples, preserved raw auxiliary data, exposed primary/null and status in MCP rows, rejected reversed role evidence without swapping, and reordered report columns diffuse/light map/normal map. Server 0.1.16. Tests 23+25 pass. Regenerated fixed snapshot report: 252 candidates, 3633 unknown, zero order violations; Yvonne candidate t21/t22/t23. All 5215 shader pass reference checks pass. Growing dist cache report regenerated separately (8939 meshes). No live capture, key input, visual proof or installation edits. Memory additions staged separately from preexisting unrelated work.

## 2026-10-18: Rebuild slot table independently, then compare mods

User rejected treating their slot impression as a rule and required native per-IB analysis before mod comparison; later clarified only primary slots belong in the material table. Removed the old order/threshold/format/mod inference. Added component provenance with local normal/RGB/lighting evidence, conservative branch alternatives and unsupported-flow limits. Native build API takes no mod inputs; comparisons are attached later and cannot alter native data. Default MCP/Markdown show primary only, separate audit JSON preserves all passes. Cache appends reuse shader analyses. Server 0.1.17, probe unchanged.

26 slot/dataflow tests and 25 existing texture-slot tests passed, including register permutations, deliberately wrong mods, raw format/hash changes, swizzle/masks, overwrite, conditional alternatives, loop/early-return/gather uncertainty, primary-only output and cache reuse. Offline snapshot independently analyzed 13577 meshes / 240 PS / 15027 passes, all shaders available, 2654 evidence paths checked against saved assembly. Final 111 primary candidates; after inference, mod comparison found 67 agreeing declaration checks and 61 unresolved across 164 named components. Native digest unchanged by comparison (d7e6ad8b...c799299). First build 26.1 s, warm query about 6 ms, no query writes. Reports in captures/slot-table-rebuild-20261018. No game input, live capture, visual proof or installation modifications. Prior unrelated memory edits preserved; only this round's memory changes staged.

## 2026-10-18 - Restore captured IB coverage and retain primary shader variants

- User asked why the table showed so few IBs after browsing almost every character. Audited the prior full evidence: 164 native mod-labelled keys were present, but Markdown showed 81; 34 were suppressed by role ambiguity, 16 by differing primary shader mappings, and 33 by insufficient primary evidence. Across the old snapshot 111 rows were displayed, 78 partial candidates and 25 differing-mapping candidates were suppressed. All earlier record keys are still in the current cache.
- Implemented server 0.1.18/schema 3: per-pipeline primary variants, per-role uncertainty, shared roles only with cross-variant agreement, capture/filter coverage, explicit unresolved reasons, and shader-context-required INI comparison. Primary-only Markdown now retains all captured keys, showing labelled unresolved entries and an expandable unlabelled unresolved inventory. Native inference remains independent of mod labels/roles, formats and slot numbering/order.
- Tests: slot_table_test.py 31 and texture_slots_test.py 25 pass; complete server registration 31 tools/read-only slot query. Read-only fixed snapshot of growing cache: 63120353 bytes, SHA256 372d2f2f72031e9915432d59dca2be8e284b5149d9a833963e645f3b09f9fa0d; last record 14:35:04 local. 32306 native records / 30668 keys; 214 primary candidate keys / 248 variants. Labelled: 164 total / 131 primary candidate keys / 148 variants / 33 unresolved. 2654 instruction paths checked; every native key in both material exports; no missing analyses; mod comparison preserves native digest. Declared roles: 89 candidate matches / 29 unresolved / 10 requiring shader context. Cold build 28.381 s; cached label queries 36.6-37.9 ms, zero writes.
- Evidence under captures/slot-table-coverage-20261018; earlier artifacts retained and marked superseded. All game/mod/installation files read-only; no input, F10/F11, new frame capture, visual verification, DLL/probe change or game restart. Existing unrelated memory edits preserved outside the staged change. Reviewed code and tests; commit/push required by working agreement.
- Remaining: unresolved shader/primary semantics, roster identity, cross-version migrations. Also recorded cache growth from 29868 changing IB/count keys sharing PS92dc6757dbd7cfb3; no object/effect identity or probe mitigation claimed. Captured key totals must not be presented as character coverage.

## 2026-10-18: Slot cache filters for character draws (probe 0.1.9)

The user found the cache flooded after playing in the open world: 44,104 records of draws from a DYNAMIC index buffer the CPU rewrites every frame, each with a new hash. At the user's suggestion the filter was aligned with LoyalTools' draw selection (EFMI-Tools' raw object extractor: bone pool at vs-t0, weight semantics) plus static index data. 0.1.9 adds `bones_only` and `static_only`, records `ib_usage`, and the sandbox gained `--dynamic-ib` for the test. Slot cache test 74, self-test 143, MCP 106; parity skipped while the game has the foreground. Installed by moving 0.1.8 aside; the cleanup script for the existing file waits for the game to close. Codex's slot table rewrite was assessed read-only (dataflow agrees with the mods 89/89; presentation and defaults are the problem); the rework was proposed, the user has not decided.

## 2026-10-18: Old slot tables removed, slot table reworked (server 0.1.19)

The user deleted the old cache and asked for the previous slot tables to be cleaned up and the rework to proceed. Removed Codex's slot-table folders from `captures/` (about 343 MB) and rewrote `slot_table.py`: one row per mesh with diffuse, light map and normal map of the lit pass, decided from dataflow, position, formats and the same shader, never from mods; the mods label and check. Found 17 meshes drawn by two lit shaders that read the same textures at slots two apart, so a mod's binding is wrong for one of them. On the cache at 15:34: 3,788 meshes, 18 mods name 149, 109 declared slots agree, none contradicts, 2 open, 14 wrong for another lit pass. Tests: slot table 14, dataflow 12 (moved from Codex's file), texture slots 25, MCP 106. No game input; the probe was not changed.

## 2026-10-18: Slot table: no depth or G-buffer main passes, LoyalTools' sequence

The user saw t0/t1 and other low slots their mods never use. They came from G-buffer passes of deferred open-world meshes, depth passes and unlit effect passes taken as main passes; these are now excluded or listed apart. LoyalTools' texture marking was read (sRGB start, linear followers, BC5 hint, preference for slot 10 and up, lower start, square textures of 256 KB and more) and compared: it agrees with the table on 112 of 122 lit named meshes when the table's material textures replace its file-size filter. Its lower-start preference and its sequence for filling a missing diffuse or normal map were adopted. Slot table test 16.

## 2026-10-18: Slot table checked live on Chen Qianyu

At the user's request the character on screen (Chen Qianyu, unmodded) was captured and compared with the table: all main shaders drawn with the named textures, and the read-back textures confirm body, hair and face roles; the face's diffuse had been left open between two sRGB textures. A new basis takes a role from another pass of the same mesh that resolved it for the same texture, which settles the face (t18 skin, t20 shadow map). No key press; evidence in `captures/slot-table-live-check-20261018/`.

## 2026-10-18: Translucent skirt panel for Yvonne (`F:\EFMI TTL`)

At the user's request a blended translucency was built in the separate workspace `F:\EFMI TTL` and deployed into the part of `Yvonne Ensnared Heart 1.8` the user named (one line of `yvonne.ini` replaced, backup `yvonne.ini.bak-20261018-translucency`, new `EFMITTL\` folder in the mod). The first F10 showed the part skipped in the forward pass but never captured: the INI matched the probe's hashes of pixel shaders that RabbitFX's ShaderRegex rewrites, not XXMI's. Mapped through `ShaderCache\<hash>-ps_regex.bin`, and switched to the lit vertex shader's filter index so as not to replace RabbitFX's 1718.1 tag. The second F10 gave both layers and the composite (24928 px changed, none outside the part); at the user's request the opacity went to 70% (third F10; measured 0.702). Ctrl T and motion are not checked. Committed in `F:\EFMI TTL` (`70003bc`); this repository's code is unchanged.

## 2026-10-21: Continuous mode over time and a triggered capture (probe 0.1.10 / server 0.1.20)

The user asked whether a boss's skill, which lasts a moment, can be read, and then asked for continuous mode to be completed. The probe's stream now stores each distinct line once and adds a stretch of frames up itself (`live_summary`); a trigger watches every frame for an IB hash, a shader hash or a new vertex/pixel shader pair after a baseline and records the next frame in full, contents included. The server got `efmi_live_history` and the three trigger tools; the sandbox a flashing mesh (`--flash`). Sandbox only: continuous test 62, unit test 20, full regression 22/22; installed as `dist\efmi_probe.dll` (0.1.9 kept in `dist\backup-0.1.9\`), the game was not running. Not checked in the real game; that needs a restart of the game, no key press.

## 2026-10-21: Recording switched by the user (probe 0.1.11 / server 0.1.21)

0.1.10 was tried in the game: continuous mode 0.75 ms per frame; the new-pair trigger caught the user's dash (a glow particle, frame 35110 in full); the look back lost part of three dashes to its one-minute window and showed volatile batches as news (merged in the server since). At the user's request a recording switched by Ctrl+F9 in the game (or `efmi_record`), written by the probe into `captures\recording-<date>-<time>\`: runs of frames per mesh/shader line, per-frame counts, keyframes at the start, on new shader pairs and every ten seconds; the server reads it back (`efmi_record_history` events / recurring / lines, `efmi_record_keyframe`). Sandbox only: record test 46, unit tests 9 + 21, full regression 22/22 with the candidate. Installed in `dist\` (0.1.10 kept in `dist\backup-0.1.10\`); the game was not running.

## 2026-10-21: Zenless Zone Zero (ZZMI) support, first part

- Server 0.1.22 / probe 0.1.12: installation from the connected game, whole-buffer hashes for ZZMI, F6 mods switch with the state from the draws (stricter draw rules for both games), SO targets per call, `efmi_dump_frame log=xxmi`. Details and the scope of verification in CURRENT_STATE; next steps in T-019.
- The key-press sandbox tests could not run to the end: the user was working in Blender and then playing Zenless Zone Zero (elevated) in the foreground. Not a fault of the change, but they are still to be run.
- The user's new request mid-round: the model workflow should use LoyalTools' new ZZZ support; LoyalTools 1.7.1 extracts the user's dump outside Blender (reference workspace in the session's scratch folder).

## 2026-10-21: Zenless Zone Zero model workflow on LoyalTools' ZZMI support

- Server: `model_zzmi.py` and two Blender scripts drive LoyalTools 1.7.1's own ZZMI extraction (merged skeleton) and export; the tools choose the game by connection, `frame_dump` or project. The dump writer writes files only for the calls LoyalTools needs (`files_for`); the in-game comparison skips stream-output vertex buffers and keeps texture-only ZZMI entry points as kept by the export.
- Correction of the first part: the skip-section rule of the mods switch applies to ZZMI only (LoyalTools' EFMI entry points have no `handling = skip`; EFMI's core replaces the draw).
- Offline: 21/21 on the user's dump through the MCP tools, 6 unit tests incl. LoyalTools reading a dump this server wrote. Not in the game yet; the key-press sandbox tests are still pending (the user was in the foreground).

## 2026-10-21: Zenless Zone Zero in the game

- The user started the game with the probe; checked through the new server code: adoption, hashes, stream output, 4 announced F6 presses (mods on at the end), a live extraction equal to the F8 dump's, an export equal to the F8 project's. Details in CURRENT_STATE.
- Three fixes from what the game showed: indexed draws only as draws of a mesh (skinning Draws keep an index buffer), XXMI's hash for stream-output buffers a mod's VertexLimitRaise enlarged, typeless textures in their view's format. Unit tests added; offline suites and MCP end to end 106 pass.
- Open: deployment with F10 (needs a decision about the user's phoenix mod), the key-press sandbox tests.

## 2026-10-22: Slot cache off by default with ZZMI (probe 0.1.13)

- At the user's request: the probe decides the slot cache's default by the loaded installation (on for EFMI and without XXMI, off otherwise); an explicit setting still wins; flush-only requests no longer count as settings. Switched off in the running game's 0.1.12 by command. Sandbox: slot cache test 80/80 with a new ZZMI case, plus the other probe regressions without key input.

## 2026-10-22: Two characters extracted from one Zenless Zone Zero scene

- `exclude_ibs` for live ZZMI extractions (the merged skeleton takes the largest character only), and the Blender import check now allows triangles Blender merges (same vertex set) with a warning. Two characters extracted live into `E:\ZZZ mods\character-c0975bbf` and `character-e12e13f8`; no key pressed. Unit tests for leaving out a mesh; offline suites pass.

## 2026-10-23: EFMI TTL 0.2.0, opacity from the calling mod

The user found the opacity key in the shared library wrong: like RabbitFX, the library should only offer an interface. `$\EFMITTL\alpha` is now a per-call parameter, stored per pixel through a second draw with the library's own pixel shader, and Yvonne's mod got its own variable and Ctrl T key (backup `yvonne.ini.bak-20261023-mod-alpha`). Offline checks only; the game was not running. Committed in `F:\EFMI TTL` (`96d1f76`); this repository's code is unchanged.

## 2026-10-23: EFMI TTL, the opacity written as a value

The user: the adjustable value should not be fixed as a variable; a mod may pass a number or a variable, and by default a plain value. Yvonne's call now reads `$\EFMITTL\alpha = 0.7`; the variable and key added earlier the same day were removed (backup `yvonne.ini.bak-20261023-alpha-variable`), so her INI differs from the original only in that call. The tutorial shows the value first and the variable with a key as an option. `F:\EFMI TTL` commit `faeadd6`; offline only, the game was not running.
