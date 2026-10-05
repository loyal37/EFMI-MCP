# EFMI MCP

让 agent 在《明日方舟：终末地》运行时，实时拿到以往要靠 EFMI/XXMI 抓帧（frame analysis dump）才能得到的数据：每个绘制调用用了哪个索引/顶点缓冲、哪些着色器、哪些贴图和常量缓冲，以及它们的哈希和内容。

不修改任何原版工具：XXMI 的 `d3d11.dll`、`d3dx.ini`、`Core\`、XXMI Launcher、现有 mod 都只读不写。

## 状态

| 项目 | 状态 |
|---|---|
| 与 XXMI 1.2.0 的哈希一致性 | 沙盒中已验证，三种加载顺序各 69 项对比零差异 |
| 用真实抓帧离线验算区域哈希 | 586 个 IB/VB 全部一致（`E:\ssmt4e\EFMI` 下的抓帧） |
| 探针功能自测 / MCP 端到端 | 73 项 / 39 项全部通过（沙盒） |
| **在真实游戏中运行** | **尚未验证**，需要你在启动器里开启后实测，见下文 |

沙盒是一个按 Unity/终末地方式使用 D3D11 的测试程序，配上一份 XXMI 运行时的私有拷贝。它能验证探针与真正的 XXMI DLL 共存、哈希一致、不卡顿，但验证不了游戏本身的行为，尤其是反作弊。

## 风险

游戏带 ACE 内核反作弊。探针是一个额外注入游戏进程的未签名 DLL，做的事情和 XXMI 同类（拦截 D3D11 调用），但 ACE 是否容忍它**没有任何实测依据**。账号风险需要你自己判断；建议先用小号试。

## 组成

```
游戏进程                                        游戏外
┌───────────────────────────────────┐          ┌──────────────────────────┐
│ Endfield.exe                      │          │ MCP 服务 (Python, stdio) │
│   XXMI d3d11.dll   （原版，未改） │  命名管道 │   server\efmi_mcp        │◄── agent
│   efmi_probe.dll   （探针）       │◄────────►│                          │
│     挂在系统 D3D11 对象上         │          │ 导出文件 -> captures\    │
└───────────────────────────────────┘          └──────────────────────────┘
```

- **探针** `dist\efmi_probe.dll`：由启动器在游戏启动时一并注入。它只替换系统 D3D11/DXGI 对象的虚表指针，不改 XXMI 模块，也不改游戏模块。哈希按 XXMI 公开源码中的算法重算。
- **MCP 服务** `server\`：纯 Python 标准库实现，无需安装依赖（贴图预览用到本机已有的 Pillow/numpy，没有也能运行）。

探针平时什么都不采集。agent 连接后才开始跟踪缓冲写入；请求一帧时记录下一帧并立即返回；开启连续模式后每帧自动记录摘要并保留最近若干帧。全部在内存里，不按帧写盘。

## 安装

1. 构建（已构建过可跳过）：

   ```bash
   probe\build.bat
   ```

2. 让启动器在开游戏时加载探针。打开 XXMI Launcher → 设置 → 高级设置 → 勾选 **注入库（Inject Libraries）**，在列表里加一行：

   ```
   F:\EFMI MCP\dist\efmi_probe.dll
   ```

   这一项带签名，只能在启动器界面里设置，不能直接改它的配置文件。之后照常从启动器启动游戏。

3. 把 MCP 服务注册给 agent。本目录下的 `.mcp.json` 已对在此目录启动的 Claude Code 生效；要在别的项目里也能用：

   ```bash
   claude mcp add --scope user efmi -- "D:\Python312\python.exe" "F:\EFMI MCP\server\run_server.py"
   ```

   Codex 在 `%USERPROFILE%\.codex\config.toml` 里加：

   ```toml
   [mcp_servers.efmi]
   command = "D:\\Python312\\python.exe"
   args = ["F:\\EFMI MCP\\server\\run_server.py"]
   ```

4. 进游戏后让 agent 调用 `efmi_status`。应看到 `attached: True`。如果显示未加载或未挂接，看 `dist\efmi_probe.log`。

卸载：在启动器里取消"注入库"即可；探针不会在别处留下东西。

## 工具

| 工具 | 作用 |
|---|---|
| `efmi_status` | 游戏和探针是否就绪、分辨率、已见到的资源数、已安装 mod |
| `efmi_capture_frame` | 记录下一帧（`full` 含全部绑定；`cb_data=true` 连同每个 draw 当时的常量缓冲内容） |
| `efmi_list_draws` | 按哈希、调用类型、来源等过滤列出绘制调用 |
| `efmi_get_draw` | 单个绘制调用的完整管线状态 |
| `efmi_find_hash` | 某个哈希在这一帧里以什么角色出现，以及哪些 mod 的 ini 引用了它 |
| `efmi_read_buffer` | 读索引、顶点（按输入布局解码）或常量缓冲 |
| `efmi_read_texture` | 读贴图或渲染目标，存 DDS/PNG 并返回预览图 |
| `efmi_get_shader` | 着色器反汇编 |
| `efmi_export_mesh` | 导出某个 draw 的网格（OBJ + 原始缓冲 + 描述） |
| `efmi_live_start` / `efmi_live_now` / `efmi_watch_hash` / `efmi_live_stop` | 连续模式：当前画了什么、某个哈希逐帧是否出现 |
| `efmi_diff_captures` | 两次抓取之间多了/少了哪些网格 |
| `efmi_list_mods` | EFMI 目录里会被加载的 mod 及其引用的哈希（不需要游戏） |
| `efmi_frame_dump_summary` | 读已有的抓帧目录（不需要游戏） |
| `efmi_verify_against_dump` | 自检：把一次抓取和 XXMI 的抓帧对比，看哈希是否仍一致 |

`ib` 哈希就是 mod 里 `[TextureOverride] hash =` 用的区域哈希。

## 已知限制

- 探针必须在游戏创建 D3D 设备之前加载，所以只能由启动器在开局注入，不能附加到已运行的游戏。
- 看到的是真正发给显卡的调用。某个 mod 生效时，游戏对该网格的原始绘制已被替换，不会出现；出现的是 mod 发出的绘制，来源标为 `xxmi`。来源是按调用栈深度判断的启发式结果。
- 缓冲和贴图的读取返回"读取那一刻"的内容。要某个 draw 当时的常量缓冲，抓取时加 `cb_data=true`。
- 由 GPU 写入的缓冲（计算着色器蒙皮等），XXMI 自己缓存的哈希可能是旧值；探针给的是当前字节的哈希，两者可能不同。
- 只有最近一次抓取的资源可读；延迟上下文（deferred context）里的绘制看不到，`efmi_status` 会提示是否出现过。
- 多重采样贴图和三维贴图暂不支持读回。
- XXMI 的 `shader_hash = bytecode` 模式未实现（EFMI 默认是 `3dmigoto`）。

## 性能

沙盒实测，每帧约 4000 次绘制、XXMI 同时在场：

- 探针已加载但无人连接、或已连接但不采集：帧率与不加载探针相比测不出差别。
- 连续模式：探针内部计时每帧约 0.6 毫秒。
- 请求一次完整抓取：被记录的那一帧多花约 9 毫秒。
- 从显存读回数据是异步的，渲染线程不等待 GPU。

两次运行之间帧率本身有约 1.5 毫秒的波动，所以以上以探针内部计时为准。真实游戏里的数字未测。

## XXMI 更新之后

启动器会自动更新 XXMI。哈希算法若有改动，探针会跟着算错，所以更新后跑一遍：

```bash
sandbox\build.bat
python sandbox\run_all.py
```

它会从 EFMI 目录拷一份运行时到 `sandbox\run`（原目录只读），让那份 XXMI 自己抓帧当作标准答案，与探针逐项对比。也可以在游戏里手动抓一帧，再让 agent 调 `efmi_verify_against_dump`。

## 目录

```
probe\      探针源码 (C++，MSVC x64)
server\     MCP 服务 (Python)
sandbox\    测试程序和测试脚本，不属于交付物
dist\       构建出的 efmi_probe.dll 和运行日志
captures\   导出的网格、贴图、着色器反汇编
```
