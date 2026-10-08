# EFMI MCP — 架构说明

## 数据链路

`agent → Python MCP stdio → 命名管道 → efmi_probe.dll → 系统 D3D11/DXGI 对象 → 捕获/读回 → MCP 结果或导出文件`

探针与 XXMI 同处游戏进程。`bootstrap.cpp` 重定向系统 D3D11 模块内存中的创建设备导出入口，取得真实设备；`hooks.cpp` 替换真实 COM 对象的虚表指针。原版 XXMI DLL 文件及模块不作为修改目标。

## 探针模块

| 文件 | 职责 |
| --- | --- |
| `probe/src/dllmain.cpp` | 加载日志、配置、注册表、捕获、bootstrap、IPC；固定 DLL 到进程退出 |
| `probe/src/bootstrap.cpp` | 发现系统运行时，拦截创建设备，解析真实设备/立即上下文 |
| `probe/src/hooks.cpp` / `hooks.h` | 设备/上下文/交换链钩子，Present 帧边界，绘制与资源写入跟踪 |
| `probe/src/registry.cpp` / `registry.h` | 资源/着色器/输入布局登记、创建哈希、缓冲写入历史 |
| `probe/src/capture.cpp` / `capture.h` | 单帧与连续捕获、管线绑定快照、GPU 数据读回、摘要 |
| `probe/src/hash.cpp` / `hash.h` | XXMI 兼容哈希计算 |
| `probe/src/ipc.cpp` | 命名管道服务与请求分派 |
| `probe/src/util.cpp` / `common.h` | 日志、配置、模块信息、共享声明 |

设备、立即上下文、交换链必须属于同一渲染目标；绘制记录按所选上下文过滤，Present 决定捕获帧边界。`0.1.1` 的 `hooks_attach_device` 分离安装钩子与选择捕获目标：先为设备建立钩子，首个发现的交换链通过 `GetDevice` / `GetImmediateContext` 绑定对应目标，并持有设备/上下文引用；辅助设备不会覆盖已选目标。`context1` 能力按上下文保存，GPU writer 跟踪也限定所选上下文且要求已有交换链。

选定目标后，其他设备创建的替代交换链会被拒绝，日志提示重启；目前不支持多个交换链中自动判定哪个是主画面。同设备的交换链销毁/重建期间，跨边界的捕获完整性仍待验证。

`ipc.cpp` / MCP 状态返回捕获目标指针和所选上下文的 draw 计数；空捕获显示警告。连续模式捕获带 generation，停止、重启或客户端重连后，旧 generation 的在途帧不得再发布到新会话；该修复已通过 `83` 项自测及最终整套回归。

## MCP 服务模块

| 文件 | 职责 |
| --- | --- |
| `server/run_server.py`、`efmi_mcp/__main__.py` | 入口、EFMI 只读根目录与输出目录参数 |
| `efmi_mcp/mcp_stdio.py`、`tools.py` | MCP 协议、工具定义、会话与捕获缓存 |
| `efmi_mcp/reload_workflow.py` | 同一管道/PID/渲染目标下预检、停止 live、失效旧缓存、单次 F10 后等待和新帧门槛 |
| `efmi_mcp/reload_input.py` | `send_reload_via_probe`：让探针在游戏内按 F10 并判读回执；`send_reload_f10`：旧的外部 Win32 按键（仅非管理员游戏可达）。均不提供任意按键接口 |
| `efmi_mcp/model_capture.py` | 模型流程的采样：单帧归档（几何／骨骼缓冲、贴图、CB 快照、截图）与多次独立采样；游戏重写的顶点流另采一帧轻量帧即时读取并校验（`_read_volatile`），失败重采 |
| `efmi_mcp/loyal_bridge.py` | 调用 LoyalTools：候选角色、工作空间提取与三采样校验、LOD 帧匹配与 JSON 匹配；解析要使用的 LoyalTools 目录 |
| `efmi_mcp/blender_runner.py` | 找 Blender 及其已安装的插件目录；后台运行脚本，输出进日志文件 |
| `efmi_mcp/model_jobs.py` | 任务线程、阶段、取消；同一时间一个任务 |
| `efmi_mcp/model_workflow.py` | 项目目录、提取／LOD／导出的编排、导出产物检查与清单 |
| `efmi_mcp/mods_toggle.py` | F11 开关全部 mod：核对 `[KeyToggleMods]` 绑定、让探针按键、采集新帧并从 EFMI 的提示文字读回状态，最多两次按键 |
| `efmi_mcp/mod_runtime.py` | 部署到专用目录（标记、备份、SHA256 核对）、冲突列表、绘制与贴图对照 |
| `efmi_mcp/model_tools.py` | 模型流程的七个 MCP 工具 |
| `efmi_mcp/probe_client.py` | 发现 `\\.\pipe\efmi-probe-<PID>` 并发送请求；每个探针同时服务一个客户端 |
| `efmi_mcp/capture_model.py`、`decode.py`、`shader.py` | 捕获模型、缓冲/贴图解码、反汇编 |
| `efmi_mcp/pipeline.py` | 固定管线状态、采样器、视图参数、间接绘制参数的名字和文字（探针 0.1.5 的数据） |
| `efmi_mcp/draw_outputs.py` | `efmi_capture_draw_outputs`：把选择换成匹配条件、抓帧、读回绘制前后的渲染目标拷贝、比较、存图、释放 |
| `efmi_mcp/mods.py`、`fa_dump.py`、`hashes.py`、`parity.py` | 只读 mod/抓帧解析、哈希与对照验证 |

管道消息为 `[u32 header_len][u32 payload_len][header JSON][payload]`。服务内保留最近六次捕获的记录及已取回的 CB／间接参数快照字节，但探针只保证最新一次捕获的普通资源仍可读。普通读取返回读取时刻的内容；需要 draw 当时的 CB 应用 `cb_data=true`。

`hashes.py` 在导入时尝试绝对路径加载 `dist/efmi_crc32c.dll`，CPU SSE4.2检查及已知向量通过后加速CRC；缺库/不支持时保持Python回退。源文件与构建位于 `server/native/`，完全独立于注入探针。`decode.texture_to_pngs` 同次解码生成完整PNG/预览，压缩等级1保留像素。

### 探针 0.1.6 / 服务 0.1.12：调用当时的内容

- 数据：`Capture::conts`（`ContSnap`：缓冲的一个版本，或贴图的一个子资源）、`tracks`（按资源指针：缓冲的当前版本号、补丁表 `ContPatch{ver, off, size, chunk, at}`、是否已封口／丢失；贴图的“还没被写过的被引用子资源”表）、`cchunks`（存补丁的暂存缓冲，4 MiB 一块，大补丁单独一块）。绑定记录各带一个 `cont`（`ib_cont`、`VbBind`／`CbBind`／`ViewBind`），序列化为与 `vb`／`cb`／`srv`／`out` 平行的 `cvb`／`ccb`／`csrv`／`cout` 和 `cib`，表本身是 `cont`：`[资源, 0, 版本]` 或 `[资源, 1, mip, 切片]`。
- 引用：`cap_on_draw`／`record_stage` 对每个绑定调 `cont_buffer`／`cont_of_view`（请求里的 `ct_kinds` 决定哪几类）；输出在原调用返回后由 `cap_after_draw` 引用（`ct_out_kinds` 加 `ct_match`／`ct_out_all` 选调用），Dispatch 也走这条路。
- 写前拷贝：钩子在放行前调 `cap_before_write`（CPU 侧写入和拷贝，带子资源和字节范围）、`cap_before_view_write`（经视图的清除、GenerateMips）、`cap_before_call_writes`（绘制／Dispatch 的输出，取自 `g_om_out`／`g_cs_out`，绑定变过才向运行时重查）、`cap_before_unknown_writes`（command list：置 `ct_unknown`）。Present 前 `cont_before_present` 处理交换链的后备缓冲。只在 `g_barrier`（有抓取在保留内容）时生效，`g_bar` 指向那次抓取。
- 读回：`read_content`（任务类型 6）。贴图：有拷贝读拷贝，否则原地暂存一份。缓冲：`start_read_content` 选出版本号不小于所问版本的补丁（从新到旧），需要时再拷一份现在的缓冲；`finish_read_content_buffer` 按顺序盖上去。`release_content`（类型 7）只放内容。`stage_subresource` 是暂存一个子资源的公共入口（1D／2D／3D、块压缩取整、多重采样先解析），`read_texture` 也改用它。
- 写入列表：`g_events` 置位的那一帧，各写入钩子调 `cap_event`／`cap_event_view`，记下种类、“在第几次调用之前”、目标和来源资源、四个数、清除值；序列化为 `ev`。
- 预算与内存：`ct_budget`（字节）；`memory_allows` 在分配大块前看空闲物理内存和提交量，低于 2 GiB 余量就不拷。丢失的项在读取时报原因（`k_cont_why`）。
- 服务端：`slot_data.py`（槽位露出缓冲的哪一段、字节从哪来）、`contents.py`（抓取选项、写入的文字、`dump_frame` 导出）、`capture_model.py`（`content()`、`events()`、各绑定的 `content`、`viewports`）、`parity.compare_contents`（与 XXMI 导出逐字节比较）。
- 线程：与原来一致，钩子、帧边界、读回任务都在使用即时上下文的线程上；序列化只读记录结束后不再变的字段（`cont` 表的资源／版本／子资源），状态类字段只在读回时给。

### 探针 0.1.5 / 服务 0.1.8：管线状态与绘制前后的渲染目标拷贝

- 完整抓取时 `cap_on_draw` 多记：`record_fixed_state`（混合／深度模板／光栅化状态对象及其动态值、裁剪矩形、第一个视口的数值和视口总数）、`record_stage` 里的采样器、每个被绑定视图的描述（`view_desc_id`）、间接参数的快照。状态对象和视图各有一张表（`states`、`views`），绘制用下标引用；表项按内容去重，地址只用来找候选。2026-10-08 审计纠正此前“全部视口”的表述：代码虽查询全部，仅序列化第一个。
- 抓取请求的 `rt` 选项给出一组 `DrawMatch`（绘制编号、索引缓冲对象与序号、索引数、起始索引、VS／PS 哈希、任一着色器哈希、来源；设了的字段都要相符）。匹配且有目标可拷贝的前 `rt_max` 个绘制：`rt_copy` 在绘制前把目标的那一级 mip／切片（可限 `box`）拷进一块新建的暂存纹理，并把视图留给 `cap_after_draw`；钩子在原函数返回后见到 `g_rt_after` 就调它，拷贝第二份。拷贝随抓取存活，`read_rt` 按序号读（走已有的读回任务队列，类型 4），`release_rt`（类型 5）或下一次抓取、客户端断开时释放。
- 计数：`rt_matched`（匹配的绘制）、`rt_draws`（做了拷贝的）、`rt_depth_only`（没有颜色目标且没要深度的）、`rt_bytes`、`rt_over_budget`。
- 服务端：`Capture.draw()` 把新增的键展开成 `blend`／`depth_stencil`／`rasterizer`／`scissors`／`samplers`／各槽位的 `view`／`indirect.args`；缺这些键（旧探针或轻量抓取）时照旧。

### 探针 0.1.2 / 服务 0.1.4：进程内重载按键与重载证据

`probe/src/reload.cpp` 实现管道命令 `reload_key`，在 IPC 线程执行；渲染线程只在按键进行期间于 Present 钩子里记时间并置事件（`reload_on_present`）。流程：核对交换链、XXMI、d3dx.ini 两项默认绑定、窗口与按键状态 → 取前台（`direct`，被拒则 `attach_input`）→ 等 3 帧 → `SendInput` 按下并等到读到按下状态 → 在第二个 Present 或晚于 10ms 的第一个 Present 处松开 → 可选还原焦点。回执经 `{"ok":true,"result":{...,"code":...}}` 返回，`code!="ok"` 由服务端转成 `ReloadInputError`。0.1.3 起同一过程（`key_press_run`）也用于命令 `mods_key`：按 F11，前置检查换成 `Core\EFMI\KeyBindings.ini` 的 `[KeyToggleMods]` 以及 `[Hunting]` 中不带修饰键的 F11 绑定，其余相同。

状态的 `features` 列表决定服务端走哪条路径（`reload_workflow.py`）：含 `reload_key` 用 `send_reload_via_probe`，否则用旧的 `send_reload_f10`。`reload_evidence()` 读取捕获里资源/着色器的 `cf`，与回执的 `frame_at_key_down` 比较。0.1.3 一节描述的预检、失效旧缓存、等待和新帧门槛不变。

### 服务 0.1.3：重载请求、输入状态与采集工作流（外部按键路径，旧探针仍用）

`efmi_reload_and_capture` 为有副作用且不可假定幂等的 MCP 工具，探针协议/DLL 保持 0.1.1。工作流读取当前根目录 `d3dx.ini`，仅接受 `[Hunting]` 中各一条 `reload_config`、`reload_fixes = no_modifiers VK_F10`，不改原配置。使用已连接的同一客户端，核对管道 PID、XXMI 路径及 device/context/swap chain；检查不能进行中的普通单帧捕获，live 的 recording 则先停止。

执行阶段清旧会话捕获/latest、失效 `ModCache`、停止live并release保持资源。输入模块只对该进程窗口发送一次F10按下/释放，按住约200ms，检查焦点、修饰键及GetAsyncKeyState高位。`key_down_sent`保留SendInput接受状态，`key_down_observed`要求按住期间至少一次高位可见；未观察到就抛 `ReloadInputError("keydown_not_observed")`，工作流返回input阶段ToolError，finally仍尝试释放，不继续capture、不重复按下。按键门槛通过才默认等2000ms并记录barrier；后续非空帧必须晚于原帧/barrier且前后PID/device一致，才作为latest发布。live保持停止。

没有 XXMI 重载完成回执，返回 `reload_requested` 与恒为 false 的 `reload_confirmed`，不把输入接受或新帧等同于 INI 成功解析。预检失败保留旧捕获；执行后的错误清空旧结果并报告输入是否已发送或未知，不自动重试 F10。`timeout_ms` 仅传给后续 probe capture。此服务更新只需重启 MCP，不需重新注入或开启狩猎；正常 F10 的持久变量保存属于实际副作用。

该状态门槛来自2026-10-06实机失败：SendInput返回接受，但扫描码诊断期间异步高位从未置位，用户移出的模组仍在绘制。它识别失败，不解决底层输入不可见，也不证明游戏线程观察到了按键。未改探针、未添加非公开XXMI调用、未把扫描码切换作为生产修复，详见BUG-004。

## 构建与验证入口

- `probe/build.bat`：构建探针；`sandbox/build.bat`：构建沙盒。
- `sandbox/prepare_run.py`：把只读 EFMI 运行时复制到本项目 `sandbox/run/`。
- `sandbox/run_all.py`：准备副本、三种加载顺序的哈希对照、探针自测、MCP 端到端测试，以及两组无系统输入的自动重载单元测试。
- `sandbox/selftest.py`、`mcp_test.py`、`parity_test.py`、`perf_test.py`：对应专项验证。
- `sandbox/draw_outputs_test.py`（2026-10-08）：`efmi_capture_draw_outputs` 与 `efmi_get_draw` 新字段的离线单元测试（伪探针），已纳入 `run_all.py`。沙盒程序的 `--state-pass` 多画一个所有固定管线状态都取非默认值的间接绘制和一个只写深度的绘制，供自测和端到端测试核对。
- 内容相关（2026-10-08，探针 0.1.6）：`sandbox/contents_test.py`（真探针＋沙盒 `--content-pass`，逐字节核对每次调用的内容）、`sandbox/contents_parity_test.py`（`--content-static`，与私有副本 XXMI 的导出逐字节对照，并逐字比较 `efmi_dump_frame` 写出的 `.txt`）、`sandbox/contents_unit_test.py`（伪探针，离线）；三个都在 `run_all.py` 里。沙盒窗口默认开在副屏、不激活（`--primary-screen` 还原）。`prepare_run.py` 给私有 `d3dx.ini` 的 `analyse_options` 加了 `dds`，让 XXMI 把贴图都写成 DDS 以便比较。
- 2026-10-06 晚：两组单元测试增至 27 / 46 项（新增探针按键路径与重载证据）；`reload_integration_test.py` 改为验证探针按键、证据与焦点还原，探针自己按键时不再因存在其他探针而拒绝。沙盒脚本可用 `EFMI_PROBE_DLL` 指定待测 DLL。以下为 0.1.3 时的说明。
- `sandbox/reload_input_test.py`：21项固定F10/Win32 ABI/焦点/修饰键/状态观察/释放回归；`sandbox/reload_workflow_test.py`：32项MCP边界、缓存、目标、新帧及输入失败检查。本轮两组、既有MCP stdio私有沙盒39项及compileall通过；专项用fake输入/探针，不对用户游戏发键。
- `sandbox/reload_integration_test.py`：单独运行的真实F10集成，聚焦私有沙盒，发现其他探针则拒绝；不纳入自动全套。历史0.1.2在hunting=0下验证skip规则只移除目标IB，证据 `sandbox/build/reload-xuxnsq73/reload-evidence.json`；本轮用户游戏运行时未重跑。沙盒处理SC_KEYMENU避免菜单模式阻塞，规则指定VS的checktextureoverride=ib及IB match_index_count。该历史结果不能替代当前实机失败结论。
- `sandbox/multidevice_test.py`：辅助设备先/后创建，以及有/无 XXMI 的多设备专项；旧版探针保存在 `dist/backup-before-multidevice/`，用于复现对照。
- `server/native/build.bat`、`scripts/test_crc32c.py`：独立CRC加速库构建/种子与回退语义回归；`sandbox/decode_test.py`：PNG像素及预览等价性。MCP测试仅在自己的子进程屏蔽测试开始时已存在的probe，不连接用户已有游戏。

`dist/`、构建目录、`sandbox/run/`、`captures/`、日志被 `.gitignore` 排除；不要因未出现在 Git diff 中就假设构建产物已经部署或加载。

## LoyalTools 合并骨骼桥接（2026-10-06）

`scripts/capture_merged_source.py` 保存完整共享几何/骨池、draw时刻CB、角色材质DDS及读回证据。IB/VB完整资源必须由探针确认自捕获后未变化；XXMI region哈希仅是附加检查，实际几何范围必须从完整IB的min/max索引计算。

`server/efmi_mcp/loyal_bridge.py` 只读动态导入本机LoyalTools，不执行依赖bpy的包入口、不写参考项目字节码。将Capture转换为其原生帧模型，直接调用原角色分组、矩阵去重、来源选择（含梨诺火箭靴黑名单）和工作空间写出。CB快照已按原first_constant裁剪，桥接时first_constant归零。每draw保留独立布局、原索引范围和signed BaseVertex。

0.1.1的骨骼SRV只能晚读。静态编辑模式必须用三份独立采样，核对实际几何字节、canonical骨组来源、实际带权跨组件合并和变化中的有效骨矩阵；这不是指定帧姿态快照。CLI入口 `scripts/export_merged_workspace.py` 输出目标根目录下的 `workplace`；`scripts/verify_merged_workspace_blender.py` 用已安装LoyalTools实际导入、检查权重/贴图/蓝图并保存新 `.blend`。不生成未采集的LOD映射。

实时归档保存紧凑JSON与阶段 `durations_ms`；第二次身份复核不算VB哈希，IB/CB及第一帧完整buffer校验保留。离线桥接每个样本独立缓存经过SHA验证的整buffer，以克隆reader函数的局部globals供原算法读内存，不修改LoyalTools源文件/模块globals；子网格结果使用deep-copy隔离后续输出命名等变更。

辅助样本验证结束即清除其整buffer缓存，导出finally清空已登记缓存；样本保留实际几何切片/矩阵副本用于三采样比较。`scripts/test_loyal_bridge_performance.py` 从指定Git版本载入旧桥接，与当前实现交错导出、对比文件、统计源文件读取、验证拒绝门槛/NumPy读语义/缓存释放；默认使用项目内已有liimo-static-a/b/c，只在captures内写测试输出。
