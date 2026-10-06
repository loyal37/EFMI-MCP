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
| `efmi_mcp/probe_client.py` | 发现 `\\.\pipe\efmi-probe-<PID>` 并发送请求；每个探针同时服务一个客户端 |
| `efmi_mcp/capture_model.py`、`decode.py`、`shader.py` | 捕获模型、缓冲/贴图解码、反汇编 |
| `efmi_mcp/mods.py`、`fa_dump.py`、`hashes.py`、`parity.py` | 只读 mod/抓帧解析、哈希与对照验证 |

管道消息为 `[u32 header_len][u32 payload_len][header JSON][payload]`。服务内保留最近六次捕获的元数据，但探针只保证最新一次捕获的资源仍可读。普通读取返回读取时刻的内容；需要 draw 当时的 CB 应用 `cb_data=true`。

## 构建与验证入口

- `probe/build.bat`：构建探针；`sandbox/build.bat`：构建沙盒。
- `sandbox/prepare_run.py`：把只读 EFMI 运行时复制到本项目 `sandbox/run/`。
- `sandbox/run_all.py`：准备副本、三种加载顺序的哈希对照、探针自测、MCP 端到端测试。
- `sandbox/selftest.py`、`mcp_test.py`、`parity_test.py`、`perf_test.py`：对应专项验证。
- `sandbox/multidevice_test.py`：辅助设备先/后创建，以及有/无 XXMI 的多设备专项；旧版探针保存在 `dist/backup-before-multidevice/`，用于复现对照。

`dist/`、构建目录、`sandbox/run/`、`captures/`、日志被 `.gitignore` 排除；不要因未出现在 Git diff 中就假设构建产物已经部署或加载。
