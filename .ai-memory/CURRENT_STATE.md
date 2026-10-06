# EFMI MCP — 当前开发状态

> 最后更新：2026-10-06。0.1.1 实机角色/Mod验证完成；梨诺合并骨骼工作空间和可编辑Blender工程已交付。

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
