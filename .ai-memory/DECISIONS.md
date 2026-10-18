# EFMI MCP — Technical decisions

## D-001: Fix inside the project; EFMI/XXMI stay read-only

- Date: 2026-10-05.
- Decision: changes are limited to `F:\EFMI MCP`; the original EFMI/XXMI DLLs, INI files, Core, the launcher configuration and existing mods are not written to.
- Reason: the user explicitly asked not to touch EFMI and to change only our own tool, which also matches the boundary already set in the README.
- Implementation: tests use the private copy in `sandbox/run/`; from the reference `E:\loyal tools\.ai-memory` only the structure is read, and nothing of that unrelated project is modified or brought in.
- Addition of 2026-10-06: the user authorized the tool to send the normal F10 reload automatically; the original files above are still not edited directly, but a normal XXMI reload may save persistent variables on its own. "The tool does not edit files" must not be taken to mean "F10 has no side effects on files".

## D-002: Memory split by seven responsibilities

- Date: 2026-10-05.
- Decision: overview, architecture, current state, decisions, issues, tasks and session log are kept in seven Markdown files; the root `AGENTS.md` is the entry point for resuming and maintaining them.
- Reason: keep facts and verification boundaries across sessions instead of relying on chat recollection.
- Maintenance: current state and tasks reflect the latest facts; the log gets appended summaries; decisions/issues are updated when there is a new conclusion. Historical measurements keep their source and scope.

## D-003: Device, context and frame boundary must agree

- Date: 2026-10-05.
- Decision: `hooks_attach_device` separates hook installation from target selection; the first swap chain found selects the target with `GetDevice` / `GetImmediateContext` and holds references. An auxiliary device can still be hooked but must not override the target. The `context1` capability belongs to each context; the GPU writer tracks only the selected context and requires a swap chain to have been found.
- Reason: the real game creates additional devices; the old main swap chain still provided Present while the new context had no draws of the main picture, which produced captures that looked connected but were entirely empty.
- Boundary: once selected, a replacement swap chain of another device is refused with a hint to restart; neither automatic recognition of the main picture among several swap chains nor complete captures across a re-creation of the swap chain on the same device is promised.
- Status: implemented in `0.1.1`; the `192` checks of the four multi-device scenarios and the final full regression pass; the new version has passed non-empty frame and real resource read-back verification on the Yvonne screen of PID `38252`.

## D-004: Verification recorded by layer

- Date: 2026-10-05.
- Decision: record separately injection/connection, non-empty frames and continuous data, resource read-back, and recognition of character-related data; keep sandbox and real-game results apart.
- Reason: all historical sandbox tests passed and still did not cover this multi-device scenario of the real game. Recompiling the DLL on disk does not update the existing game process either.

## D-005: Continuous mode isolates sessions by generation

- Date: 2026-10-05.
- Decision: a live capture records the generation at its start; a stop (`enable=false`) or a disconnect advances the generation, and at publication in-flight frames that are stopped or of an old generation are refused. Re-configuring parameters with `enable=true` does not change the generation.
- Reason: emptying the live ring buffer alone is not enough to stop an old frame that is being captured from being back-filled a little later; the first self-test run therefore showed `71` passed / `2` failed.
- Status: the fix and `10` new regression checks are built; the self-test shows `83` passed / `0` failed, and the final full regression passes.

## D-006: Sandbox shaders get a stable floating-point baseline; strict hash assertions stay

- Date: 2026-10-05.
- Evidence: the posed VB may still have `39` floats changing by `1 ULP`, at most `5.96e-8`, without a dump and even without XXMI; each time the probe hash agrees with the CRC of the read-back, so the buffer content really changes.
- Decision: use HLSL `precise` in the test shader to stabilise the byte baseline, rather than loosening hash or byte comparisons to hide the difference. The old probe without XXMI reproduces the change too, so it is not a regression of this round; the exact compiler optimisation mechanism is only inferred.
- Status: the test scene is fixed; with and without XXMI, the `11616` bytes of posed VB of `12` consecutive first-frame captures each are byte-identical, CRC `1b4be723`. The second-capture consistency checks of all three parity runs of the final full suite pass.

## D-007: Commit and push code unprompted after finishing a change

- Date: 2026-10-05.
- The user explicitly asked to remember to commit the code after every change.
- Original decision (2026-10-05): after each round of changes has passed its verification, sync the project memory, review the diff and create a local Git commit; automatic push was not authorized at the time.
- Update (2026-10-06): the user explicitly asked that this round and future rounds also be pushed to `https://github.com/loyal37/EFMI-MCP`. The new agreement is: verify, sync the memory, review the diff, commit and push to the specified repository; remote history is not force-overwritten. "No push" in the historical log remains a fact of what was done then and is no longer a current constraint.
- Addition (evening of 2026-10-06): the user confirmed that the agreement applies to Claude sessions as well ("commit and push as required, and the same from now on").
- Raw capture files stay in the ignored `captures/`; local code commits do not mix in game screenshots, large binaries or build output.

## D-008: Reuse the original merge algorithm; verify the static-editing contract with several samples

- Date: 2026-10-06.
- The user named the reference E:\loyal tools and asked for the current character to be written to E:\Endfield mods\liimo merge; that output directory is an explicitly authorized exception, while EFMI and the reference tool stay read-only.
- Use LoyalTools' native character recognition, exact de-duplication of bone matrices and workspace writing; do not copy the bone mapping of other characters, and do not call an ordinary OBJ export a merged-skeleton extraction.
- 0.1.1 offers no snapshot of the bone SRV as it was at the draw, and the address of the game's bone pool changes dynamically. The goal of the task is an editable static mesh with global vertex groups, using three independent samples: the complete geometry is unchanged, canonical (component identity, local VG) agrees, the weighted merge across components is not empty, and the bone matrices are valid and really change between samples. The report states the late-read time and promises neither the pose of a specified frame nor all animation actions.
- After generating the native workplace, it is really imported in a background Blender with the installed add-on, and a new project is saved only when verification passes; the user's existing workspace or blend is not overwritten.

## D-009: Optimise the processing outside the game first; keep full verification

- Date: 2026-10-06; the user reported that reading draw data was slow.
- After measuring, an independent C ABI CRC32C helper was adopted (loaded by absolute path, CPU detection, self-test, Python fallback), without changing the injected DLL or forcing a new pip dependency. The old incremental seed and buffer semantics are kept.
- Compact JSON saves encoding cost; PNG with low compression is lossless but the files are slightly larger; the full picture and the thumbnail of one MCP read share a single decode, and live textures are not cached across frames. The second character identity check only skips the VB hashes and keeps IB, CB and the existing identity thresholds; the complete geometry validation of the main frame is still performed.
- The bridge cache is limited to one offline archive export; it first checks the complete size/SHA and then gives the same verified bytes to the native reader. The byte-for-byte geometry / canonical bone group consistency of the three samples, changing bone matrices and a non-empty weighted merge must not be cut.
- Performance evidence is labelled separately as microbenchmark, real complete archive, and offline conversion; the roughly 924-fold speed-up of the CRC must not be claimed as an improvement of the whole capture path. The median of three real archive runs improved by 33.8%; the cost of trading PNG space for time is recorded explicitly.

## D-010: A single F10 request and the completion of the reload are proven separately

- Date: 2026-10-06; the user asked for the MCP to be changed so that the agent presses F10, waits and captures again automatically.
- Decision: the server gets `efmi_reload_and_capture`, with a fixed F10 input through Win32, reusing the probe's existing capture; the server goes to 0.1.2, the probe stays at 0.1.1, and only the MCP has to be restarted. No hunting mode, no change to Core/the launcher, and no re-injection into the existing process.
- Thresholds: the same pipe/PID, the current EFMI installation, the selected device/context/swap chain; the original `reload_config` and `reload_fixes` of this installation are each a single `no_modifiers VK_F10`. The effective foreground window and the modifier key state are confirmed; if they cannot be confirmed an error is reported, without forcing the focus or elevating rights automatically.
- Data boundary: stop live mode, release, clear old captures and the mod index, wait the default 2000 ms after the single F10, then establish the barrier and capture a strictly later non-empty frame; the target is re-checked before and after the new frame. Live mode stays stopped, and after an error following the input the key is not resent automatically.
- Proof boundary: `reload_requested=true` proves only that the input was sent; the probe has no reload receipt, `reload_confirmed` stays false, and the target mod still has to be verified through draws/resources. On an exception the sent / not sent / unknown state is kept; a normal F10 can make XXMI save persistent variables, so the tool cannot be declared completely read-only.
- Verification at the time: 19 offline input checks, 30 workflow/MCP boundary checks and the existing 39 MCP checks passed; a real F10 in the private sandbox under hunting=0 made the target skip rule take effect, evidence `sandbox/build/reload-xuxnsq73/reload-evidence.json`. The later real-game test of that round failed, see the update in D-011; historical sandbox results cannot replace the real game.
- Input boundary: during one F10 down/up pulse of about 200 ms, XXMI 1.2.2 re-creating its key registrations may trigger several internal reloads. Only a single input by the tool without resending is guaranteed, not that XXMI performs exactly one internal reload; the notes about the missing receipt and the normal saving of persistent variables stay.

## D-011: Input accepted by the API and the pressed state are verified separately; the real-game failure stands

- Date: 2026-10-06; the user moved Yvonne out and explicitly confirmed that the automatic F10 did not unload that mod. Two ordinary VK sends were accepted by the API and new frames were read normally, yet the actual refresh failed; the scan-code diagnosis further showed that the high bit of F10 was not visible during the whole hold, so scan codes are not taken as the fix.
- Decision: the server goes to 0.1.3 and samples GetAsyncKeyState during the existing hold of about 200 ms, continuing only when the high bit is seen at least once; API acceptance is written to `key_down_sent`, visibility of the state separately to `key_down_observed`. When the press is not observed, the input stage returns `keydown_not_observed`, `finally` tries to release, there is no capture and no resend; the receipt boundary `reload_confirmed=false` is unchanged.
- This is a fix of error detection; the cause of the real input being invisible is not located, and the real-game goal of T-007 is still not reached. From the current Python being medium integrity and access to the target token being denied, no specific permission or security-software cause can be concluded; no automatic elevation, no change to EFMI/the launcher, and no internal function address or non-public entry point in place of a supported interface.
- Source basis: in the official v1.2.2, [input.cpp](https://github.com/SpectrumQT/XXMI-Libs-Package/blob/v1.2.2/DirectX11/input.cpp) polls GetAsyncKeyState; [IniHandler.cpp](https://github.com/SpectrumQT/XXMI-Libs-Package/blob/v1.2.2/DirectX11/IniHandler.cpp) does not write user.ini when nothing is dirty. An unchanged file time is not a failure criterion; the user's observation and the old mod still being drawn are the actual evidence of this failure.
- Verification: in that round the 21 input checks, 32 workflow checks, the 39 checks of the MCP stdio private sandbox and compileall passed. The user's game was still running, so the real-F10 sandbox test, which refuses other probes, was not re-run; real input is not repeated automatically. The scan-code report and an independent assessment are in `captures/verify-reload-scancode-41976-ready/`, keeping the original evidence and not relabelling the diagnostic pass as a successful reload.

## D-012: The reload key is pressed by the probe inside the game process, and re-created resources are the evidence of the reload

- Date: evening of 2026-10-06; the user asked that the MCP really be able to perform the F10 refresh (the external key press done by Codex before had no effect in the real game).
- Basis: the game's manifest says `requireAdministrator`, the MCP server has ordinary rights, and UIPI makes an external `SendInput` ineffective without an error. The probe is already inside the game process, at the same integrity level.
- Decision: probe 0.1.2 provides a fixed-function `reload_key` (it can only be F10, is sent only to this process's window in the foreground, and checks the default bindings); server 0.1.4 uses it when the probe supports it, and keeps the external path for an old probe with a hint to restart the game. No automatic elevation, no change to XXMI/EFMI/the launcher, no call into XXMI's internal functions, no change to XXMI's import table.
- Update to D-010/D-011: with `focus_game=true`, when taking the foreground normally is refused, borrowing the input state of the foreground thread (`attach_input`) is allowed, and by default the focus is given back to the original window; no other key such as Alt is sent and no system setting is changed. `reload_confirmed` is no longer always false; it is now decided by "XXMI created, after the key press, resources/shaders that the new frame uses"; this is behavioural evidence, not a receipt from XXMI.
- Boundary: the pipe is open to processes of the same user, so the command accepts neither a key value nor a target window parameter. If the in-process key press were still filtered in the real game, that would be regarded as another layer of protection and not bypassed. The sandbox is not an administrator process; until it passes in the real game this task does not count as done.

## D-013: The model editing loop reuses LoyalTools; preview materials are diffuse only (implemented, see D-014)

- Date: 2026-10-06; the user explicitly asked for model extraction, export of game INI / BUF after editing in Blender, strict use of LoyalTools' export logic, and a refresh after export to look at what the target mod actually draws.
- Approach: reuse the native complete blueprint export entry point with its pre- and post-processing; the MCP manages jobs, file identity, output checks, deployment into the specified test folder and the authorized normal F10, and writes no substitute INI / BUF generation algorithm.
- Blender connects only the diffuse texture; the other original DDS / slot information is still kept for the game export. The export pre-check allows legitimate edits and does not apply the import-time condition that the original vertex count is unchanged.
- Acceptance: distinguish valid export output, consistent deployment, reload evidence, and what the target mod actually draws this time; check BUF / textures per component, and state the main / LOD and branch coverage clearly. The detailed plan is in `docs/model-extraction-export-plan.md`. When this entry was written T-008 was not implemented yet; how it was implemented and what was verified is in D-014.

## D-014: The model workflow is built on integrating LoyalTools

- Date: 2026-10-07; the user asked for the model workflow to be integrated according to the plan document, with LOD matching added, and then made clear that it should mainly be an integration: otherwise an update of LoyalTools would force an update of the MCP as well.
- Decision: only call LoyalTools' entry points and keep no copy of its algorithms. By default the add-on installed in the selected Blender is the one and only LoyalTools, and the server process and Blender run the same code; another directory is used only when configured explicitly, and the result states path, version and origin.
- LOD: frame matching calls `map_merged_skeleton_lod`, and each of the two frames can be a frame analysis directory or a probe archive; JSON matching calls `restore_merged_skeleton_lod`. Main model and LOD share one edited mesh, and entry points and buffer variants are generated by the add-on at export; LODs that were not captured are not inferred.
- Checks do not depend on the add-on's naming or the wording of its log: success or failure of an export is decided by the output, a failure statement in the add-on's log acts as a veto; the in-game check goes by buffer content and follows the `run` / `ref` references of the INI to connect entry points with files.
- The project directory is the root of a LoyalTools custom workspace (the directory name is the mod name); the `project/` level of the plan is dropped, matching the layout of the user's existing projects.
- Write boundary: deployment writes only into a directory under `<EFMI>\Mods` that this tool created and that carries the marker file; old files that are replaced are moved to the project's `verification/`, outside what XXMI loads. All other EFMI content stays read-only (AGENTS.md updated accordingly).
- Unresolved coupling: the archive extractor replaces a few internal methods of `DumpWorkspaceExtractor`. Writing the probe data in the format of a frame analysis directory would remove this dependency; it should be done only after a comparison with XXMI's frame analysis while the game is running.
- The plan document had been kept local at the user's request. During implementation it was once committed along with the code (three commits starting at `5518332`), after which the user said not to upload the plan document to GitHub: it was removed from the repository and added to `.gitignore`, the file stays local, and the README now carries its own project layout and LOD description. The history of those three commits still contains it; removing it completely would need rewriting history and a force push, which is not done without the user's explicit consent. From then on the "local only" requirement is not overridden by a general "commit and push".

## D-015: Switch mods with EFMI's own F11; the state comes from EFMI's notification

- Date: 2026-10-07; the user asked that the agent be able to enable or disable all mods at any time to compare the original draws with the mods' draws, and named F11.
- Decision: do not implement "disable" ourselves but press the key EFMI already has. `[KeyToggleMods]` flips `$enable_mods`, and the core decides from it whether IB hashes are matched. This moves or rewrites no mod file, does not reload and does not change XXMI's memory, in line with "do not modify the original tools".
- The probe only gains a second fixed action (F11) and still accepts no key chosen by the client; both actions share the same foreground, modifier key and frame synchronisation protection. Sending is refused when the binding is not the default, or when F11 pressed alone would also trigger other functions in `[Hunting]`.
- Source of the state: XXMI has no interface for reading variables, so the server reads the notification text EFMI prints in the frame after the key press. Both texts are read from the local `main.ini` instead of hard-coding the English wording; if it cannot be read, the state is reported as unknown and the tool stops instead of guessing. `on` / `off` are therefore implemented as "press once, look at the result, press once more if it is wrong", at most twice. The state remembered in the session is only the result of the last notification; a manual F11 is not seen, which the tool text says.
- Live mode is not stopped and old captures are not cleared: the switch destroys no resources, and old captures are exactly what a new frame is compared with.
- Mods that hook shaders on their own are not controlled by `$enable_mods`; this is EFMI's behaviour, stated in the tool description, with no further handling.
- Switching back on afterwards is written into the tool description, the server instructions and AGENTS.md.

## D-016: Corrections to several checks after verification in the real game

- Date: 2026-10-07; extracting and deploying Yvonne in the real game ran into a series of checks that were too strict, assumed something that does not hold, or took the wrong frame (see the first section of CURRENT_STATE).
- CPU-posed components: "what is read must be data that some draw actually used" is not loosened; instead a moment is chosen that can prove it: an additional light frame is captured and the range is read immediately, with the probe's range write records and the draw-time hash of that frame as proof, re-sampling on failure. The price is that these vertices belong to the pose of a few frames later, which matches the existing scope "the bone buffer is read back later anyway and does not reproduce the pose of one frame", and is written into the archive and the report. Not adopted: reading the whole buffer (always judged as rewritten), accepting an unproven read-back, or changing the probe for this (needs another game restart; left for later).
- Identity check: the first 64 bytes of the instance constants (the world matrix) used to serve as identity, which changes as soon as the object moves. The point of the check is that the bone buffer was not swapped during reading, so now only the bone buffer pointer is compared; meshes entering or leaving the picture are irrelevant.
- Bone merging: the merge results of the samples are no longer required to be identical item by item (noise in the last digits of the game's matrices makes them differ naturally); instead it is verified that every merge of the sample used for the export holds in all samples (matrix tolerance 1e-5). What really has to be prevented, two different bones coinciding by chance, is still rejected; merging less only produces duplicate vertex groups. The merge algorithm itself is still LoyalTools', neither copied nor changed.
- F11 `on` / `off` get a shortcut that presses nothing, which needs two conditions at once: this server has read EFMI's notification and it was the target state, and the draws of a freshly captured frame agree with it. Memory alone would miss a manual key press by the user, and draws alone are undecidable when no mod mesh is in view, so both are required.
- The frame judged after a reload: the wait counts from the game resuming to present frames, not from the key release; frames in which the mods do not draw anything yet are not judged. F10 is still pressed only once.
- Checks go by content: when a recorded hash does not match the exported file, no conclusion is drawn directly (it may be an old value); the whole buffer is read back and compared. Only different content of `vb0` (the stream the INI binds from a file) is a failure; other slots may be bindings left by the game and are only listed.
- Disabling a mod of the user's is an exception to "existing mods are read-only", done only when the user explicitly asks on the spot, by renaming, renaming back afterwards and stating it in the result; the tool itself does not offer this action.

## D-017: The region hash cache is keyed by buffer object, not by address

- Date: 2026-10-07; the user asked for the doubt noted during the real-game deployment check to be dealt with, and a sandbox reproduction confirmed a probe defect (BUG-005).
- Decision: a cache entry records the creation serial of the buffer object, which is compared with the current object on lookup. "Clear the entry on destruction" was not adopted because the probe does not hook the buffers' Release, and adding that would put another hook layer on every resource; "empty the cache on every creation" was not adopted either, since it would invalidate the cache whenever the game creates buffers frequently and push continuous capture back to a read-back every frame.
- The probe signals this with the feature `region_hash_per_object`, and the server bases its hints on that rather than on a version comparison. With an old probe the server attempts no remedy such as clearing caches on reconnect; it only explains this in the status and the reload result and recommends reading contents, and the deployment check always goes by content.
- The reproduction is an option of the sandbox program plus a self-test check, rather than depending on an XXMI reload: the test program creates the address reuse itself and confirms from addresses and serials in the capture that it really happened, which is what makes the test meaningful.
- Reloading an unchanged mod in the real game yields no counter-example (old and new content are the same); this is recorded honestly as "cannot count as verification".

## D-018: The plan document in old commits stays; history is not rewritten

- Date: 2026-10-07. The plan document once entered the repository with three commits (`5518332`, `c673447`, `919db60`), was then moved out and added to the ignore list; asked twice whether history should be rewritten to remove it completely, the user answered that it does not need to be removed completely.
- Decision: leave things as they are, with no history rewrite and no force push; from now on the plan document stays local only (`AGENTS.md`). This is settled and need not be asked again.

## D-019: Archives take only extractable objects; an LOD capture job hands the probe back midway

- Date: evening of 2026-10-07; the first LOD capture on an open-world screen failed for the whole frame because of unrelated draws.
- Scope of the archive: only the draws of objects with "at least one mesh skinned on the GPU" are kept, objects being told apart by the per-object constants (the first 64 bytes of the instance constant block). Merged-skeleton extraction and LOD matching both need bone weights, so objects without a GPU-skinned component are out of the question; LoyalTools itself uses the same constants to tell objects apart and discards such objects, so this step removes no draw it would have used. "Archive everything and skip what cannot be read" was not adopted: an open-world frame would mean reading 160 MiB more of buffers and dozens of textures, and waiting longer with mods switched off.
- Draws whose index data the game rewrites: they no longer fail the whole frame but are skipped and recorded; the object they belong to is marked incomplete in the candidate list and refused at extraction. The earlier whole-frame refusal was meant to prevent models with missing components; that guarantee now rests on the object.
- Rewritten vertex data is read back once per buffer rather than mesh by mesh: the data stays for only a few frames, and with several characters reading one by one is bound to be late for some.
- A job with `method=capture` hands the probe back as soon as the frames are on disk (`Job.probe_done`), instead of adding a "capture only" tool: matching takes tens of seconds and does not need the game, and the time with mods off should be only the few seconds of capturing; the existing `method=frame` with an archive directory can still be used for offline re-runs.
- The reload evidence of the deployment check is taken again on the frame that is finally judged: the check already skips the first frames in which no mod is drawing yet, and the evidence should come from the same frame, otherwise a reload that did happen is reported as unconfirmed.

## D-020: Pipeline state is added to the protocol without changing anything; render target copies are made by the probe before and after the draw

- Date: 2026-10-08; the user asked for items 1, 4 and 5 of the improvement list.
- The protocol number is unchanged (still 1): everything new is an added key or an added command (`read_rt`, `release_rt`, the `rt` option of a capture), each with a `features` entry. An old server reading a new probe and a new server reading an old probe both work as before, and the server says what is missing by `features` instead of comparing version numbers.
- State objects and views are recognised by content, not by address: the probe has no creation / destruction hooks for them, and although an object being destroyed and another created at the same address within a frame is unlikely, BUG-005 has already shown that the assumption "address is identity" quietly gives wrong data after some reload. The description is fetched at every binding and compared with the record; the cost is a dozen or so description fetches per draw, and measurements show no slowdown of a full capture. "Hold references while recording" was not chosen: that would change the lifetime of the game's objects.
- Fields that D3D11 does not keep are not shown (see LIMIT-006): showing the defaults the runtime fills in would make people think the game set them.
- Looking at several more frames after a reload (item 1) lives in the server and polls with light captures, instead of "wait until XXMI starts drawing" in the probe: the basis of the decision (whether objects XXMI created after the key press are used) is in the server anyway, and the probe does not need to know what a reload is.
- "What was drawn" is answered by the difference of two copies, before and after the draw, rather than a copy after the draw only: "after" alone does not show the contribution of this one draw. The comparison is on raw bytes without decoding, so the changed area can be given for any format.
- Where the copies are made: the one before in `cap_on_draw` (before the original function is called), the one after in the hook after the original function returns, triggered by a global flag, so that normally each draw costs only one more read of the flag. Each copy goes into its own staging texture and, like buffers, is mapped only when the client comes to read it; the render thread does not wait for the GPU.
- Selection is by "index buffer object + range" rather than by draw number: draw numbers change every frame, the region hash is known only after a read-back, and what is available when the draw is issued is only object identity, arguments and shader hashes.
- Passes without a colour target take no quota: a mesh is usually drawn in several shadow and depth passes first, which come earlier in the frame and would use up the default quota of 8 without anything being copied.
- Copies are released when reading is done, plus a `release_rt` command, instead of "invalid after one read": the self-test needs to read repeatedly, and releasing should be an explicit step.

## D-021: After a reload, the frame is taken when "what the mods draw has stopped growing"

- Date: 2026-10-08; in the real game a second F10 (with the shortest wait) returned a frame in which only half of the mods were back, while the standard of the time had judged it confirmed and returned.
- "Did the reload happen" and "are the mods all back" are two things: the former looks at whether objects created after the key press are used (unchanged), the latter at whether that number is still growing. The frame the tool returns is for the agent to go on checking what it changed, so the latter should decide.
- Choice of the quantity: "resources + shaders created after the key press and used by the frame", which `reload_evidence` already computes and which a light frame contains. XXMI's draw count was not used: it fluctuates every frame with the view and the shadow cascades; the creation counters of the probe status were not read either: they include creations outside the picture.
- Stop after two consecutive polls without growth, not three: frames are slow during recovery (that time in the real game the frame number advanced by only one in the 250 ms after recovery), and each capture records the next frame after the request, so two neighbouring polls see the picture after two successive loads; the cost of waiting one more round would fall on every reload. Fewer mods in view (the number going down) counts as stable too.
- The full frame is captured only once, at the end: the probe guarantees only that the latest capture can be read, so capturing the full frame first and looking at light frames afterwards would invalidate it.
- The default `settle_ms` (2000) was not changed: there is no data showing that shortening it is safe.

## D-022: Contents as they were at the call use "copy before write", not a copy at draw time

- Date: 2026-10-08; the user asked for "essentially every type of data in a frame analysis folder" to be obtainable (T-013).
- Volume of the sample directory: one frame has, after de-duplication, 38 buffers with 315 MiB and 1.5 GiB of DDS, plus the render targets after each draw: several GiB for the whole frame. XXMI copies synchronously after every call and writes to disk, which takes tens of seconds per frame. Doing this live in the game cannot simply copy that approach.
- Chosen: while recording, only versions are registered, and old content is set aside only when something is about to rewrite a resource. Reason: the vast majority of resources do not change within a frame (meshes, textures), so copying each at draw time is wasted; resources that do change (dynamic buffers, UAVs, render targets) are copied once per version that was used, which is the lower bound. For buffers only the overwritten bytes are stored, avoiding "a 16 MiB mesh heap copied whole every time a mesh is appended".
- Cost: correctness depends on "every write path goes through a hook". All write entry points of the D3D11 immediate context are hooked; what cannot be seen (command lists, video, sharing across devices) is flagged in the result or written into the limits. In return the feature can be switched on for the inputs of all calls of a whole frame with almost no extra cost in that frame.
- `NO_OVERWRITE` does not start a new version: such a Map hands memory straight to the application, and a GPU copy queued before it would also read the newly written bytes unless the GPU is waited for first, and a synchronisation at every append is too expensive. The bytes the call reads are still exact (D3D's contract guarantees it), so the contract is trusted by default, with `strict` for byte-for-byte comparisons.
- Acceptance uses XXMI itself: the sandbox scene has identical bytes in every frame, the probe captures one frame, XXMI dumps another, and the files are compared byte for byte. That adds an independent witness compared with checking only against the bytes we expect ourselves.
- "Exporting exactly the same folder as XXMI" was not made a goal in itself: the call numbers (XXMI counts the game's calls, the probe counts the calls that reach D3D11) and its internal log cannot agree; what the exporter achieves is the same layout and file formats, enough for the existing reader and for tools that work on frame analysis directories.

## D-023: Query texture slots from shader references in memory

- Date: 2026-10-16; the user asked to avoid repeated helper-script/file creation for slot lookup.
- Provide a read-only batch MCP tool that joins a requested capture's bindings with static SM4/5.0 SRV instruction references. Return inline evidence and grouped material/target bindings, preserving draw numbers and mesh ranges. Declarations and leftover bindings do not prove use; conditional references do not prove per-pixel execution. Do not guess diffuse/normal roles from fixed slots or treat failed analysis as an empty usage set.
- Cache immutable shader bytecode/listings/parsed references by hash for one probe connection, with LRU limits; clear on a connection change. Do not cache material bindings across captures. Full shader paging uses the same cache and writes files only on explicit export. This avoids repeated probe requests, disassembly, disk exports and agent scripts without changing the probe or requiring a game restart.
- Validate both the no-file contract and data correctness in unit/real-sandbox tests, then compare live slot metadata against the existing draw query. Report cold/warm measurements as such, without presenting them as a benchmark of an old agent workflow.

## D-024: The probe collects slot data per game version by itself, keyed without texture hashes

- Date: 2026-10-18.
- Context: slot-style bindings move with game updates; the user wants a general per-version repair and has ruled out texture hashes. XXMI keeps no copies of unpatched shaders from before an update, and a resource update can move slots while the client files stay the same.
- Decision: the probe looks at one frame in 30 and appends one record per new skinned draw (IB region hash and range, shaders, slot shapes, output formats) to a folder named after the client version plus a fingerprint of the resource indexes and client files, and saves the shaders' bytecode. Records are de-duplicated by a key without texture hashes; texture hashes are kept only as information. Only draws with `BLENDINDICES` by default, because XXMI's overlay would otherwise add lines all the time. All file work happens on a writer thread; the render thread only queries bindings, and walks the stack only for new draws.
- Consequence: the cache fills while the game is played, also across sessions; comparing versions, deriving slot migrations and rewriting INIs is server work (T-016 steps 2 and 3). With mods on, modded characters are recorded as XXMI's draws; their native draws need F11 off.
- Revised the same day (probe 0.1.8), after the first run in the game: the key also leaves out where a mesh lies (first index, base vertex, which buffer) and all sizes, because the game moves meshes within shared buffers and screen-sized textures follow the dynamic resolution. A mesh is its index data (region hash and count).

## D-025: File-free version slot tables with explicit uncertainty

- Date: 2026-10-18. Complete the existing slot-table draft as a read-only MCP query and explicit CLI export. Cache one version per server tool instance; rebuild when records/version/shader metadata changes, refresh explicitly for installed-mod changes.
- Texture hashes never determine identity, role labels or migrations. The table labels material/global candidates by formats and suggests roles from mod resource names or formats/order, always as hypotheses. Unknown shader analysis and conflicting evidence withhold roles. Static references include conditional paths, not proof of pixel execution. Frame groups do not establish character identity across sessions.
- Single-version slot tables do not complete shader-pair migration or authorize writing installed mod files. Those remain separate work.

## D-026: Separate primary-material candidates from auxiliary passes (withdrawn by D-027)

- Date: 2026-10-18. Historical agent error: a tentative user impression was misread as a constraint. The withdrawn implementation used 10 < diffuse < lightmap < normal as a primary-candidate constraint, not a universal D3D11 or ZZMI law. A low-slot auxiliary pass is never the primary fallback. Incomplete or competing mappings remain unknown, with raw bindings retained. Format-only inference conservatively requires one consecutive triple (BC5 strengthens normal evidence); explicit mod mappings can be nonconsecutive. Contradictory roles are rejected, not relabeled.

## D-027: Independent shader evidence, primary-only material output (supersedes D-026)

- Date: 2026-10-18. User explicitly clarified their >10/order statement was an impression; turning it into a rule was the agent's error. Withdraw numeric/order/adjacency gates, format role assignments, cross-shader format voting and mod-guided inference.
- Analyze each IB/count and observed shader/output pass before reading mod declarations. Preserve component-level sampling/consumption evidence and conditional alternatives. Infer only labeled candidates from local arithmetic; do not claim a complete decompiler or runtime execution. Primary identification needs surface-material and lighting evidence, not the most slots or agreement with a mod. Ambiguous/incomplete semantics remain explicit.
- Mod names/bindings are a separate post-analysis cross-check; native digest must be invariant under changed or absent mods. Main material tables expose primary slots only; all other passes stay in a separate audit artifact/opt-in query. Texture hashes are informational only.

## D-028: Coverage is independent of role recognition; primary mappings are per pipeline

- Date: 2026-10-18. User challenged the sparse table after browsing almost every character. The previous all-or-nothing filter hid captured IBs. Preserve every observed native IB/count in material outputs with explicit reasons when primary identity or roles remain unresolved; keep auxiliary pass details in the separate audit. Labelled entries are visible, unlabelled entries without primary evidence are in an expandable inventory, and all keys remain queryable/JSON-exported.
- A primary candidate can have several observed VS/PS/output variants with different slot mappings. Retain each mapping instead of rejecting the whole IB or selecting by slot number/mod agreement. Resolve roles independently within each variant. A shared role requires the same independently resolved slot in every variant; missing/ambiguous evidence cannot be filled from another variant. Multi-mapping INI comparisons require shader context.
- Coverage counts observed IB/count keys, candidate primary variants and unresolved reasons separately. Mod labels and unseen INI entries are attached only after native analysis; they cannot establish a full character roster. Schema 3 exposes these distinctions. This corrects the aggregation/display policy in D-027, without weakening independent evidence requirements or claiming more shader semantics were solved.
