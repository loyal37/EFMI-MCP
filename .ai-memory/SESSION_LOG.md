# EFMI MCP — 开发日志

> 每轮结束追加简短摘要，记录实际完成、验证范围、遗留问题和下次入口；不粘贴聊天或完整日志。

## 2026-10-06：用户启动游戏后的自动 F10 试用

功能提交 `ab6f4a5` 已推送至loyal37/EFMI-MCP/main。用户本轮启动游戏并要求试刷新；动态工具搜索接口返回internal error，直接启动生产MCP服务0.1.2并通过真实stdio调用工具，连接唯一探针Endfield PID41976（0.1.1）。首次一次F10按下/释放获API接受，聚焦准确窗口，无Numpad0；2328ms完成等待及抓帧，原帧57703→barrier57971→57972，共323次调用（203 game/120 xxmi）、188资源、176shader和9,523,456字节CB快照。IB/VB0读回正常，旧缓存失效，MCP子进程退出0。证据 `captures/verify-reload-41976/result.json`；这只证明API接受和后续采集。

用户随后移出Yvonne模组并授权再试。目录扫描7→6个mod；一次focus_refused在发键前拒绝，用户手动前台后第二次普通VK请求API接受并得到帧84113，证据 `captures/verify-reload-41976-mod-removed-foreground/result.json`。用户明确确认模组仍显示、没有刷新成功，截图与旧网格绘制吻合；XXMI调用139→120是同一网格额外pass变化，不是卸载证据。当前实机自动刷新失败，不能再写“待用户确认”或“实机尚未验证”。

扫描码诊断保存在 `captures/verify-reload-scancode-41976-ready/result.json`：down/up的SendInput返回1、错误0，前台始终PID41976且无修饰键，但整个按住期F10异步高位均false；帧160361→160664的XXMI DrawIndexed IB及次数相同。`assessment.json` 明确实际刷新失败，raw报告pass仅表示诊断调用/采集完成。扫描码不是修复，原因尚未知；Python为medium、读取游戏token access denied不能推出目标权限或安全软件拦截结论。

据此服务升0.1.3，增加按住期间至少一次GetAsyncKeyState高位的门槛，区分 `key_down_sent` 与 `key_down_observed`。未观察到按下返回input阶段 `keydown_not_observed`，finally释放、不capture、不重发；`reload_confirmed=false`不变。官方v1.2.2输入确实使用该高位，user.ini仅dirty时保存，因此本轮mtime不变不作为失败证据。未发现受支持外部reload接口；Core/auto_update.xcmd属于启动器更新/启动脚本，不能作运行时替代。

本轮输入21项+工作流32项、MCP stdio私有沙盒39项及compileall通过；实际F10私有沙盒因用户游戏运行未重跑，旧沙盒结果仅作历史证据。此轮完成的是失败检测，真实输入问题仍未解决，T-007目标保持未完成。未改EFMI/启动器/Core/模组或探针，未自动提权、未再发送输入，游戏仍运行。同步记录后按既有授权提交推送0.1.3；提交及推送以实际git结果为准。

## 2026-10-06：自动 F10 后采集与推送约定

用户要求 MCP 自动完成修改 mod 后的刷新与重新采集。服务升至 0.1.2，新增 `reload_input.py` 固定 F10/前台/PID/修饰键输入模块、`reload_workflow.py` 同一管道与渲染目标的重载后捕获流程；探针仍为 0.1.1，只需重启 MCP 服务，无需重注入或狩猎模式。读取原始双 F10 绑定，不直接编辑 EFMI 文件；正常 F10 可能保存持久变量，文档明确保留此副作用。

工作流停止 live、release 并失效旧捕获/mod 索引，单次 F10 后默认等 2 秒，再接受严格晚于 barrier 的非空帧；不重连换进程、不自动重发 F10、live 不自动恢复。`reload_requested` 与恒 false 的 `reload_confirmed` 分开，后者没有 XXMI 回执。专项发现未知输入异常曾被误报为未发送，修正为 null；检查 live 的 recording 时先停止 live，不能误判为另一个未完成单帧请求。

最终输入 19 项与工作流/MCP 边界 30 项、既有 MCP 39 项、私有沙盒实际 F10 集成均通过。前两组用 fake 输入和探针，纳入 `run_all.py`，无系统输入；实际集成单独运行 `reload_integration_test.py`，会真实聚焦/F10，发现其他探针则拒绝。

集成证据 `sandbox/build/reload-xuxnsq73/reload-evidence.json`：`hunting=0` 且未按 Numpad0，私有新 INI 的 VS `checktextureoverride=ib` 加 IB `hash=64e97b80 / match_index_count=864 / handling=skip` 经实际 F10 生效。目标 IB 次数 1→0，其余 `3c980a7d:2`、`3970063e:1` 完全保留；工具帧 5→barrier 209→新帧 210（10 draws，6 game/4 xxmi）。旧缓存清除、新 mod 索引、live 停止及保留 IB 读回通过，沙盒进程退出 0。此为私有沙盒验证，未对用户游戏执行 F10，不声称真实角色重载通过。

初次集成暴露沙盒默认 `SC_KEYMENU` 进入 modal loop，修正测试窗口处理；随后 IB 规则因遗漏 `match_index_count` 未命中，补齐后通过。两者没有证明产品依赖狩猎模式。另观察到 XXMI 1.2.2 在单次约 200ms F10 按住期间重建按键注册，可内部多次 reload；工具保证单次 down/up 脉冲、不自动重发，不能保证 XXMI 内部只重载一次。

用户本轮另明确要求完成后及后续轮次推送 `https://github.com/loyal37/EFMI-MCP`，已更新 AGENTS 和 D-007。旧日志“不 push”保留当时上下文；当前按验证、记忆、差异审查、提交、推送顺序完成，不把截图、捕获或忽略的构建产物加入源码提交。

## 2026-10-06：优化真实数据读取耗时

用户反馈采集慢。先对PID29988完整归档剖析，再优化游戏外CRC32C、PNG压缩/共享解码、JSON编码、身份复核无用VB哈希和离线桥接重复读缓冲。新增独立 `server/native/build.bat` 构建CRC helper，不改探针；缺失/CPU不支持/自测不通过自动回Python，保留增量种子及各类buffer语义。MCP测试在其子进程屏蔽启动测试前已有probe，可在用户游戏运行时安全验证。

实机三轮交错旧/新采集：中位1.843→1.220秒（减少33.8%），各轮完整buffer 41,967,616字节、24DDS/45,616,608字节及86几何CRC全匹配；新帧313285/313640/313988合并导出仍通过三采样gate。单独16MiB CRC为1.754秒→1.897毫秒。PNG像素不变、样本体积增大52%–75%。离线桥接三轮中位1.890→1.662秒，BUF读取1134→9，928个输出文件比对通过（只忽略输出路径/日志秒数）；6拒绝门槛、6 NumPy读语义和缓存释放检查通过，整buffer缓存峰值约80MiB且返回时清空。边界测试纠正了NumPy offset超尾部的异常语义，没有删掉错误检查来通过。

相关回归selftest83/MCP39、CRC8组/图像6组均通过，compileall/diff检查通过，独立审查无阻断项。证据 `captures/performance/` 和 `captures/bridge-performance-1fac750a2f/results.json`；这些耗时不含Blender导入，也不代表GPU渲染帧成本。所有改动只在本项目，探针DLL SHA仍为b0fee912...8fa8a，游戏PID29988保持运行，EFMI/LoyalTools/E盘成品未修改。同步记忆后本地提交，不自动push。新Python采集直接生效，旧MCP服务需重启以选择CRC backend，游戏无需重新注入。

## 2026-10-05：建立记忆，完成 `0.1.1` 工具修复与本地验证

**用户目标**：参考 `E:\loyal tools\.ai-memory` 为当前工具建立项目记忆，然后修复已注入但读不到当前角色数据的问题；只改本工具，不动 EFMI。

**实际完成**：

1. 只读参考七文件结构，核对本项目 README、探针/MCP 源码与日志，创建七份记忆文档及根 `AGENTS.md`。
2. 修复后建辅助设备覆盖 `g_dev/g_ctx` 而 `g_swap` 保留的问题：设备挂钩与目标选择分离，首个发现的交换链绑定所属设备/上下文并持有引用；逐上下文保存能力，GPU writer 限定所选上下文和交换链。
3. 探针/服务升至 `0.1.1`；状态显示目标指针/draw 计数，空捕获警告；live 增加 generation，阻止停止/重启/断开后的旧帧回填。

**失败到修复的证据**：旧游戏 PID `39000` 注入可连接，但第 `22964` 帧 draw / 资源 / 着色器均 `0`，live 空。旧版多设备后建场景 `6` 通过 / `7` 失败，仅录辅助三角形。首次自测 `71` 通过 / `2` 失败，定位 live 在途帧回填；generation 修复并新增 `10` 检查后自测全部通过。

**测试场景修正**：posed VB 偶发 `39` 个 float 变化 `1 ULP`（最大 `5.96e-8`），旧探针 + 无 XXMI 也复现，每次 hash 与读回 CRC 一致，不是本轮探针回归。测试 shader 加 HLSL `precise`，保留严格断言；有/无 XXMI 各 `12` 次连续首帧捕获的 `11616` 字节均一致，CRC `1b4be723`。具体优化机理仍属推断。

**最终验证**：`sandbox/build/verification-0.1.1.log` 退出码 `0`，八阶段全部 `PASS`。三种加载顺序各 `69` 项 hash 对照 `0` mismatch，二次捕获 IB/VB 哈希一致均为 `True`；自测 `83`、多设备无/有 XXMI 各 `96`、MCP `39` 全部通过。`git diff --check`、Python `compileall` 通过。

**交付与保护**：`dist/efmi_probe.dll` 为 `355840` 字节，SHA256 `b0fee912263763c9db28adc21798505845c9c803606bbf3969227a2bc948fa8a`；旧版备份 `dist/backup-before-multidevice/`。只改本项目；EFMI 原目录 `56` 文件最终 SHA256 `changed=[]`。未修改启动器配置、未结束用户进程，结束时游戏未运行。

**遗留边界**：新版真实游戏角色采集尚未验证。多交换链自动识别主画面不支持；跨设备替代交换链拒绝并提示重启；同设备交换链重建期间跨界捕获完整性未验证。

**下次继续**：先读 `CURRENT_STATE.md` / `TASKS.md`。T-001 本地工具修复和 T-002 记忆建档完成；用户按原方式启动游戏后执行 T-003，确认实际加载 `0.1.1`，再采集完整帧、live 和角色相关资源。

## 2026-10-05：真实游戏伊冯采集验证与提交约定

用户重新注入，PID `38252` 确认运行 `0.1.1`。帧 `9113` 为 `285` draw / `170` 资源 / `184` 着色器；15 个连续摘要均非空。探针 backbuffer 截图确认当前为伊冯，30 draw、6 IB 与 Yvonne INI 的 hash/index_count 匹配。

进一步捕获 frame `14377`（286 draw），读取三组网格完整 IB/VB0/1/2 共12个缓冲，CRC32C全部与捕获哈希相符；保留13,979,968字节draw时刻CB快照，读取5张纹理与VS/PS字节码及反汇编。证据位于 `captures/verify-game-38252/`。采集后停止 live、释放保持资源，未改 EFMI/游戏/启动器；未声称完成网格或骨架重建。

用户明确要求以后每次改完提交代码，已写入根 `AGENTS.md` 和 D-007。本轮将上一轮已验证的代码修复与记忆一并做本地 Git 提交（提交号由 `git log` 查阅）；捕获文件和构建产物保持忽略，不自动 push。

## 2026-10-05/06：Mod实机验证与后续模型提取

伊冯Mod验证完成：93次替换绘制、18参数组合、72区域CRC一致；16完整缓冲29,824,876字节及2K纹理4,194,304字节与Mod源文件逐字节相同，截图和12帧live均已保存。证据在captures/verify-mod-38252，结束已停止live/release，EFMI只读。

随后用户要求参考LoyalTools合并骨骼方式提取当前屏幕角色到E:\Endfield mods\liimo merge。2026-10-06新进程29988的56883帧确认当前为梨诺，输出目录为空。先提交此前已验证的修复和记忆，再实现当前提取任务；用户明确授权该输出路径，不修改LoyalTools/EFMI。

## 2026-10-06：梨诺合并骨骼工作空间交付

此前修复已提交6278b07。新增capture_merged_source、loyal_bridge、export CLI和Blender检查脚本，引用LoyalTools原代码不改其源码。捕获骨池offset动态，首次严格同帧尝试未通过；改按用户静态编辑目标用三份独立数据验证，未降低成“非零即通过”。147189/147435/147679三帧完整几何3,769,210字节一致，canonical来源一致；682使用骨矩阵在另两份均变化，170个跨组件带权骨/34,736通道稳定合并。

交付E:\Endfield mods\liimo merge：workplace、liimo merge.blend、验证JSON、preview.png与提取说明。18组件/79044顶点/94051三角、696私有组槽到512canonical；72个输出IB/VB字节核对通过。Blender4.5.7+LoyalTools1.6.7实导入errors=[]；首轮normal材质选项默认关闭导致验证失败，脚本在该临时场景显式启用normal map后通过，并选择漫反射节点作为Solid纹理显示。源几何和用户原目录不受这些验证修复影响。

预览核对完成，火箭靴等特效未重建游戏shader，2个小组件无自动贴图，均保留数据并写明边界。不含未捕获LOD、不声明某帧pose或命名层级rig。最终同步记忆并本地提交，不push。

## 2026-10-06 晚：定位 F10 失效原因，改由探针按键（Claude 会话）

用户反馈 Codex 实现的自动 F10 实机无效。查得 `Endfield.exe` 清单要求管理员权限，MCP 服务为普通权限，UIPI 丢弃其按键而 `SendInput` 照常返回成功。实现探针 0.1.2 的 `reload_key`（进程内固定 F10）与服务 0.1.4 的重载证据，详见 CURRENT_STATE 首节、D-012。

沙盒首轮按键送达但 XXMI 未重载：`SendInput` 被系统键盘钩子拖住数毫秒，期间的 Present 被计作按住帧。改为从读到按下状态起计帧后通过；再把松开条件定为“第二个 Present 或晚于 10ms 的第一个 Present”，最终一次按键恰好一次重载。游戏占前台时沙盒取不到前台，按键类测试等用户切出游戏后才跑完。

最终 DLL（SHA `a1ba4a93…2668`）全套通过并装到 `dist/efmi_probe.dll`，旧 0.1.1 存 `dist/backup-0.1.1/`。游戏 PID41976 仍在运行且仍是 0.1.1；未改 EFMI/启动器，未对用户游戏发键。

用户随后重启游戏、移出伊冯 mod 并要求刷新。实机一次通过：PID 43816 报告探针 0.1.2、进程 elevated（完整性 0x3000）。用户移出伊冯 mod 后由工具重载：按键在进程内发出并被读到，持续 1 帧（21696→21697），重载那一帧耗时 92.7ms（平时约 8ms）；重载前 367 次绘制中 XXMI 120 次（93 DrawIndexed + 27 Dispatch），重载后 277 次全部来自游戏，XXMI 的 93 次/18 个网格消失，游戏原始的 30 次/6 个网格出现；`reload_confirmed=true`（XXMI 在按键后创建了新帧所用的 27 个着色器）。游戏继续运行。证据 `captures/verify-reload-inprocess-43816/`。当时游戏本来就在前台，探针从后台取前台（`attach_input`）并还回焦点这一段在实机上尚未执行过，只在沙盒验证。

用户确认本轮及以后的 Claude 会话同样按 D-007 提交并推送。

**下次继续**：有机会时在游戏处于后台时再验证一次取前台/还焦点。
