# EFMI MCP — 项目概览

> 建档：2026-10-05。结构参考 `E:\loyal tools\.ai-memory`，内容仅记录本项目。

## 目标

为 agent 提供《明日方舟：终末地》运行时的 D3D11 绘制数据，减少依赖手动 EFMI/XXMI FrameAnalysis dump。关注角色所用的 IB/VB、着色器、贴图、常量缓冲及 XXMI 兼容哈希。

## 组成与技术栈

- `probe/`：C++17、MSVC x64 探针，构建为 `dist/efmi_probe.dll`，由启动器在游戏创建设备前注入。
- `server/`：Python stdio MCP 服务，通过 Windows 命名管道连接探针。基础功能使用标准库，贴图预览可使用 Pillow/numpy。
- `sandbox/`：D3D11 测试程序和回归脚本，在项目内复制一份 XXMI 运行时做兼容验证。
- `captures/`：按需导出的网格、贴图、着色器等，不是持续帧日志目录。

## 当前功能

状态查询、单帧完整捕获、绘制调用过滤、哈希查找、IB/VB/CB 读取、贴图读取、着色器反汇编、网格导出、连续帧摘要、捕获差异比较、只读扫描 mod 与离线抓帧比对。

`efmi_reload_and_capture`（探针 0.1.2 / 服务 0.1.4）：核对游戏/安装/默认双 F10 绑定，停止 live 并失效旧捕获，由探针在游戏进程内按一次 F10（必要时取前台并还回），等待后采集新帧，并以“XXMI 在按键后重建了新帧所用的资源/着色器”作为重载证据给出 `reload_confirmed`。无需狩猎模式。旧探针下退回外部按键并提示重启游戏。

服务 0.1.5 接入模型流程：`efmi_list_models`／`efmi_extract_model`／`efmi_map_lod`／`efmi_export_mod`／`efmi_verify_exported_mod`／`efmi_model_project`／`efmi_model_job`。分组、骨骼合并、LOD 匹配与 INI／BUF 生成由 Blender 中已安装的 LoyalTools 插件完成，本项目只调用其入口并检查结果。离线部分已验证，实时提取与部署核对待实机验证（T-008、D-014）。

0.1.3 的实机失败原因是 UIPI（游戏以管理员运行，丢弃普通权限进程的按键）。探针按键的新路径沙盒与实机均已验证（2026-10-06，PID43816，游戏在前台和在后台各一次），详见 BUG-004 / T-007 / D-012。普通采集及 IB/VB 读回正常。

`0.1.1` 已修复辅助设备覆盖采集目标与 live 会话在途帧回填，本地整套回归通过，新 DLL 已就位；新版真实游戏伊冯画面的捕获与资源读回也已验证，详见 `CURRENT_STATE.md` / T-003。

## 本机入口

- 项目：`F:\EFMI MCP`；服务入口：`server/run_server.py`。
- README 配置的 Python：`D:\Python312\python.exe`。
- 服务默认 EFMI 目录：`D:\XXMI-Launcher-Portable-v2.1.6\EFMI`，支持 `--efmi-root` / `EFMI_ROOT` 覆盖，工具不直接写入其文件。
- 探针日志：`dist/efmi_probe.log`。服务版本为 `0.1.4`，磁盘上的探针为 `0.1.2`、协议号 `1`（2026-10-06 晚运行中的游戏仍加载 `0.1.1`）；历史真实游戏 PID `38252` 曾确认报告 `0.1.1`，此前 `0.1.0` 的空捕获仅是历史故障证据。
- 源码仓库：`https://github.com/loyal37/EFMI-MCP`。用户 2026-10-06 要求此后验证完成后提交并推送，详见 `AGENTS.md` / D-007。

## 长期约束

只改我们的工具，不修改 EFMI/XXMI、本机启动器配置、现有 mod 或游戏文件。读取真实运行数据和只读查看 EFMI 文件属于本项目用途；沙盒写入只发生在项目私有目录。注入、连接、非空捕获、角色数据可用分别验证，不能互相替代。

已授权的 F10 自动重载不直接改原版文件，但 XXMI 原有行为可能保存持久变量，不能把它称为纯只读操作。普通最新画面采集不需要 F10。
