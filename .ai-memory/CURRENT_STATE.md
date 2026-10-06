# EFMI MCP — 当前开发状态

> 最后更新：2026-10-06 晚。自动重载由探针在游戏进程内按 F10（探针 0.1.2 / 服务 0.1.4），**实机验证通过**（PID43816，游戏在前台和在后台各一次）。已运行的 MCP 服务需重启才加载 0.1.4。

## 自动重载改由探针按键（T-007，2026-10-06 晚）

- 根因：`Endfield.exe` 内嵌清单为 `requireAdministrator`（XXMI Launcher 相同），MCP 服务是 medium 完整性。UIPI 丢弃低完整性进程发往高完整性窗口的输入，`SendInput` 仍返回成功、无错误码，与 0.1.3 实机现象一致。旁证：WMI 读不到游戏命令行/路径而同会话普通进程可读；沙盒探针（medium）在游戏占前台时取前台被拒（`focus_refused`，未发键）。没有发现反作弊过滤输入的证据，但新路径尚未在实机跑过。
- 探针 0.1.2 新增管道命令 `reload_key`（`probe/src/reload.cpp`）：固定 F10，只在本进程窗口处于前台时发送；独立核对 d3dx.ini 两项绑定、修饰键/F10 未按住、窗口未最小化；取前台先 `direct` 后 `attach_input`；先等 3 帧；**从读到按下状态起计帧**；在按下后的第二个 Present、或晚于按下 10ms 才到的第一个 Present 处松开；上限 3 秒；可把焦点还给原窗口。不修改 XXMI/游戏代码。
- 状态新增 `features`、`process.elevated/integrity_rid`、XXMI 资源/着色器创建计数；捕获的资源和着色器带创建帧 `cf`，资源带序号 `sn`。
- 服务 0.1.4：状态含 `reload_key` 时走 `send_reload_via_probe`，否则走旧的外部路径并在失败时提示重启游戏；新增参数 `restore_focus`。`reload_evidence` 统计新帧所用对象中 XXMI 在按键帧之后创建的资源/着色器（排除 tex1d 的 IniParams）；有这样的对象且按键前 3 帧内 XXMI 没有自行创建资源时 `reload_confirmed=true`。EFMI Core 自身的资源和着色器重载后也会重建，沙盒里没有用户 mod 时同样有证据。
- 调试中修复：`SendInput` 要等系统键盘钩子处理完才返回，期间的 Present 曾被算作按住帧，按键实际只按下一瞬，XXMI 没有轮询到（`sandbox/build/reload-ff6nv69x`，XXMI 日志只有启动时一次 Reloading）。沙盒程序新增渲染线程视角的 F10/前台变化输出，用于定位这类问题。
- 沙盒验证（最终 DLL，SHA256 `a1ba4a93a63bfc416e54ec28d46935dc3f40b72ccce1876b648c128b9d702668`，372736 字节；XXMI 1.2.2 私有副本）：三种加载顺序哈希对照、自测 83、多设备 96×2、MCP 39、输入 27、工作流 46、真实 F10 集成全部通过。集成证据 `sandbox/build/reload-hmxtmkw_/reload-evidence.json`：按键持续 1 帧（59→60），XXMI 恰好重载一次，skip 规则生效，证据为 6 个资源/4 个着色器，焦点已还回。沙盒不是管理员进程，UIPI 只能实机验证。
- 交付：新 DLL 已在 `dist/efmi_probe.dll`；运行中游戏占用的 0.1.1 改名保存到 `dist/backup-0.1.1/`（SHA `b0fee912…fa8a`）。未结束游戏，未改 EFMI/启动器。
- 实机验证（2026-10-06 20:41，用户重启游戏并要求刷新）：PID 43816 报告探针 0.1.2、进程 elevated（完整性 0x3000）。用户移出伊冯 mod 后由工具重载：按键在进程内发出并被读到，持续 1 帧（21696→21697），重载那一帧耗时 92.7ms（平时约 8ms）；重载前 367 次绘制中 XXMI 120 次（93 DrawIndexed + 27 Dispatch），重载后 277 次全部来自游戏，XXMI 的 93 次/18 个网格消失，游戏原始的 30 次/6 个网格出现；`reload_confirmed=true`（XXMI 在按键后创建了新帧所用的 27 个着色器）。游戏继续运行。证据 `captures/verify-reload-inprocess-43816/`。
- 同日 20:51 第二次实机，游戏在后台（前台是 Claude 窗口）：探针以 `direct` 方式取得前台，按键持续 1 帧（91844→91845，重载帧 79.8ms），随后把前台还给原窗口（前后为同一 hwnd）；移出的伊冯 mod 同样消失（XXMI 120→0 次绘制，游戏原始 30 次绘制出现），`reload_confirmed=true`。`attach_input` 这条备用取前台路径在实机上没有用到，只在沙盒验证。目录内 `background-result.json` / `background-comparison.json` 是第二次的结果；第一次的 JSON 被第二次运行覆盖，保留了它的控制台输出 `foreground-run-console.txt`。
- 未覆盖：反作弊在这两次按键后没有可见反应，但样本只有两次。已运行的 MCP 服务仍是旧代码，本次用脚本直接调用磁盘上的 0.1.4 工作流；新会话启动的服务才是 0.1.4。前台是管理员窗口时沙盒的按键类测试（哈希对照、F10 集成）无法运行。本轮改动经用户确认后按 D-007 提交并推送，提交号以 git log 为准。

## 0.1.3 实机失败记录（T-007，2026-10-06，已被上节取代）

- 实机结论：用户移出 Yvonne 模组后，普通 VK F10 请求未使其卸载；用户明确确认“没有刷新成功”，截图仍为 Yvonne Ensnared Heart。当前自动输入路径不可用，尚未定位原因。读取新帧成功不等于刷新成功；T-007 实现已交付，但真实游戏自动刷新目标仍未完成。
- 两次普通 VK 输入：第一次生产 stdio 0.1.2 连接 Endfield PID41976/probe0.1.1，F10 down/up API 均接受，2328ms 后取得帧57972（原帧57703、barrier57971），323调用/120 xxmi、188资源、176shader、9,523,456字节CB；IB `5ede2273`、VB0 `61616f88`读回正常。证据 `captures/verify-reload-41976/result.json`。用户移出模组后再次授权测试，目录扫描7→6个mod；一次 `focus_refused` 未发送按键，随后手动前台下的完整请求取得帧84113，但模组仍显示，证据 `captures/verify-reload-41976-mod-removed-foreground/result.json`。XXMI调用139→120仅为同一网格额外pass差异，不能视为卸载。
- 扫描码诊断也未刷新：`captures/verify-reload-scancode-41976-ready/result.json` 记录 down/up 返回1、错误码0，目标前台PID41976且无修饰键，但按住期间所有 `GetAsyncKeyState(VK_F10)` 高位均为false；帧160361→160664，XXMI DrawIndexed 的IB及次数相同。`assessment.json` 明确 `actual_reload_succeeded=false`；raw报告的pass只表示诊断调用/采集读回完成。扫描码不作为修复，未再重复发送输入。
- `d3dx_user.ini` mtime/SHA不变不是失败判据：官方XXMI v1.2.2的 `SavePersistentSettings(false)` 在未dirty时不写入。失败依据是用户确认及模组绘制保留。当前Python为medium，读取游戏token被拒绝，不能据此推断游戏确切权限级别或归因安全软件。未修改EFMI/Core/启动器/模组，游戏仍运行。
- 服务0.1.3在F10约200ms按住期间要求至少一次观察到异步按键高位；`key_down_sent`保留API接受语义，`key_down_observed`独立记录状态。接受事件但未观察到按下则返回input阶段 `ToolError` / `keydown_not_observed`，finally尝试keyup，不开始后续capture、不重发。此改动修复错误检测，未修复实机输入阻塞；观察到按键也不是XXMI重载回执，`reload_confirmed=false`。

- 用户要求 agent 自动完成修改 mod 后的 F10/等待/重新采集。新增 `efmi_reload_and_capture`，默认 `detail=full`、`cb_data=true`、`settle_ms=2000`、`timeout_ms=15000`、`focus_game=true`；捕获超时不包含等待时间，无需狩猎模式。
- 预检连接管道 PID、XXMI 路径及所选 device/context/swap chain，读取当前安装 `[Hunting]` 的 `reload_config`、`reload_fixes`，必须各为单条 `no_modifiers VK_F10`。只对该进程的有效前台窗口发送一次 F10，拒绝修饰键已按住或焦点失败；无任意按键、自动提升权限、重连换进程或重发 F10。
- 开始执行后停止 live、release 并清空会话旧捕获和 mod 索引；等待后记录帧号 barrier，再接受同一进程和渲染目标下比原帧/barrier 都新的非空捕获，保存为 latest。live 保持停止。预检失败保留旧缓存，执行开始后的失败不再发布旧捕获。
- `reload_requested=true` 仅指键盘输入已发送，`reload_confirmed=false` 明确无 XXMI 重载回执；仍须验证目标 draw/资源。输入异常分别报告未发送、已发送或未知，后续捕获失败不能自动再次 F10。工具不直接写 mod/Core/启动器，正常 F10 仍可让 XXMI 保存持久变量配置。
- 本轮0.1.3验证：输入21项、工作流/MCP边界32项（合计53项）、既有MCP stdio私有沙盒39项及compileall均通过；专项使用fake输入/探针。真实F10沙盒未重跑，用户游戏仍运行且脚本会拒绝其他探针。不得把本次错误检测通过记为游戏刷新修复。
- 历史0.1.2私有XXMI沙盒集成通过，证据 `sandbox/build/reload-xuxnsq73/reload-evidence.json`，进程40584退出0。`hunting=0`，未按Numpad0；私有skip规则使目标IB `64e97b80` 从1次变0，其余IB次数保留，帧5→barrier209→210，缓存、live、mod索引及IB读回符合预期。此为历史沙盒证据，不能覆盖本轮实机失败。
- 集成过程修正沙盒自身默认 `SC_KEYMENU` 的 modal loop，并给测试规则补齐 `match_index_count` 才命中 IB；不构成产品依赖狩猎模式的证据。一次 F10 down/up、约 200ms 按住期间，XXMI 1.2.2 重建按键注册可能发生多次内部重载；工具只承诺一次输入脉冲且不重发，不能承诺 XXMI 只重载一次。
- 服务版本0.1.3，探针源码/DLL保持0.1.1；重启MCP服务加载检测更新，无需重注入。后续继续定位正常输入路径，不自动提权或改EFMI。官方输入采用 `GetAsyncKeyState`，未找到受支持的外部reload接口；源码证据与边界见KNOWN_ISSUES / D-011。按既有用户要求提交并推送指定仓库，结果以实际git执行为准。

## 读取性能优化（2026-10-06）

- 用户反馈实际读取慢，本轮只修改本项目。先剖析真实 PID29988：带 profiler 的一次完整归档2.248秒，capture/readback管道等待约0.89秒、截图PNG约0.54秒、缩进JSON约0.31秒、Python CRC约0.27秒。此数含profiler，不与后续无profiler结果直接混算。
- 无profiler、同游戏交错前/后各三轮：中位1.84294→1.22016秒，减少33.8%。每轮仍有41,967,616字节完整buffer、24张DDS/45,616,608字节、两次完整绑定/CB捕获和全分辨率截图；86项region CRC全匹配。新帧313285/313640/313988已通过三采样合并骨骼导出，保留完整几何/骨来源验证。
- 新增游戏外 `dist/efmi_crc32c.dll`，由 `server/native/build.bat` 单独构建、Python用绝对路径加载；SSE4.2检测和自测失败/缺失时回原算法。16MiB真实buffer中位1.754秒→1.897毫秒，86真实区域与原算法/探针hash均一致。任意旧seed语义和各类memoryview保持；8组回归及新进程缺库回退通过。仅重启已运行的MCP服务才加载新backend，新的采集脚本直接生效。
- PNG压缩级别1、MCP完整图/预览只解码一次；像素/尺寸不变，样本PNG约增大52%–75%，DDS不变。6组解码回归通过。大型归档JSON紧凑编码；第二次身份复核保留IB/CB、关闭不用的VB hash；主帧完整CRC/三采样gate保留。归档增加 `durations_ms`。
- LoyalTools桥接复用单次导出内已验证SHA的buffer和构建结果；三采样源BUF读取1134→9次，三轮交错导出中位1.890→1.662秒（减少12.1%）。4对共928个输出文件一致（仅排除输出目录/日志计时）；6项拒绝门槛及6项NumPy读语义检查通过。完整buffer缓存峰值约80MiB、返回时清空，GC后3个extractor均释放。缓存仅用于离线归档，绝不拿旧帧数据充当新的实时读取。
- 相关沙盒回归：selftest83、MCP39全部通过；MCP测试子进程现在屏蔽测试启动前已有probe，用户游戏运行时可测。未改探针源码/DLL，其SHA256仍为 `b0fee912263763c9db28adc21798505845c9c803606bbf3969227a2bc948fa8a`，未结束游戏。
- 证据：`captures/performance/ab-timings.json`、`crc32c-results.json`、`fresh-validated-workplace/live-bridge-report.json` 和沙盒日志；桥接最终前后对比见 `captures/bridge-performance-1fac750a2f/results.json`。原采样、游戏/EFMI/LoyalTools及E盘已交付成品未修改。
- 已有提取功能提交为 `8faa4c2`；本轮性能代码与记忆一起本地提交（以git log为准），不推远端。上述时间不含Blender导入，不保证其他场景固定耗时；下一项主要成本仍是逐个纹理的GPU等待/管道传输，本轮没有改GPU队列或协议。

## Mod 绘制验证（2026-10-05，已完成）

用户开启伊冯 Mod 后，帧34714/42531均记录375次调用：254 game、121 xxmi（94 DrawIndexed、27 Dispatch）。其中93次绘制精确对应Yvonne的四组替换网格、18个唯一绘制参数组合；72个唯一IB/VB区域CRC与源文件切片一致。16个完整缓冲共29,824,876字节和2K漫反射4,194,304字节，均与Mod源文件逐字节一致。PS t70/t71/t72可见三个8K材质绑定，截图确认替换服饰。12个live摘要已保存，结束已停止live并release。证据在 `captures/verify-mod-38252/`，包括 `analysis.json`、`readback.json`、`offline-readback-comparison.json` 和截图；未修改EFMI。绘制分支只能推断实际执行的开关条件，不能读取所有未执行分支变量。

## 梨诺合并骨骼提取（2026-10-06，已完成）

- 用户指定参考 `E:\loyal tools`，输出至 `E:\Endfield mods\liimo merge`。截图确认当前梨诺，进程29988运行探针0.1.1；参考工具/EFMI保持只读。
- 代码新增实时归档、LoyalTools原生模型桥接、Blender导入验证入口。未改探针，不需重新注入。此前多设备修复已本地提交 `6278b07`；本轮提取代码另行提交。
- 最终采样 `captures/liimo-static-a/b/c`：帧147189/147435/147679。实际完整几何3,769,210字节相同；底层几何资源均由探针确认未改变，文件SHA一致。仅86个XXMI区域hash不作为全几何验证替代。
- 原算法选Character79044：18组件、79,044顶点、94,051三角、696私有组槽→512canonical来源。三次canonical(组件unique_str,localVG)完全一致；682个实际使用的骨矩阵均有效且在另两采样变化，170个带权local骨跨组件合并，涉及34,736带权通道。
- 输出 `E:\Endfield mods\liimo merge\workplace`：18个IB+54个VB共72文件与原始源范围逐字节相同；121 DDS包含组件独立副本/Map别名。原布局冲突警告保留在live-bridge.log。
- `liimo merge.blend` 已用Blender4.5.7 LTS+已安装LoyalTools1.6.7实际导入保存；18物体均696组，权重有效、蓝图与贴图链接检查通过，errors=[]。本轮检查是结构/有效性，未逐顶点反向比对Blender权重。已保存居中视图和preview.png，预览经人工查看。
- 两个特效组件无自动材质贴图；火箭靴原工具自动材质在预览呈噪声纹理，游戏特效shader未重建。相关说明写入目标根 `提取说明.txt`。
- 骨SRV为晚读，本结果用于静态编辑与全局VG，不保证指定帧姿态，不含未采集LOD或游戏原生命名层级rig。严格单帧证明不足的早期归档仅保留在captures，未当作正式源。
- 验证：三采样gate、72输出缓冲比对、6项拒绝门槛与非空目录保护、Blender实际导入、compileall/diff检查。所有采集连接已释放。

## 最新真实游戏验证（2026-10-05）

- 用户重新注入后，实际连接 `Endfield.exe` PID `38252`，进程报告探针 `0.1.1`，分辨率 `2560×1600`。新增辅助设备后采集仍绑定原主交换链，修复在本次实机生效。
- 帧 `9113`：`285` draw、`170` 资源、`184` 着色器；连续模式读取 `15` 个摘要（每 2 帧采样，frame `9156–9184`），每个含 `284–286` draw。
- 通过探针读回 backbuffer 生成 `current-screen.png`，界面明确为伊冯。帧 `9113` 的 `30` 次绘制、`6` 个唯一 IB 与已扫描的 Yvonne INI 的 hash/index_count 精确匹配；仅该匹配不推断 mod 开关状态。
- 帧 `14377`：`286` draw、`170` 资源，并保存 `13,979,968` 字节 draw 时刻 CB 快照。三组角色 IB `d160fb72`（123678 索引）、`c0bb7cd6`（80781）、`06e91fc9`（20331）的完整 IB/VB0/VB1/VB2 共 `12` 个读回缓冲 CRC32C 全匹配捕获哈希。
- 成功读取 `5` 张绑定纹理并保存 DDS/PNG（含 `5485c97a`，2048² BC7）；VS `3625594f80f7ac6f` 和 PS `dfadffc708665def` 字节码与反汇编均可取得。
- 证据在 `captures/verify-game-38252/`：`capture.json`、`character-capture.json`、`character-verification.json`、`analysis.json`、`live.json`、`current-screen.png` 和资源文件。纹理/普通缓冲读回为读取时刻内容；CB 快照为 draw 时刻内容。已停止本次 live 并释放捕获资源，游戏仍运行。
- 本轮只读 EFMI；未修改游戏/EFMI/启动器。未执行完整骨架重建、全角色网格导出或其他角色/LOD 验证。

此前旧版 `0.1.0` 的 PID `39000`、空帧 `22964` 是修复前故障证据，不是当前状态。

## 本轮问题与修复

BUG-001：后建辅助 D3D11 设备覆盖 `g_dev/g_ctx`，主画面的 `g_swap` 仍保留，导致旧主上下文的 draw 被 `h == g_ctx` 条件过滤。

日志顺序：22:46:41 挂接主设备/上下文 → 22:46:58 发现主交换链 → 22:47:04 出现 `a new device replaces ... as the tracked device`。对应代码位于 `bootstrap.cpp` 的 `probe_attach` 和 `hooks.cpp` 的设备/上下文安装逻辑。

`0.1.1` 已将设备钩子安装与采集目标选择分离。首个发现的交换链通过 `GetDevice` / `GetImmediateContext` 确定并持有所属设备/上下文；辅助设备不再覆盖目标。逐上下文保存 `context1` 能力，GPU writer 限定所选上下文和已发现的交换链。状态新增目标指针、所选上下文 draw 计数，空捕获会警告。

选定目标后，不接受另一个设备的替代交换链，日志提示重启；多个交换链中自动判断主画面尚不支持。同设备交换链销毁/重建期间的跨界捕获完整性未验证，详见已知问题。

live 同时增加 generation：停止（`enable=false`）或断开连接后，旧会话的在途帧不能回填摘要；`enable=true` 的参数重配不更换 generation。

新版 `dist/efmi_probe.dll` 已就位，大小 `355840` 字节，SHA256：`b0fee912263763c9db28adc21798505845c9c803606bbf3969227a2bc948fa8a`。旧版 `0.1.0` 备份在 `dist/backup-before-multidevice/`。DLL 固定到进程退出，不支持现有进程热替换。EFMI 原目录 `56` 个文件最终 SHA256 核对 `changed=[]`，无变化。

## 本轮最终验证

| 检查 | 当前结果 |
| --- | --- |
| XXMI 哈希对照 | 三种加载顺序各 `69` 项、`0` mismatch，二次捕获 IB/VB 哈希一致均为 `True` |
| 探针自测 | `83` 通过 / `0` 失败，含新增 live 会话隔离检查 |
| 多设备专项 | 无 XXMI `96` 项、有 XXMI `96` 项，全部通过，涵盖辅助设备先/后创建 |
| MCP 端到端 | `39` 通过 / `0` 失败 |
| 基础检查 | `git diff --check`、Python `compileall` 通过 |
| 新版真实游戏角色数据 | 伊冯画面非空捕获、连续数据、IB/VB/CB/纹理/着色器读回均已验证 |

完整日志：`sandbox/build/verification-0.1.1.log`，最终运行退出码 `0`；全部八个阶段显示 `PASS`。这些是本地沙盒结果；真实游戏的独立证据见本文首节。

验证中另修了测试场景：posed VB 在旧探针 + 无 XXMI 时也可出现 `39` 个 float 的 `1 ULP` 变化（最大 `5.96e-8`），每次探针 hash 与读回 CRC 一致，不是本轮探针回归。测试 shader 加 HLSL `precise`，保留严格断言；有/无 XXMI 各 `12` 次连续首帧捕获，`11616` 字节均一致，CRC `1b4be723`。具体优化机理仍属推断。

## 历史验证基线

README 的历史结果：XXMI 三种加载顺序各 `69` 项一致、真实离线抓帧 `586` 个 IB/VB 一致、沙盒探针自测 `73` 项、MCP 端到端 `39` 项通过。这是既有基线，不是本次修复后的实测结果，也不能证明当前真实角色捕获正确。

本轮尚未重跑真实离线 `586` 个 IB/VB 对照；T-003 的当前伊冯画面验证已完成，范围见首节。

## 恢复入口

先读本文件与 `TASKS.md`；实现细节见 `ARCHITECTURE.md`，约束见根 `AGENTS.md` / `DECISIONS.md`，问题见 `KNOWN_ISSUES.md`。接续时先确认实际源码、产物和加载中的进程版本，不将 PID 或帧号当作永久配置。
