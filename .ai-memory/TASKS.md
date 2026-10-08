# EFMI MCP — 任务列表

> 最后更新：2026-10-08。

## 当前状态

T-001 至 T-007 已完成。T-007 的原因是 UIPI；探针 0.1.2 / 服务 0.1.4 改为进程内按键，沙盒与实机（游戏在前台、在后台各一次）均已验证。每次改完并完成相关验证后提交并推送指定仓库（见 `AGENTS.md` / D-007）。

### T-012：把 RabbitFX 形式的 mod 改成槽位形式（TangTang 已按用户要求改完并实机核对；只剩 LOD 距离没看，2026-10-08）

- **TangTang 已改**（CURRENT_STATE 首节）：原 INI 备份在 mod 目录的 `tangtang.ini.rabbitfx-backup-20261008`。取槽位的办法比下面设想的更直接——读 RabbitFX 改写后的着色器里的回退槽位，不需要关 mod、不需要提取。
- 画面对比和“留在槽位上的 mod 贴图是否影响别的绘制”已补做（CURRENT_STATE 首节）：主着色结果看不出差别，没有发现别的绘制采到 TangTang 的贴图。
- **待补（需要用户把 TangTang 放到 LOD 距离，不按键）**：看 LOD 绘制绑定的像素着色器是否仍是近景那几个（`47868ca9…`、`2d0b12a7…` 等），不是的话用 `efmi_get_shader` 读它的回退槽位，槽位不同就得给槽位行加 `$lod_level` 条件。
- 用户若觉得组件 2 的描边或 G-buffer 有差别要补：得按遍加条件（例如给描边遍的顶点着色器 `1041f0408b0e4fc3` 一个 `ShaderOverride` 的 `filter_index`，在绘制段里 `if vs == …` 再写 `ps-t13`），不能无条件写。

- 两种写法（只读查看所得）。槽位形式：组件的绘制段里直接写 `ps-t16 = Resource…DiffuseMap` 这类行（`Xaihi Venomous Vows 2.0`、`arcane fes 1.2`，LoyalTools 的导出也是这种）。RabbitFX 形式：写 `Resource\RabbitFx\Diffuse／LightMap／NormalMap = ref …` 再 `run = CommandList\RabbitFx\SetTextures`（`Yvonne Ensnared Heart 1.6`、`TangTang Seaside Breeze 1.4`、`perlica Ravenb 2.1`、`laevatain lune fluer 1.4`、`Liino Honmei Knit 1.6`）。
- RabbitFX（`Mods\RabbitFX -ENDMI-\RabbitFX.ini`，2.7）做的事：用 ShaderRegex 改写游戏的像素着色器，加上 t70–t74（漫射、光照图、法线、裁剪图、雨水图）和 t60／t61（发光、FX），着色器里先看 t70 等是否绑了东西，绑了就采它，否则采原来的槽位。它既改主着色遍（Main），也改 G-buffer 遍的各种变体（它叫 Shadow，特征是 o0–o4 五个输出、贴图在 t0–t2）。另外还包住了游戏原有的 discard（`$censor`）。
- 同一张贴图在不同遍里的槽位不同，同一遍里不同组件也不同。伊冯 `d160fb72`（10-07 提取的工程，`workplace\<组件>\TYPE_GPU-EFMI\TextureSlots.json` 按绘制逐个记录）：漫射在 G-buffer 遍是 `ps-t0`、主着色遍是 `ps-t16`、描边遍是 `ps-t13`；法线在 G-buffer 遍 `ps-t1`、主着色遍 `ps-t18`；光照图在主着色遍 `ps-t17`。槽位形式的 mod 和 LoyalTools 的导出用的都是主着色遍的槽位。
- 所以槽位表拿得到：`efmi_extract_model` 产出的工程里每个组件都有 `TextureSlots.json`；现场用 `efmi_get_draw` 也能逐遍看。2026-10-08 上午实机上 `arcane` 组件 2 的 INI 写的 `ps-t13`（漫射）／`ps-t14`（法线）与抓到的绑定一致；同一个绘制的 G-buffer 遍里 `ps-t0`／`ps-t1` 仍是游戏自己的贴图，`ps-t12`、`ps-t15–17`、`ps-t21` 是别的组件留下的旧绑定——“绑着”不等于“着色器在用”，要以着色器声明的槽位为准。
- 伊冯的槽位表已有（主着色遍）：`d160fb72`、`c0bb7cd6`、`06e91fc9` 是 16／17／18（漫射／光照图／法线），`7091ba4f` 是 13／14（漫射／法线）。其余四个角色要各提取一次（角色在画面里、mod 关着）。
- 改成槽位之后与 RabbitFX 不等价的地方：① G-buffer 遍不再用 mod 的漫射和法线（现有的槽位形式 mod 就是这样；要补就得按遍加条件，不能无条件写 `ps-t0`，主着色遍和描边遍的 t0／t1 是别的资源）；② `Liino` 用了 `Resource\RabbitFX\FXMap` 和 `Commandlist\RabbitFX\Run`（裁剪／发光），槽位形式表达不了，只能留着 RabbitFX 或放弃这个效果；③ 槽位随游戏着色器版本变，游戏更新后可能要重查；LOD 距离下着色器用的槽位还没有专门核对过。
- 做法（若用户要求）：转换结果写成副本，不改用户原来的 mod（`AGENTS.md`）；按组件把 RabbitFX 的三类行换成对应槽位行，`= null` 的（沿用游戏贴图）不写，去掉 `SetTextures`；部署到专用目录后 F10，逐遍比较绑定，并用 `efmi_capture_draw_outputs` 比较转换前后各遍画出的像素。
- 用户的 Mods 目录里有反方向的工具 `SlotToRabbitFX.exe` 和它 9 月的转换报告（按资源名猜类型，掩码类贴图判断不了）。

### T-011：采集数据的三项完善（已完成：探针 0.1.5／服务 0.1.10，2026-10-08 实机核对通过；重载等待的新标准待实机确认）

- 做了清单里的 1、4、5，结果与验证范围见 CURRENT_STATE 首节。
- 实机核对已做（CURRENT_STATE 前两节）：管线状态和渲染目标拷贝通过。重载工具：第二次按键发现返回的帧里 mod 只恢复了一半，已把等待标准改成“mod 画的量不再增长”（服务 0.1.10，单元测试和沙盒真实按键通过）。
- 新标准已在实机跑过一次（2026-10-08 15:36，默认 `settle_ms`）：`frames_looked_at=3`、`waited_for_xxmi_ms=359`、`mods_settled=true`，返回帧的 XXMI 绘制数与重载前相同（271）。“第一帧只恢复一部分”的情形（`settle_ms=250`）没有再专门跑，哪天顺带做一次即可。带渲染目标拷贝的那一帧的游戏内耗时也有了：7.37 毫秒（16 份拷贝，94.6 MiB），与不带拷贝时基本相同。
- 可以再做的（未要求）：深度目标按 `box` 拷贝（先整块拷到一张同族格式的普通纹理，再取一块到暂存纹理），省掉 `depth=true` 时阴影图的几十 MiB。
- 清单里没做的（用户未要求，按需再议）：
  - 2：`efmi_status` 提示“运行中的服务比磁盘上的代码旧”（仅服务端，很小）。
  - 3：绘制当时快照被游戏重写的顶点／索引范围（现在只有常量缓冲和间接参数是绘制当时的内容；动态合批、UI、粒子的索引读不到）。
  - 6：同时保留两次抓取可读（做开／关 mod 对比时不必抢在下一次抓取前读完）。
  - 低优先：某个哈希一出现就抓；取前台被拒时多等一会儿（不是重按）；第二个只读连接。

### T-010：探针区域哈希缓存给出已销毁缓冲的哈希（已完成：沙盒复现并修正，探针 0.1.4 实机核对通过，2026-10-07）

- 结果见 CURRENT_STATE 首节和 BUG-005。游戏重启后已确认状态为 0.1.4、带 `region_hash_per_object`，两次重载前后哈希与内容相符。当日 17:39 按用户的要求做了“内容改变后重载”的实机实验：一块新缓冲建在此前另一块同样大小缓冲的地址上、读取范围相同、内容不同，记录的哈希是新内容的（CURRENT_STATE 首节）。无剩余事项。以下为当时的记录。

- 现象与依据见 CURRENT_STATE 首节末尾。`probe/src/capture.cpp` 的 `g_rcache` 以 `{缓冲地址, 偏移, 大小}` 为键、以写入计数和 `gen` 判有效，`reg_add_resource` 在地址复用时直接替换登记项，新旧缓冲的写入计数都可能是初始值。
- 做法：先在沙盒复现（给某个 draw 的 vb0 换成文件缓冲，重载；把文件改成同样大小的另一份内容，再重载，比较记录的区域哈希），再让缓存值带上资源序号 `serial` 并在不符时作废；重建、跑沙盒全套、装入 `dist`（游戏运行时用改名法），下次启动游戏生效。
- 服务端已有的缓解：部署核对对哈希不符的顶点缓冲按内容裁决。索引缓冲哈希不符仍会被报为“旧部署或别的 mod”。

### T-009：agent 用 F11 开关全部 mod（已完成，实机通过，2026-10-07）

- 实机结果与随后的“不必要时不按键”改进见 CURRENT_STATE 首节。以下为实现时的记录。

- 探针 0.1.3 的 `mods_key` 与服务 0.1.6 的 `efmi_toggle_mods`；实现与验证范围见 CURRENT_STATE 首节、D-015。
- 待办（需要用户重启游戏加载 0.1.3，并重启 MCP 服务）：画面里有被 mod 替换的角色时，`efmi_capture_frame` → `efmi_toggle_mods state=off`，核对 `mods_enabled=false`、该角色的原始绘制以 `game` 来源出现、XXMI 的替换绘制消失 → `efmi_diff_captures` → `state=on` 核对恢复；再试一次已开时的 `on`（两次按键）和关闭后 F10 重载恢复开启。顺带在关闭状态下做 T-008 的实机提取。
- 结束对比后必须恢复为开启。

### T-008：模型提取、LoyalTools 原生导出与游戏绘制验收（已完成：主流程、修改后的模型、LOD 距离均已实机通过，2026-10-07）

- 2026-10-07 实机：伊冯在 mod 关闭状态下提取到 `E:\Endfield mods\yvonne merge`，JSON LOD、Blender 导入、导出、部署→F10→逐组件核对均通过（CURRENT_STATE 首节）。
- 2026-10-07 17:39 实机：在工作空间副本上用 Blender 移动全部顶点后导出，部署重载后 40 个缓冲与修改后的导出逐字节相同（CURRENT_STATE 首节）。
- 2026-10-07 晚实机：LOD1 距离下 `efmi_map_lod method=capture` 和 LOD 入口的部署核对通过，期间修了大世界画面归档的问题（服务 0.1.7，CURRENT_STATE 首节）。
- 两处 LOD 映射差异已查清（CURRENT_STATE 首节）：5 项骨索引重映射的两种选择效果相同（顶点相距不到 1 厘米）；CPU 姿态组件的 LOD 条目是按 JSON 复用时被 LoyalTools 的检查去掉的，用户的旧工程里有。都与用户已安装的 mod 无关。用户答复：CPU 组件从不修改，都是直接删掉、用游戏原来的，所以这一条缺失没有影响，不补。
- 可选的后续（不阻塞）：比 LOD1 更远的级别；在大世界里实机跑一次三采样提取；手工修改的模型的观感。
- 剩余：① ~~角色处于 LOD 距离时核对 LOD 入口和缓冲变体，以及 `efmi_map_lod method=capture`~~（已做）；② ~~改动模型后导出并在游戏里核对~~（已做；改动是脚本做的整体平移，手工修改的观感留待用户实际做模型时再看）；③ ~~游戏关闭后重跑 `sandbox/run_all.py`~~（已随 T-010 补跑通过，游戏运行时即可跑）。

- 服务 0.1.5 提供七个模型工具，M1–M5 的代码已就绪；按计划书的完成标志衡量，只有 M2（仅漫射的 Blender 工程）已达到；M3 的离线往返已做（未修改、移动顶点后各导出一次），其余都还差实机验证。实现方式与验证范围见 CURRENT_STATE 首节和计划书第 13 节。
- 不需要游戏的部分已用真实的 LoyalTools 1.6.8（Blender 4.5.7 中已安装的插件）验证：仅漫射的 Blender 导入、LOD 的 JSON 匹配与帧匹配（两侧为帧分析目录）、插件原生导出及产物检查；部署规则、绘制对照、任务管理以及“用哪个 Blender、哪份插件”的选择有 37 项单元测试。
- 待办（需要游戏运行、目标角色在画面中且未被模组替换）：`efmi_list_models` → `efmi_extract_model`（可带 `lod_json`）→ 不改动直接 `efmi_export_mod` → `efmi_verify_exported_mod`，核对三采样、导入、导出、部署一致、刷新证据和逐组件结果；再把角色放到 LOD 距离验证 `method=capture` 与 LOD 入口；最后做一次改动后的往返。实机通过前本任务不算完成。
- 可选后续：把探针数据写成帧分析目录格式，去掉归档版提取器对 LoyalTools 内部方法的依赖。

以下为计划阶段的记录：

- 用户确定完整流程：提取 → 工作空间／Blender 编辑 → LoyalTools 原生 INI／BUF 导出 → 指定测试目录部署 → 自动 F10 → 目标模组绘制与资源验证。
- Blender 材质只连接漫射；游戏需要的其他 DDS／槽位元数据保留。导出复用原生完整入口，包含前后处理，不能仅依赖 `FINISHED`。
- 计划见 `docs/model-extraction-export-plan.md`，M1–M6 全部待实施。重点覆盖主控／LOD 共用网格、产物身份、实际运行时缓冲对应与未覆盖状态报告。
- 本轮仅写计划和项目记忆，未开始实现或游戏操作。

### T-007：agent 自动 F10 重载后获取新帧（已完成，实机通过，2026-10-06 晚）

原因已定位：游戏以管理员运行，UIPI 丢弃普通权限的 MCP 服务发来的按键。探针 0.1.2 改为在游戏进程内按 F10（`reload_key`），服务 0.1.4 优先走该路径并给出重载证据（`reload_confirmed` 不再恒为 false）。沙盒全套与真实 F10 集成通过，新 DLL 已安装；详见 CURRENT_STATE 首节。

实机：PID 43816 报告探针 0.1.2、进程 elevated（完整性 0x3000）。用户移出伊冯 mod 后由工具重载：按键在进程内发出并被读到，持续 1 帧（21696→21697），重载那一帧耗时 92.7ms（平时约 8ms）；重载前 367 次绘制中 XXMI 120 次（93 DrawIndexed + 27 Dispatch），重载后 277 次全部来自游戏，XXMI 的 93 次/18 个网格消失，游戏原始的 30 次/6 个网格出现；`reload_confirmed=true`（XXMI 在按键后创建了新帧所用的 27 个着色器）。游戏继续运行。证据 `captures/verify-reload-inprocess-43816/`。

同日 20:51 第二次实机，游戏在后台（前台是 Claude 窗口）：探针以 `direct` 方式取得前台，按键持续 1 帧（91844→91845，重载帧 79.8ms），随后把前台还给原窗口（前后为同一 hwnd）；移出的伊冯 mod 同样消失（XXMI 120→0 次绘制，游戏原始 30 次绘制出现），`reload_confirmed=true`。`attach_input` 这条备用取前台路径在实机上没有用到，只在沙盒验证。无遗留验证项。本轮改动已按 D-007 提交并推送，提交号以 git log 为准。

以下为 0.1.3 及之前的记录：

PID41976两次普通VK请求均获API接受并采集到新帧，但用户移出Yvonne后仍显示模组，明确确认未刷新；中间一次focus_refused未发键，不计有效输入。扫描码诊断也未生效：down/up返回1、错误码0，却始终未观察到F10异步按下状态；帧160361→160664的XXMI IB及次数相同。证据见 `captures/verify-reload-scancode-41976-ready/assessment.json` 及CURRENT_STATE。具体原因未知，扫描码不是修复。

MCP服务0.1.3增加 `key_down_observed`：按住期间未观察到高位就返回 `keydown_not_observed`，finally释放，不开始capture或重发。`key_down_sent`仍表示API接受，`reload_confirmed=false`仍无回执。默认双F10绑定、前台/PID/device验证、旧缓存失效、成功输入后的等待/新帧规则保留。探针仍0.1.1，只需重启服务，无需狩猎模式或重注入。

本轮输入21项+工作流32项、MCP stdio私有沙盒39项及compileall通过；完成的是失败检测。历史0.1.2真实F10私有沙盒通过（`sandbox/build/reload-xuxnsq73/reload-evidence.json`），本轮游戏运行时未重跑，不能代替当前实机失败。下一步定位正常输入为何不可见；不自动提权、改EFMI或再次发送输入。普通capture继续可用，当前目标不能标为全部完成。

### T-006：优化实际数据读取速度（已完成，2026-10-06）

实机同场景三轮交错对照，完整归档中位1.843→1.220秒（减少33.8%），每轮86几何校验全通过；本机CRC32C、PNG编码/共享解码、紧凑JSON、身份复核省去无用VB哈希、离线骨骼提取复用已验buffer均完成。新采集三帧通过合并骨骼导出；selftest83/MCP39、CRC8组/解码6组通过。只改游戏外工具，无需重新注入；启动新Python即可使用，已运行MCP服务重启才载入加速库。证据和边界见CURRENT_STATE。

### T-004：启用Mod后的实际绘制验证（已完成）

伊冯Mod的93次替换绘制、四组完整IB/VB与材质绑定已确认；16缓冲与2K贴图均和源文件逐字节一致。证据在 `captures/verify-mod-38252/`，详情见当前状态。

## 已完成实机验证

### T-003：用 `0.1.1` 采集当前角色数据（2026-10-05）

- 实际 PID `38252` 报告 `0.1.1`；主设备保持稳定，捕获帧 `9113` / `14377` 均非空。
- backbuffer 截图确认伊冯；帧 `9113` 的 `6` 个角色 IB、`30` draw 命中 Yvonne INI 的 hash/index_count。
- 三组角色完整 IB/VB0/1/2 共 `12` 个缓冲读回 CRC 全匹配；draw 时刻 CB、`5` 张纹理、VS/PS 字节码及反汇编均已读取，连续采集也非空。
- 证据目录：`captures/verify-game-38252/`；详细计数和边界见 `CURRENT_STATE.md`。未修改 EFMI 或结束游戏，验证后已停止 live 并释放资源。
- 本任务验证数据可获取，不代表已完成全角色网格/骨架重建、其他角色或 LOD 的验证。

## 已知未覆盖场景

多交换链自动识别主画面、跨设备替代交换链迁移、同设备交换链销毁/重建期间的跨界捕获完整性；详见 `KNOWN_ISSUES.md`，不得视为本轮 `192` 项已覆盖。

## 已完成

### T-001：工具内修复多设备空捕获（2026-10-05）

- `0.1.1` 分离挂钩与目标选择，由交换链绑定所属设备/立即上下文；辅助设备不再覆盖，诊断显示目标和 draw 计数。
- 补修 live generation 会话隔离；修正沙盒 shader 的浮点基准，保留严格哈希/字节断言。
- 最终整套通过：三种加载顺序各 `69` 项 hash 对照无差异且二次捕获一致；自测 `83`；多设备有/无 XXMI 各 `96`；MCP `39`；`git diff --check` / `compileall` 通过。
- `dist/efmi_probe.dll` 已就位，大小 `355840` 字节；SHA256 见 `CURRENT_STATE.md`。旧版保存在 `dist/backup-before-multidevice/`。
- EFMI 原目录 `56` 文件最终 SHA256 `changed=[]`。本地工具修复完成，随后 T-003 也通过本次伊冯画面实机验证。

### T-002：建立项目记忆体系（2026-10-05）

已参考 `E:\loyal tools\.ai-memory` 的七文件结构，创建本项目七份中文记忆文档与根 `AGENTS.md`。只读参考目录，未迁入 LoyalTools 项目细节；已记录最终实现、产物、通过结果与真实游戏验证范围。

### T-005：当前梨诺的合并骨骼提取（已完成，2026-10-06）

按原LoyalTools算法生成目标workplace及liimo merge.blend，18组件/79044顶点/94051三角，696组槽/512canonical来源。三独立采样、完整几何逐字节核对和Blender真实导入均通过；预览与说明随成品保存。EFMI/LoyalTools只读；静态编辑与材质边界见CURRENT_STATE。
