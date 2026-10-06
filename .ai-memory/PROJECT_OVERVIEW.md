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

MCP服务0.1.3的 `efmi_reload_and_capture` 核对游戏/安装/默认双F10绑定，停止live并失效旧捕获，只发送一次F10；按住期间异步状态至少一次可见才等待并采集，否则返回 `keydown_not_observed`。`key_down_sent`与 `key_down_observed`分开，`reload_confirmed=false`仍无完成回执。无需狩猎模式，更新只需重启服务。

当前真实游戏自动刷新测试失败：用户移出Yvonne后模组仍显示，扫描码诊断中SendInput接受但按下状态始终不可见。0.1.3仅改进失败检测，原因仍未定位；普通采集及IB/VB读回正常。历史私有沙盒通过不代表实机刷新可用，详见BUG-004 / T-007。

`0.1.1` 已修复辅助设备覆盖采集目标与 live 会话在途帧回填，本地整套回归通过，新 DLL 已就位；新版真实游戏伊冯画面的捕获与资源读回也已验证，详见 `CURRENT_STATE.md` / T-003。

## 本机入口

- 项目：`F:\EFMI MCP`；服务入口：`server/run_server.py`。
- README 配置的 Python：`D:\Python312\python.exe`。
- 服务默认 EFMI 目录：`D:\XXMI-Launcher-Portable-v2.1.6\EFMI`，支持 `--efmi-root` / `EFMI_ROOT` 覆盖，工具不直接写入其文件。
- 探针日志：`dist/efmi_probe.log`。服务版本为 `0.1.3`，探针仍为 `0.1.1`、协议号 `1`；历史真实游戏 PID `38252` 曾确认报告 `0.1.1`，此前 `0.1.0` 的空捕获仅是历史故障证据。
- 源码仓库：`https://github.com/loyal37/EFMI-MCP`。用户 2026-10-06 要求此后验证完成后提交并推送，详见 `AGENTS.md` / D-007。

## 长期约束

只改我们的工具，不修改 EFMI/XXMI、本机启动器配置、现有 mod 或游戏文件。读取真实运行数据和只读查看 EFMI 文件属于本项目用途；沙盒写入只发生在项目私有目录。注入、连接、非空捕获、角色数据可用分别验证，不能互相替代。

已授权的 F10 自动重载不直接改原版文件，但 XXMI 原有行为可能保存持久变量，不能把它称为纯只读操作。普通最新画面采集不需要 F10。
