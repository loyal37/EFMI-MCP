# EFMI MCP

让 agent 在《明日方舟：终末地》运行时，实时拿到以往要靠 EFMI/XXMI 抓帧（frame analysis dump）才能得到的数据：每个绘制调用用了哪个索引/顶点缓冲、哪些着色器、哪些贴图和常量缓冲，以及它们的哈希和内容。

工具不直接修改原版文件：XXMI 的 `d3d11.dll`、`d3dx.ini`、`Core\`、XXMI Launcher、现有 mod 均只读。`efmi_reload_and_capture` 会发送正常 F10 重载请求，因此会产生 XXMI 原有的运行时副作用，包括保存持久变量配置。

## 状态

| 项目 | 状态 |
|---|---|
| 与 XXMI 1.2.0 的哈希一致性 | 沙盒中已验证，三种加载顺序各 69 项对比零差异 |
| 用真实抓帧离线验算区域哈希 | 586 个 IB/VB 全部一致（`E:\ssmt4e\EFMI` 下的抓帧） |
| 探针功能自测 / MCP 端到端 | 0.1.1：83 项 / 39 项全部通过（沙盒，含连续采集停止/重启/重连隔离） |
| 多 D3D11 设备 | 0.1.1 按交换链所属设备选择采集上下文；辅助设备先建/后建、带/不带 XXMI，共 192 项通过 |
| **在真实游戏中运行** | **0.1.1 已在伊冯画面验证**：非空帧、连续摘要、角色 IB/VB/CB、纹理与着色器读回成功；细节见项目记忆 |
| 模型提取 → Blender → 导出 → 游戏内核对 | MCP 服务 0.1.5 接入 LoyalTools 的提取、LOD 匹配、导入和原生导出。离线部分已用真实 LoyalTools 1.6.8 和 Blender 4.5.7 验证；**实时提取和部署核对尚未实机验证** |
| 自动 F10 后重新采集 | 探针 0.1.2 在游戏进程内按 F10 并给出重载证据，MCP 服务 0.1.4。0.1.3 实机失败的原因是游戏以管理员运行，Windows（UIPI）丢弃普通权限进程发来的按键。**2026-10-06 实机验证通过**，游戏在前台和在后台各一次：移出伊冯 mod 后由工具重载，XXMI 的 93 次替换绘制消失，游戏原始的 30 次绘制出现；游戏在后台时探针把它切到前台，按键后把焦点还给原窗口 |

沙盒是一个按 Unity/终末地方式使用 D3D11 的测试程序，配上一份 XXMI 运行时的私有拷贝。它能验证探针与真正的 XXMI DLL 共存、哈希一致、不卡顿，但验证不了游戏本身的行为，尤其是反作弊。

## 风险

游戏带 ACE 内核反作弊。探针是一个额外注入游戏进程的未签名 DLL，做的事情和 XXMI 同类（拦截 D3D11 调用）。目前仅确认一次真实游戏中注入、挂接和通信成功，长期运行兼容性仍未验证。账号风险需要你自己判断；建议先用小号试。

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
- **MCP 服务** `server\`：以 Python 标准库实现，无必需 pip 依赖；可选游戏外 CRC32C 辅助 DLL 加速大缓冲校验（贴图预览用到本机已有的 Pillow/numpy，没有也能运行）。

探针平时什么都不采集。agent 连接后才开始跟踪缓冲写入；请求一帧时记录下一帧并立即返回；开启连续模式后每帧自动记录摘要并保留最近若干帧。全部在内存里，不按帧写盘。

## 安装

1. 构建（已构建过可跳过）：

   ```bash
   probe\build.bat
   ```

   可选的 CRC32C 加速库独立构建：`server\native\build.bat`。生成 `dist\efmi_crc32c.dll`，仅由 Python 服务加载，无需注入游戏；不存在、加载不兼容或 CPU 不支持 SSE4.2 时自动使用 Python 校验。构建后新启动的采集脚本自动使用；已运行的 MCP 服务需重启服务，游戏无需重启。此库不改变 `efmi_probe.dll`。

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

4. 进游戏后让 agent 调用 `efmi_status`。应看到 `attached: True`、交换链已发现、帧号增长；再用 `efmi_capture_frame` 确认实际 draw 和资源非空。状态会显示采集目标的设备/上下文/交换链及所选上下文的 draw 计数，不能仅凭 `attached` 判断角色数据已采集成功。如果未加载、未挂接或持续空帧，看 `dist\efmi_probe.log`。

更新探针后需要退出并重新启动游戏；游戏进程中的 DLL 不会随磁盘文件变化而更新。旧 DLL 仍被游戏占用时，用 `probe\build.bat "F:\EFMI MCP\dist\candidate"` 构建到另一个目录，沙盒脚本通过环境变量 `EFMI_PROBE_DLL` 指向它即可测试。验证后把 `dist\efmi_probe.dll` 改名（被占用的 DLL 可以改名，不能覆盖），再把新 DLL 放到原注入路径，下次启动游戏生效。

**探针 0.1.2 / MCP 服务 0.1.4**：自动重载改由探针在游戏进程内按键，需要重启游戏加载新探针，并重启 MCP 服务。游戏里还是 0.1.1 时，`efmi_reload_and_capture` 仍会失败并提示重启游戏；普通采集不受影响。

**MCP 服务 0.1.5**：增加模型流程的工具，只需重启 MCP 服务；探针仍是 0.1.2。需要本机有 Blender 并已安装 LoyalTools 插件，默认使用 `D:\blender4,5\blender.exe`，可用 `--blender`／`EFMI_BLENDER` 或工具参数 `blender_path` 指定。

卸载：在启动器里取消"注入库"即可；探针不会在别处留下东西。

## 工具

| 工具 | 作用 |
|---|---|
| `efmi_status` | 游戏和探针是否就绪、分辨率、已见到的资源数、已安装 mod |
| `efmi_capture_frame` | 记录下一帧（`full` 含全部绑定；`cb_data=true` 连同每个 draw 当时的常量缓冲内容） |
| `efmi_reload_and_capture` | 改完 mod 后让 XXMI 重载并记录新帧：探针在游戏内按一次无修饰键 F10（必要时先把游戏切到前台，按完把焦点还回去），并返回重载是否发生的证据；会停止 live 并清除旧捕获 |
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
| `efmi_list_models` / `efmi_extract_model` / `efmi_map_lod` | 模型流程：识别角色、提取为 LoyalTools 工程、LOD 映射（见下文“模型流程”） |
| `efmi_export_mod` / `efmi_verify_exported_mod` | 模型流程：用插件原生导出模组；部署、刷新并核对实际绘制 |
| `efmi_model_project` / `efmi_model_job` | 模型流程：项目现状；跟进或取消任务 |

`ib` 哈希就是 mod 里 `[TextureOverride] hash =` 用的区域哈希。

### 修改 mod 后自动重载和采集

`efmi_reload_and_capture` 执行“把游戏切到前台 → 探针在游戏进程内按 F10 → 把焦点还给原窗口 → 等待 → 抓取新帧 → 判断重载证据”。只想获取当前画面时，直接用 `efmi_capture_frame`；普通采集和连续采集无需 F10 或狩猎模式。

**为什么由探针按键。** `Endfield.exe` 的清单要求管理员权限，MCP 服务是普通权限。Windows 的 UIPI 会丢弃低完整性级别进程发给高完整性级别窗口的输入，而 `SendInput` 照样返回成功、没有错误码。这就是 0.1.3 实机测试里“API 接受、始终观察不到 F10 按下、模组也没有刷新”的原因。探针本来就在游戏进程内，由它发出的按键与手按 F10 等价：不修改 XXMI 或游戏的任何代码，按键固定为 F10，客户端无法指定别的键，也只在游戏自己的窗口处于前台时才发。

| 参数 | 默认值与范围 |
|---|---|
| `detail` | `"full"`；也可设为 `"light"` |
| `cb_data` | `true`，保存 draw 当时的常量缓冲 |
| `settle_ms` | `2000`；250–10000 毫秒，F10 释放后等待 |
| `timeout_ms` | `15000`；1000–30000 毫秒，仅为后续捕获超时，不包含等待时间 |
| `focus_game` | `true`，把游戏窗口切到前台；`false` 要求它已在前台 |
| `restore_focus` | `true`，松开 F10 后把前台还给原来的窗口；游戏本来就在前台时无效 |

**前置检查。** 服务端核对同一管道/PID、XXMI 安装路径、渲染设备，以及本安装 `d3dx.ini` 的 `[Hunting]` 中 `reload_config`、`reload_fixes` 各有一条 `no_modifiers VK_F10`（不自动改自定义按键）。探针再独立核对这两项绑定、游戏窗口未最小化、Ctrl/Alt/Shift/Win/F10 均未按住。游戏不在前台且 `focus_game=true` 时先正常请求前台，被 Windows 拒绝再借用当前前台线程的输入状态切换，回执的 `focus_method` 为 `direct` 或 `attach_input`；仍失败则返回 `focus_refused`，不发送按键。随后停止 live、释放资源、清除旧捕获与 mod 索引。live 保持停止。

**按键过程。** 探针先确认游戏在前台连续画出 3 帧，再按下 F10，并从“确实读到按下状态”那一刻开始计帧：`SendInput` 要等系统里所有键盘钩子处理完才返回，这段时间里画出的帧不算数。XXMI 在每次 Present 之前轮询按键，所以按下后的第二个 Present、或距按下超过 10 毫秒才到来的第一个 Present，之前必然有过一次完整轮询；探针在那个 Present 处松开，尽量不让 XXMI 下一帧再重载一次（重载很快时仍可能发生两次）。按住期间失去前台或出现修饰键会立即松开并报错，上限 3 秒。回执包含 `frames_while_held`、`released_on`、`hold_ms`、`focus_restored` 等。

**重载证据。** XXMI 自己不提供回执，但重载会销毁所有 mod 资源和自定义着色器，mod 下次绘制时再由 XXMI 创建。探针记录每个资源和着色器的创建帧，服务端检查新帧实际用到的对象里有多少是 XXMI 在按键之后创建的。有，则 `reload_confirmed=true`，`reload_evidence` 给出数量和示例；没有则为 `false`，画面上没有 mod 内容时这属于正常，需要自己核对改动的 draw 和资源。如果 XXMI 绘制所用的 mod 缓冲早于按键，说明文字会指出重载可能没有生效。`reload_confirmed=true` 也不代表每个 INI 都解析无误。

失败结果保留输入未发送、已发送或状态未知。任何情况下都不自动重发 F10，需要更多画面时使用普通 `efmi_capture_frame`。正常 F10 可能保存持久变量，不能把此操作当作纯只读。从切到前台到还回焦点（通常几十到几百毫秒，取决于重载耗时），键盘输入会落到游戏里。

游戏里的探针早于 0.1.2 时，服务端退回从外部发送按键的旧路径。游戏以管理员运行时它必然失败，错误信息会提示重启游戏以加载新探针。

**验证范围。** `sandbox/run_all.py` 包含无系统输入的单元检查。`python sandbox/reload_integration_test.py` 用一份新的 XXMI 私有副本做真实按键测试：写入新的 mod 文件，由工具重载，核对新规则生效（指定 IB 的 draw 消失，其余不变）、重载证据为真、焦点已还回。它会把沙盒窗口短暂切到前台并发送一次真实 F10；探针只在自己的窗口处于前台时按键，旁边运行的游戏不受影响。前台是管理员窗口（例如正在玩游戏）时，普通权限的沙盒抢不到前台，测试会以 `focus_refused` 结束而不发送按键。沙盒不是管理员进程，UIPI 本身只能在真实游戏里验证。

**实机（2026-10-06）。** PID 43816 报告探针 0.1.2、进程 elevated（完整性 0x3000）。用户移出伊冯 mod 后由工具重载：按键在进程内发出并被读到，持续 1 帧（21696→21697），重载那一帧耗时 92.7ms（平时约 8ms）；重载前 367 次绘制中 XXMI 120 次（93 DrawIndexed + 27 Dispatch），重载后 277 次全部来自游戏，XXMI 的 93 次/18 个网格消失，游戏原始的 30 次/6 个网格出现；`reload_confirmed=true`（XXMI 在按键后创建了新帧所用的 27 个着色器）。游戏继续运行。证据 `captures/verify-reload-inprocess-43816/`。同日 20:51 第二次实机，游戏在后台（前台是 Claude 窗口）：探针以 `direct` 方式取得前台，按键持续 1 帧（91844→91845，重载帧 79.8ms），随后把前台还给原窗口（前后为同一 hwnd）；移出的伊冯 mod 同样消失（XXMI 120→0 次绘制，游戏原始 30 次绘制出现），`reload_confirmed=true`。`attach_input` 这条备用取前台路径在实机上没有用到，只在沙盒验证。目录内 `background-result.json` / `background-comparison.json` 是第二次的结果；第一次的 JSON 被第二次运行覆盖，保留了它的控制台输出 `foreground-run-console.txt`。

0.1.3 及之前的实机失败证据保留在 `captures/verify-reload-*`。

## 模型流程：提取 → Blender → 导出 → 游戏内核对

把游戏里的角色变成可编辑的 LoyalTools 骨骼合并工程，改完再导出成模组并在游戏里核对。分组、骨骼合并、LOD 匹配和 INI／BUF 生成全部由 LoyalTools 完成；本工具采集数据、调用它的入口、检查结果。默认使用所选 Blender 里已安装的那份插件，所以更新插件后这里自动用新版，不需要改 MCP。

| 步骤 | 工具 | 说明 |
|---|---|---|
| 看画面里有哪些角色 | `efmi_list_models` | 采一帧，列出 LoyalTools 识别出的对象、组件和截图 |
| 提取 | `efmi_extract_model` | 三次独立采样（几何必须一致、骨组来源必须一致），生成 `workplace`，后台 Blender 导入为 `<项目>.blend`，材质只连漫射贴图 |
| LOD 映射 | `efmi_map_lod` | `json`：复用 JSON 里已有的映射；`frame`：用帧分析目录或探针归档做帧匹配；`capture`：现采一帧 LOD 状态再匹配 |
| 编辑 | （你在 Blender 里改并保存） | 导出读取的是磁盘上保存的 `.blend` |
| 导出 | `efmi_export_mod` | 后台 Blender 运行插件自己的“生成 Mod”，产物在 `exports/<export_id>/mod`，并检查文件引用、尺寸、绘制范围和入口 |
| 部署并核对 | `efmi_verify_exported_mod` | 把这次导出复制到 `Mods\EFMI-MCP\<项目>`，游戏内 F10 一次，再把 XXMI 实际绘制的缓冲与导出文件按内容比较 |
| 查看项目／任务 | `efmi_model_project`、`efmi_model_job` | 项目现状；跟进或取消耗时任务 |

要点：

- **项目目录就是 LoyalTools 的自定义工作空间根目录**，目录名即模组名（插件用它给蓝图和 INI 命名）。布局见 `docs/model-extraction-export-plan.md` 第 9 节。
- **被模组替换的角色提取不了**：游戏对它的原始绘制没有发出。先停用该模组并刷新。
- **主控与 LOD 共用一份编辑网格**。LOD 入口、LOD 布局的缓冲变体和骨索引重映射由插件在导出时按 `workplace/EFMI_MergedSkeleton.json` 里的映射生成；没有采集过、JSON 里也没有的 LOD 不会被推算出来。该角色以前做过 LOD 映射时，提取时用 `lod_json` 指向旧项目即可，不用再采远景帧。
- **Blender 里只连漫射贴图**（Base Color，以及插件原生连接的同图 Alpha）。法线等其他贴图仍留在工作空间里供导出使用。
- **部署只写一个目录**：`<EFMI>\Mods` 下由本工具创建并带 `efmi-mcp-deployment.json` 标记的那个。别的模组不会被移动或停用；引用相同哈希的会列为可能的冲突。`action="remove"` 删除已部署的文件并刷新。
- **结果分开报告**：`export_valid`、`deployment_verified`、刷新（已请求／已确认）、`fresh_capture`、`target_mod_status`（`passed`／`failed`／`inconclusive`／`not_run`）以及覆盖与未覆盖的组件。刷新成功不等于这个模组生效；缓冲一致也不等于外观正确，报告里附前后截图。
- 耗时步骤以任务运行：在 `wait_s` 内完成就直接返回结果，否则返回任务 ID。

**验证范围（2026-10-07）。** 不需要游戏的部分已用真实的 LoyalTools 1.6.8（Blender 4.5.7 中已安装的插件）验证：仅漫射的 Blender 导入、LOD 的 JSON 匹配与帧匹配（两侧为帧分析目录）、插件原生导出及产物检查、在 Blender 里移动全部网格并保存后再次导出（只有顶点缓冲随之整体平移同样的距离，索引缓冲和其余文件不变）；部署规则、绘制对照、任务管理以及“用哪个 Blender、哪份插件”的选择有 37 项单元测试。需要游戏的部分尚未实机验证：MCP 工具形式的三采样提取、以探针归档作为一侧的 LOD 帧匹配、部署→F10→逐组件核对，以及修改后的模型在游戏里的表现。在这些完成之前，不能把整条流程当作已在游戏里跑通。

设计、LOD 匹配的原理和实施记录见 `docs/model-extraction-export-plan.md`。

## 已知限制

### 合并骨骼工作空间提取（命令行）

上面的 `efmi_extract_model` 把下面三步合在一起；脚本仍可单独使用。导入脚本默认只连漫射贴图，`--materials native` 恢复插件带法线的材质。

可把实时采集的数据交给本机 LoyalTools 原算法，生成其 `workplace` 工作空间，再用后台 Blender 导入并保存 `.blend`。默认只读引用 Blender 中已安装的 LoyalTools 插件（`--blender` 指定用哪个 Blender 的，`--loyaltools-root` 改用别处的源码），不修改 EFMI 或参考工具。此流程当前针对游戏原始 `DrawIndexedInstanced` 角色绘制；先在画面中显示目标角色。

```powershell
& 'D:\Python312\python.exe' scripts/capture_merged_source.py --pid <游戏PID> --archive captures/sample-a --deferred-skeleton
& 'D:\Python312\python.exe' scripts/capture_merged_source.py --pid <游戏PID> --archive captures/sample-b --deferred-skeleton
& 'D:\Python312\python.exe' scripts/capture_merged_source.py --pid <游戏PID> --archive captures/sample-c --deferred-skeleton
& 'D:\Python312\python.exe' scripts/export_merged_workspace.py captures/sample-a 'E:\输出目录\workplace' --validation-archive captures/sample-b --validation-archive captures/sample-c
& 'D:\blender4,5\blender.exe' --background --factory-startup --python-exit-code 1 --python scripts/verify_merged_workspace_blender.py -- --workspace 'E:\输出目录' --output 'E:\输出目录\角色.blend'
```

静态几何要求完整资源自捕获后未变化，按真实索引范围提取，不将 XXMI 的 VB 哈希窗口当成完整网格。CB 使用 draw 时刻快照；骨骼 SRV 是后续读回，因此上述静态编辑模式用三份独立采样核对完整几何与合并骨组映射，并保留读取时刻证据。它不保证复现某一帧的动画姿态，也不自动生成未捕获的 LOD 映射。输出拒绝覆盖已有工作空间或 `.blend`。

### 探针边界

- 探针必须在游戏创建 D3D 设备之前加载，所以只能由启动器在开局注入，不能附加到已运行的游戏。
- 0.1.1 固定使用首个发现的交换链所属设备，后建的无交换链辅助设备不会抢走采集目标。不做多个窗口/交换链间的自动主画面识别；完整设备重建后需重启游戏，同设备交换链销毁/重建期间的跨界抓帧也尚未验证。
- 看到的是真正发给显卡的调用。某个 mod 生效时，游戏对该网格的原始绘制已被替换，不会出现；出现的是 mod 发出的绘制，来源标为 `xxmi`。来源是按调用栈深度判断的启发式结果。
- 缓冲和贴图的读取返回"读取那一刻"的内容。要某个 draw 当时的常量缓冲，抓取时加 `cb_data=true`。
- 由 GPU 写入的缓冲（计算着色器蒙皮等），XXMI 自己缓存的哈希可能是旧值；探针给的是当前字节的哈希，两者可能不同。
- 只有最近一次抓取的资源可读；延迟上下文（deferred context）里的绘制看不到，`efmi_status` 会提示是否出现过。
- 多重采样贴图和三维贴图暂不支持读回。
- XXMI 的 `shader_hash = bytecode` 模式未实现（EFMI 默认是 `3dmigoto`）。

## 性能

2026-10-06 在当前梨诺画面，用同一游戏进程交错运行修改前/后各三次完整归档，耗时中位数 **1.843 秒 → 1.220 秒（减少 33.8%）**。每次仍含两次完整绑定/CB 捕获、三个完整缓冲 41,967,616 字节、24 张 DDS 45,616,608 字节及全分辨率截图；86 个几何区域校验全部匹配。此为采集及写盘时间，不含后续模型转换和 Blender 导入，不能当作所有场景的固定延迟。

优化包括本机 CRC32C、紧凑 JSON、PNG 快速无损压缩及完整图/预览共享解码；身份复核帧保留 IB 哈希与 CB，省去不参与该复核的 VB 哈希。三采样和完整几何校验仍保留。实测 16 MiB CRC32C 从约 1.75 秒降到 1.90 毫秒；2560×1600 PNG 编码从约 419 降到 176 毫秒，像素不变，PNG 文件增大（本次样本约 52%，DDS 不变）。归档 `readback.json` 的 `durations_ms` 记录各阶段耗时，包含读回与写盘的阶段已明确命名。

离线合并骨骼导出复用已验证的缓冲与构建结果，三采样源BUF读取由1134次降为9次；另一组独立三轮对照中位1.890→1.662秒（减少12.1%）。输出文件逐一比对一致（仅忽略报告输出路径/日志耗时），完整缓冲缓存峰值约80MiB并在返回时清空。可运行 `scripts/test_loyal_bridge_performance.py` 对已有归档复测；CRC和图像等价性检查分别为 `scripts/test_crc32c.py`、`sandbox/decode_test.py`。

以下是历史沙盒探针开销实测，每帧约 4000 次绘制、XXMI 同时在场：

- 探针已加载但无人连接、或已连接但不采集：帧率与不加载探针相比测不出差别。
- 连续模式：探针内部计时每帧约 0.6 毫秒。
- 请求一次完整抓取：被记录的那一帧多花约 9 毫秒。
- 从显存读回数据是异步的，渲染线程不等待 GPU。

两次运行之间帧率本身有约 1.5 毫秒的波动，所以这组沙盒数据以探针内部计时为准。真实游戏的渲染帧开销未测；本轮测量的是游戏外采集流程的总耗时。

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

## 项目记忆

跨会话继续开发时，先读 `.ai-memory/CURRENT_STATE.md` 和 `.ai-memory/TASKS.md`。七份记忆文档记录概览、架构、当前状态、技术决策、已知问题、任务和会话摘要，维护约定见根目录 `AGENTS.md`。
