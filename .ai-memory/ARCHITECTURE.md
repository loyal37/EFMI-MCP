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
| `efmi_mcp/reload_input.py` | Win32 有效窗口/前台/修饰键检查、固定 F10 输入与异常释放；不提供任意按键接口 |
| `efmi_mcp/probe_client.py` | 发现 `\\.\pipe\efmi-probe-<PID>` 并发送请求；每个探针同时服务一个客户端 |
| `efmi_mcp/capture_model.py`、`decode.py`、`shader.py` | 捕获模型、缓冲/贴图解码、反汇编 |
| `efmi_mcp/mods.py`、`fa_dump.py`、`hashes.py`、`parity.py` | 只读 mod/抓帧解析、哈希与对照验证 |

管道消息为 `[u32 header_len][u32 payload_len][header JSON][payload]`。服务内保留最近六次捕获的元数据，但探针只保证最新一次捕获的资源仍可读。普通读取返回读取时刻的内容；需要 draw 当时的 CB 应用 `cb_data=true`。

`hashes.py` 在导入时尝试绝对路径加载 `dist/efmi_crc32c.dll`，CPU SSE4.2检查及已知向量通过后加速CRC；缺库/不支持时保持Python回退。源文件与构建位于 `server/native/`，完全独立于注入探针。`decode.texture_to_pngs` 同次解码生成完整PNG/预览，压缩等级1保留像素。

### 服务 0.1.2：重载与采集工作流

`efmi_reload_and_capture` 为有副作用且不可假定幂等的 MCP 工具，探针协议/DLL 保持 0.1.1。工作流读取当前根目录 `d3dx.ini`，仅接受 `[Hunting]` 中各一条 `reload_config`、`reload_fixes = no_modifiers VK_F10`，不改原配置。使用已连接的同一客户端，核对管道 PID、XXMI 路径及 device/context/swap chain；检查不能进行中的普通单帧捕获，live 的 recording 则先停止。

执行阶段清旧会话捕获/latest、失效 `ModCache`、停止 live 并 release 保持资源。输入模块只对该进程窗口发送一次 F10 按下/释放，按住约 200ms，检查焦点及修饰键，异常也尝试释放；不重复按下。默认释放后等 2000ms，再获取 barrier 状态。捕获必须非空、帧号大于原帧及 barrier，且前后 PID/渲染目标一致，才作为 latest 发布。后续读取复用现有 MCP 资源接口，live 保持停止。

没有 XXMI 重载完成回执，返回 `reload_requested` 与恒为 false 的 `reload_confirmed`，不把输入接受或新帧等同于 INI 成功解析。预检失败保留旧捕获；执行后的错误清空旧结果并报告输入是否已发送或未知，不自动重试 F10。`timeout_ms` 仅传给后续 probe capture。此服务更新只需重启 MCP，不需重新注入或开启狩猎；正常 F10 的持久变量保存属于实际副作用。

## 构建与验证入口

- `probe/build.bat`：构建探针；`sandbox/build.bat`：构建沙盒。
- `sandbox/prepare_run.py`：把只读 EFMI 运行时复制到本项目 `sandbox/run/`。
- `sandbox/run_all.py`：准备副本、三种加载顺序的哈希对照、探针自测、MCP 端到端测试，以及两组无系统输入的自动重载单元测试。
- `sandbox/selftest.py`、`mcp_test.py`、`parity_test.py`、`perf_test.py`：对应专项验证。
- `sandbox/reload_input_test.py`：19 项固定 F10 / Win32 ABI / 焦点 / 修饰键 / 释放回归；`sandbox/reload_workflow_test.py`：30 项真实 MCP 调用边界、缓存失效、同一目标、新帧与错误状态检查。专项使用 fake 输入/探针，不接触用户游戏；本轮两组及既有 MCP 39 项通过。
- `sandbox/reload_integration_test.py`：显式单独运行的真实 F10 集成，会聚焦新建私有沙盒，发现其他探针则拒绝；不纳入自动全套。已在 `hunting=0` 下证明新 skip 规则只移除目标 IB draw，其余 IB 次数、新 mod 索引、缓存失效和保留 IB 读回正确，子进程正常退出。证据 `sandbox/build/reload-xuxnsq73/reload-evidence.json`。沙盒处理 `SC_KEYMENU`，避免默认 F10 菜单模式阻塞渲染；测试规则同时指定 VS 的 `checktextureoverride=ib` 和 IB `match_index_count`，不依赖狩猎模式。
- `sandbox/multidevice_test.py`：辅助设备先/后创建，以及有/无 XXMI 的多设备专项；旧版探针保存在 `dist/backup-before-multidevice/`，用于复现对照。
- `server/native/build.bat`、`scripts/test_crc32c.py`：独立CRC加速库构建/种子与回退语义回归；`sandbox/decode_test.py`：PNG像素及预览等价性。MCP测试仅在自己的子进程屏蔽测试开始时已存在的probe，不连接用户已有游戏。

`dist/`、构建目录、`sandbox/run/`、`captures/`、日志被 `.gitignore` 排除；不要因未出现在 Git diff 中就假设构建产物已经部署或加载。

## LoyalTools 合并骨骼桥接（2026-10-06）

`scripts/capture_merged_source.py` 保存完整共享几何/骨池、draw时刻CB、角色材质DDS及读回证据。IB/VB完整资源必须由探针确认自捕获后未变化；XXMI region哈希仅是附加检查，实际几何范围必须从完整IB的min/max索引计算。

`server/efmi_mcp/loyal_bridge.py` 只读动态导入本机LoyalTools，不执行依赖bpy的包入口、不写参考项目字节码。将Capture转换为其原生帧模型，直接调用原角色分组、矩阵去重、来源选择（含梨诺火箭靴黑名单）和工作空间写出。CB快照已按原first_constant裁剪，桥接时first_constant归零。每draw保留独立布局、原索引范围和signed BaseVertex。

0.1.1的骨骼SRV只能晚读。静态编辑模式必须用三份独立采样，核对实际几何字节、canonical骨组来源、实际带权跨组件合并和变化中的有效骨矩阵；这不是指定帧姿态快照。CLI入口 `scripts/export_merged_workspace.py` 输出目标根目录下的 `workplace`；`scripts/verify_merged_workspace_blender.py` 用已安装LoyalTools实际导入、检查权重/贴图/蓝图并保存新 `.blend`。不生成未采集的LOD映射。

实时归档保存紧凑JSON与阶段 `durations_ms`；第二次身份复核不算VB哈希，IB/CB及第一帧完整buffer校验保留。离线桥接每个样本独立缓存经过SHA验证的整buffer，以克隆reader函数的局部globals供原算法读内存，不修改LoyalTools源文件/模块globals；子网格结果使用deep-copy隔离后续输出命名等变更。

辅助样本验证结束即清除其整buffer缓存，导出finally清空已登记缓存；样本保留实际几何切片/矩阵副本用于三采样比较。`scripts/test_loyal_bridge_performance.py` 从指定Git版本载入旧桥接，与当前实现交错导出、对比文件、统计源文件读取、验证拒绝门槛/NumPy读语义/缓存释放；默认使用项目内已有liimo-static-a/b/c，只在captures内写测试输出。
