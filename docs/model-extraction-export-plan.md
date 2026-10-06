# EFMI MCP 模型提取、LoyalTools 导出与游戏验证计划

日期：2026-10-06

状态（2026-10-07）：M1–M5 的代码已实现，MCP 服务 0.1.5。不需要游戏的部分已用真实的 LoyalTools 1.6.8 与 Blender 4.5.7 验证：Blender 导入（仅漫射）、LOD 的 JSON 匹配与帧匹配、原生导出及产物检查、部署规则和绘制对照逻辑。需要游戏运行的部分——实时三采样提取、用探针归档做 LOD 帧匹配、部署→刷新→逐组件核对——尚未实机验证，在此之前不能视为闭环已通过。实施记录与验证范围见第 13 节。

## 1. 目标与用户要求

将以下流程接入正式 MCP 工具：

**游戏采集 → LoyalTools 工作空间 → Blender 编辑 → LoyalTools 原生导出 → 部署本次产物 → 自动 F10 刷新 → 检查模组实际绘制。**

已确认的要求：

1. 提取沿用 LoyalTools 合并骨骼算法，保留后续编辑和导出所需的数据。
2. Blender 模型材质只连接漫射贴图。
3. 编辑后的模型导出为游戏模组 INI／BUF，严格使用 LoyalTools 原生导出流程。
4. 主控与 LOD 使用同一套编辑网格；入口、骨映射和必要的缓冲格式适配遵循 LoyalTools 的处理。
5. 导出后自动刷新游戏，并核对本次导出模组的实际绘制及资源，保存验证证据。



## 2. 已有基础与需要补齐的部分

| 环节 | 已有基础 | 本次需要补齐 |
|---|---|---|
| 实时采集 | `scripts/capture_merged_source.py` 可归档几何、骨骼资源、贴图和 CB 快照 | 正式 MCP 入口、目标选择、统一会话管理、连续三次采样与进度回报 |
| 合并骨骼提取 | `server/efmi_mcp/loyal_bridge.py` 调用 LoyalTools 原算法，已有三采样静态校验 | 可复用接口、明确角色身份、LOD 数据接入、完整产物清单 |
| Blender 导入 | `scripts/verify_merged_workspace_blender.py` 可导入、检查并保存 `.blend` | 漫射材质规则、素材来源记录、适合交给用户编辑的工程 |
| 编辑后导出 | LoyalTools 已有完整蓝图导出入口 | 后台 Blender 包装、导出前检查、真实成功判定、文件清单 |
| 自动刷新 | 探针 0.1.2／服务 0.1.4 的 `efmi_reload_and_capture` 已实机通过 | 与本次导出及部署记录串联，检查目标模组是否实际生效 |
| 绘制检查 | 已能查询 draw、读取缓冲／纹理、比较捕获 | 将实际绘制对应到本次导出文件，输出组件级报告 |

现有桥接主要支持游戏原始角色的 `DrawIndexedInstanced`。模组开启后被替换掉的原始绘制可能无法获得；必须检查来源和完整性，不能将模组替换网格自动视为原始提取输入。

现有骨骼 SRV 为稍后读回，三采样验证用于静态编辑和骨组来源一致性，不保证复现指定帧姿态。本计划不以实现绘制时刻骨骼快照作为前置条件。

## 2.1 接入原则：调用 LoyalTools，而不是复制它

用户要求（2026-10-07）：以接入为主，LoyalTools 更新后 MCP 不应跟着改。据此：

- **只用一份 LoyalTools**：默认使用所选 Blender 里已安装的插件目录，服务进程里的步骤（提取、LOD 匹配）和 Blender 里的步骤（导入、导出）跑的是同一份代码。更新插件即更新全部步骤。只有显式设置 `--loyaltools-root`／`EFMI_LOYALTOOLS` 时才改用别处的源码；这种覆盖只作用于服务进程里的步骤，Blender 里的导入和导出始终用该 Blender 自己安装的插件，此时两处可能是不同版本。每个结果都写明实际使用的路径、版本和来源；`efmi_extract_model` 指定了别的 `blender_path` 时，服务进程里的步骤也改用那个 Blender 的插件。
- **只调用入口，不复制算法**：分组、骨骼合并、LOD 匹配、INI／BUF 生成都由 LoyalTools 执行，MCP 不保存这些逻辑的副本。
- **检查只依据稳定的东西**：导出成败看产物（INI 引用的文件、尺寸、索引范围）；游戏内核对按缓冲内容比较，并沿 INI 自身的 `run =`／`= ref` 引用找到每个入口对应的文件，不解析插件的段落命名。插件日志里的失败语句会被采纳，但日志换了措辞不会把好的导出判成失败。

MCP 依赖的全部 LoyalTools 入口：

| 步骤 | 入口 | 运行位置 |
|---|---|---|
| 识别角色、生成工作空间 | `DumpWorkspaceExtractor` 的 `get_merged_skeleton_candidates`、`select_merged_skeleton_object`、`extract_merged_skeleton` | 服务进程 |
| LOD 帧匹配 | `extract.merged_skeleton_lod_mapper.map_merged_skeleton_lod` | 服务进程 |
| LOD JSON 匹配 | `extract.merged_skeleton_lod_mapper.restore_merged_skeleton_lod` | 服务进程 |
| 读取 profile | `common.efmi_merged_skeleton.load_profile` | 两处 |
| 导入工作空间 | 操作器 `loyal.import_workspace` 及其场景属性 | 后台 Blender |
| 导出模组 | 操作器 `ssmt.generate_mod_blueprint` 及其场景属性 | 后台 Blender |

这些入口改名或改参数时 MCP 才需要跟着改；它们内部怎么实现与 MCP 无关。

**仍然深入内部的一处**：探针归档不是帧分析目录，所以 `loyal_bridge.py` 里有一个归档版提取器，它继承 `DumpWorkspaceExtractor` 并替换了几个内部读取方法，用采集到的缓冲和输入布局代替帧分析的 `.buf`／`.txt` 文件。LoyalTools 如果重构这部分内部结构，这个适配器要跟着调整。彻底去掉这层依赖的办法是把探针数据直接写成帧分析目录的格式（`log.txt` 加按 XXMI 规则命名的文件），让 LoyalTools 像读普通帧分析一样读它，届时连界面上的按钮也能直接用。这一步尚未做，需要在游戏运行时与 XXMI 自己的帧分析逐项对照后才能采用。

## 3. MCP 接口设计

已实现的工具（服务 0.1.5）：

| 工具 | 输入重点 | 输出重点 |
|---|---|---|
| `efmi_list_models` | 无（采一帧） | LoyalTools 在该帧识别出的角色对象、各组件的 IB 哈希／索引数／顶点数、截图路径 |
| `efmi_extract_model` | 新的项目目录、可选对象名、可选 `lod_json`、材质模式、Blender 路径 | 三份采样、`workplace`、仅漫射的 `.blend`、三采样与导入检查报告 |
| `efmi_map_lod` | 项目目录、`method`（`json`／`frame`／`capture`）、来源 | LOD 对象、匹配／回退组件、预览工作空间、警告 |
| `efmi_export_mod` | 项目目录、可选蓝图 | `exports/<export_id>/mod` 原生产物、带 SHA256 的清单、文件检查结果 |
| `efmi_verify_exported_mod` | 项目目录、导出编号、`action`（`verify`／`check`／`status`／`remove`）、部署目录 | 部署记录、刷新结果、逐组件绘制与缓冲对照、截图 |
| `efmi_model_project` | 项目目录 | `.blend` 保存时间、当前 LOD 映射、历次导出、近期记录（不需要游戏） |
| `efmi_model_job` | 任务 ID、`status`／`wait`／`cancel` | 阶段、进度、结果或错误 |

提取与导出使用独立入口，agent 可将其串成连续流程；已有项目可以离线复用。导出读取磁盘上的 `.blend`，报告文件路径与摘要，未保存的 Blender 编辑不在读取范围内。

耗时操作以任务运行：调用在 `wait_s` 内完成就直接返回结果，否则返回任务 ID。同一时间只运行一个任务；任务占用探针期间，其他实时工具会被拒绝并提示任务 ID，避免中途的抓帧释放掉它正在读取的资源。后台 Blender 的输出写入日志文件，与 MCP 标准输出隔离。取消在步骤之间生效并结束正在运行的 Blender；提取失败时已采集的内容挪到 `<项目>.failed-<时间>` 留作诊断。

## 4. 阶段一：提取与工作空间

1. 检查探针、MCP、Blender、LoyalTools 版本及所需入口，记录实际使用路径和版本，避免源码目录与已安装插件版本不同而静默混用。
2. 列出当前帧的候选模型，使用组件和几何信息确定目标。多角色场景不单凭顶点数最大就选定目标。
3. 自动获取三份独立采样，核对角色身份、完整几何字节、骨组来源映射以及已有的有效性条件。采样期间角色或网格改变时给出具体失败原因。
4. 调用 LoyalTools 原生提取算法生成 `workplace`，保留 `Import.json`、`EFMI_MergedSkeleton.json`、组件元数据、原始缓冲和贴图槽位信息。
5. 保存来源清单：进程与探针版本、采样帧、组件、draw、资源、文件摘要，以及骨骼数据的实际读取时机。

完成标准：同一组有效采样可重复生成一致的几何和骨组映射；缺失资源、采样切换和错误输出路径不会被报告为成功。

## 5. 阶段二：主控与 LOD 数据接入

### 5.1 LOD 映射存在哪里、导出时怎么用

LoyalTools 的 LOD 映射存放在工作空间的 `workplace/EFMI_MergedSkeleton.json`。每个主控组件带一个 `lods` 数组，每项对应一个 LOD 对象（例如 `Character 36804`），记录该组件在 LOD 状态下的入口（`ib_hash`、`index_count`、`first_index`、`first_vertex`）、主控顶点组到 LOD 骨骼的对应 `vg_map`、与主控不同的顶点布局 `vb_formats`，以及是否为回退项 `is_fallback`。

Blender 里每个组件始终只有一份可编辑网格，主控与 LOD 共用它。导出时由 LoyalTools 按这份映射生成 `TextureOverride_EntryPoint_ComponentN_LODk` 入口、LOD 布局的缓冲变体（如 `-Texcoord-LOD1.buf`）和骨索引重映射（`-Blend-LOD1-BlendRemap.buf`）。这些是同一套编辑网格的格式适配，不是另一套网格。

### 5.2 两种匹配方式

映射有两种来源。两种都调用 LoyalTools 自己的函数，本工具不另写匹配算法。

**帧匹配**（`map_merged_skeleton_lod`，界面上的“帧分析匹配”）。需要两帧：提取主控时用的近景帧，和角色处于远距离、以 LOD 绘制的一帧。LoyalTools 从两帧各自重建角色对象，先按 IB 哈希、再按几何相似度（体素匹配，阈值 55%）配对组件，然后把主控组件的顶点组对应到 LOD 网格的骨骼，把结果写进 profile，并生成一份 LOD 预览工作空间（`workplace/LODPreview/<LOD 对象>`）。找不到独立 LOD 的组件写成回退项（沿用主控网格）并给出警告。

以前这两帧都是 XXMI 的帧分析目录。现在每一帧可以是下面任意一种，匹配算法不变：

- 帧分析目录（已有的 `FrameAnalysis-*`）；
- 探针归档：`efmi_extract_model` 留下的 `source/sample-1`，或角色处于 LOD 距离时现采的一帧。

也就是说，agent 直接采集的绘制数据与帧分析目录走同一套流程。实现上，LoyalTools 的匹配函数按目录打开两帧；桥接层在它打开目录的那一处，把探针归档换成归档版提取器、把帧分析目录交给原生提取器，其余代码原样执行。顶点组对应依据的是两份网格的几何与权重，不依赖某一帧的姿态，所以探针归档里稍后读回的骨骼数据不影响匹配。

**JSON 匹配**（`restore_merged_skeleton_lod`，LoyalTools 1.6.8 提交 `ea0f0de` 新增）。LOD 入口只取决于主控网格和 LOD 网格，所以已经写在 JSON 里的映射可以直接复用，不需要任何一帧。来源可以是：

- 当前工作空间自己的 profile；
- 重新提取主控之前 LoyalTools 留下的 `EFMI_MergedSkeleton_LODBackup.json`；
- 同一角色另一个工作空间的 profile，例如以前用帧分析匹配过的项目。

条目按 `unique_str` 配对，只有组件的绘制、蒙皮方式和本地顶点组数量都没变时才复用。对不上的组件按没有独立 LOD 处理并逐个报告，需要时再用帧匹配补上。

### 5.3 在 MCP 中的用法

| 场景 | 调用 |
|---|---|
| 该角色以前做过 LOD 映射（任何方式） | `efmi_extract_model(..., lod_json=<旧项目或其 JSON>)`，或事后 `efmi_map_lod(method="json", source=...)` |
| 第一次做，游戏在运行 | 近景提取后把角色放到 LOD 距离，`efmi_map_lod(method="capture")` 现采并匹配 |
| 手头有 LOD 状态的帧分析目录或归档 | `efmi_map_lod(method="frame", source=<目录>)` |

`method="capture"`／`"frame"` 默认以项目的 `source/sample-1` 作为主控帧；由 LoyalTools 界面建立的项目则用 profile 里记录的帧，也可以用 `full_source` 指定。主控帧必须与工作空间对应（组件按 IB 哈希核对），否则 LoyalTools 会拒绝。

### 5.4 边界

1. 只映射实际采集到或 JSON 里已有的 LOD；没出现过的层级不根据主控数据推算。
2. 相同 IB、不同绘制范围或不同布局的情况由原生匹配器按组件处理，本工具不以 IB 哈希单独配对。
3. 回退项、JSON 中对不上的组件、候选冲突都写进返回结果的 `warnings`，不静默略过。
4. 只有主控数据时，导出结果只包含主控入口；`efmi_export_mod` 的 `entry_point_coverage` 会分别列出主控和各 LOD 对象已生成入口的组件。
5. 完整的主控／LOD 验收要在游戏里分别让两种状态出现在画面上；`efmi_verify_exported_mod` 只对当时画面上的那一级给出结论，其余列入未覆盖。

完成标准：主控、独立 LOD 和共用 IB 的样例均能明确描述映射；有歧义的输入会被指出，已验证范围不会扩大到未采集状态。

## 6. 阶段三：Blender 工程与漫射材质

1. 在独立后台 Blender 进程中使用 LoyalTools 导入工作空间，保留组件标记、全局骨组、权重来源、UV、蓝图和 profile 关联。
2. 将当前包装脚本中的 `use_normal_map=True` 改为符合漫射要求的导入设置，并检查最终节点连接，不能只依赖选项值。
3. 材质的图片来源仅为该组件选定的漫射图，颜色连接到 `Base Color`。同张漫射图的 Alpha 可按原生行为保留并记录；法线、粗糙度、金属度等其他纹理不连接材质输出。
4. 修改现有材质检查逻辑：当前脚本要求漫射和法线两条路径都连到输出，实施后只验证允许的漫射连接及其实际 socket。
5. 保留漫射图的选择依据：组件、draw、槽位、格式、hash、文件路径。LoyalTools 的 `DiffuseMap` 别名存在启发式兜底，不能只凭文件名判断语义；无可信来源或存在冲突时报告，并支持明确指定映射。
6. 原始 NormalMap、LightMap、DDS 和 `TextureSlots.json` 等仍保留在工作空间中，供原生游戏导出使用。简化 Blender 预览不删除游戏资源。
7. 保存可直接打开编辑的 `.blend`、清楚的视口和检查报告，验证移动输出目录后素材路径仍可解析。

完成标准：实际打开 `.blend` 后几何、UV、骨组和蓝图有效；材质中只有允许的漫射贴图参与输出；缺图或错误候选明确记录。

## 7. 阶段四：编辑后按 LoyalTools 原生逻辑导出

1. 后台打开用户已保存的 `.blend`，选择指定蓝图及 EFMI 预设，并确认对应工作空间和 profile。
2. 复用 LoyalTools 正常导出入口 `bpy.ops.ssmt.generate_mod_blueprint(blueprint_name=...)` 及其前后处理。保留修饰器求值、临时对象、蓝图解析、组件权重归属、BUF／INI 生成和原生后处理的执行逻辑。
3. 不以直接调用 `ExportEFMI(...).export()` 替代完整流程；该底层调用本身不覆盖正常入口的全部前处理。
4. 导出前检查允许合法增删顶点、修改拓扑和调整蓝图。不能将提取时的顶点数、三角数和物体数量不变作为编辑后导出的条件。
5. 检查有效权重、全局骨组引用、必要顶点数据、UV、组件来源标记和蓝图引用；由原生导出器处理主控／LOD、目标绘制归属和缓冲格式。
6. 对原生特殊行为明确报告：例如 CPU posed 组件可能保留原游戏几何及贴图，而不导出其编辑后的几何，不能承诺所有导入物体都可同样改回游戏。
7. 保存输入工程及 profile 摘要、蓝图、版本、日志和导出文件清单。操作器可能在失败后仍返回 `FINISHED`，成功判定必须结合错误记录与实际产物检查。

导出先写本任务的独立暂存目录，检查通过后发布为一份完整产物。生成 INI／BUF 的规则与目录结构由 LoyalTools 决定；MCP 不另写一套替代导出算法，也不静默修补原生输出。

完成标准：原生导出链路执行完整；INI 引用的 BUF、DDS、附属文件存在；索引、stride、绘制范围、骨索引等检查通过；失败和残缺产物不会标记为可部署。

## 8. 阶段五：部署、刷新并查看导出模组的绘制状态

### 8.1 固定本次导出与部署身份

为每次导出生成唯一 `export_id` 和文件清单，记录源 `.blend`、蓝图、profile、INI／BUF／DDS 的 SHA256。部署后再次计算目标文件摘要，确保游戏将加载的是本次已检查产物。

部署目标由调用明确指定，限定为本任务专用测试模组目录。首次建立管理记录，以后的更新只替换该记录覆盖的文件；已有非本任务目录不被静默覆盖。暂存和备份放在 XXMI 递归加载范围之外，避免备份 INI 被一起加载；文件完整就位后才执行刷新。

实施此阶段时同步仓库写入边界：允许本任务导出产物的指定测试目录部署与更新；EFMI Core、启动器配置、原有其他模组仍按现有只读约定处理。这是用户本轮要求的部署验证能力，本计划不新增逐次 F10 确认步骤。

部署前检查实际 include／exclude 规则和启用模组，报告相同入口、hash／绘制范围或命名空间的潜在冲突。相同 hash 不自动等于冲突；需结合匹配条件。指定目录外的已有模组不自动搬移或禁用，无法排除的冲突会使目标验证保持未通过或证据不足。

### 8.2 执行已验证的自动刷新流程

1. 保存刷新前基线，确认目标角色、进程与渲染目标。
2. 部署成功后调用现有 `efmi_reload_and_capture`，由游戏内探针执行一次 F10，等待并捕获严格更新的帧。
3. 分别记录输入状态、是否取得新帧、`reload_confirmed` 与重载证据；采集失败时继续诊断，不自动重复发送 F10。
4. 正常采集无需开启狩猎模式。离线导出完成但游戏未运行时，保留产物并将游戏验证标记为待执行。

### 8.3 核对目标组件和实际资源

| 检查对象 | 证据与判定 |
|---|---|
| 当前角色及状态 | 基线、采样截图、组件特征和当前主控／LOD 状态相符 |
| 入口与替换关系 | 从本次 INI 匹配条件及导出清单追踪预期组件，再识别实际 XXMI 替换绘制；不要求替换后的 IB hash 等于原游戏入口 hash |
| 几何绘制 | 核对 index count、first index、base vertex、VB 布局及实际缓冲范围，处理重复 pass 和多次绘制 |
| 静态 BUF | 将实际使用的 IB／VB 区域与本次文件对应范围比较，记录字节／CRC／SHA 结果及范围；不能以局部 hash 代替完整几何检查 |
| 动态骨骼资源 | 检查绑定、容量、寻址和映射有效性；随动画变化的骨矩阵不与静态文件强行逐字节比较 |
| 贴图 | 按槽位及实际读取的 mip／slice 核对资源元数据和可比较的纹理内容，关联本次 DDS；区分文件封装与 GPU 像素数据 |
| CPU posed 或保留原网格组件 | 按 LoyalTools 原生导出预期单列，不把保留游戏原 draw 一律判为失败 |
| 主控、LOD 与蓝图开关 | 按实际观测状态记录覆盖；没出现的 LOD、分支或组件标记未覆盖，不能推断未执行变量的值 |
| 画面 | 保存截图用于外观核对；绘制和资源一致不自动证明所有视觉效果正确 |

总 draw 数仅作为摘要。角色不在画面中、合法分支隐藏组件、游戏原 draw 被正常替换，都不能单独作为模组失败依据；存在真实内容矛盾时才报告具体不匹配。

### 8.4 验证结果与证据

报告分别保存 `export_valid`、`deployment_verified`、刷新输入结果、`fresh_capture`、`reload_confirmed` 和 `target_mod_status`。目标状态使用 `passed`、`failed`、`inconclusive`、`not_run`，并附具体原因及覆盖范围。

`reload_confirmed=true` 只说明存在重载行为证据，不能代替本次模组验证；即使该证据不足，也要单独报告目标文件和实际资源的核对结果。只有声明范围内的预期组件及资源满足检查条件，才能将该范围标记通过。

最终保存部署清单、刷新回执、前后捕获、截图、组件对照及未覆盖项。异常要明确停在导出、部署、刷新、采集还是目标绘制检查阶段，方便继续诊断。

## 9. 输出布局

项目目录就是 LoyalTools 的自定义工作空间根目录，由调用指定。LoyalTools 用这个目录的名字给蓝图和导出的 INI 命名，所以目录名即模组名；这也是用户已有项目（`E:\Endfield mods\<角色>\workplace` 加同名 `.blend`）的布局。原计划里的 `project/` 一层因此取消：

```text
<项目>/                         # 名字即模组名
  workplace/                    # LoyalTools 原生工作空间（含 live-bridge-report.json）
  <项目>.blend                  # 可编辑工程；<项目>.verification.json 是导入检查报告
  source/sample-1..n/           # 三份独立采样及来源记录
  source/lod-<时间>/            # 现采的 LOD 帧（如有）
  exports/<export_id>/
    mod/                        # LoyalTools 原生产物
    export-manifest.json        # 文件清单与 SHA256、入口、绘制命令
    export-report.json          # 后台 Blender 的导出报告；.log.txt 为插件导出日志
    blender.log
  verification/<run_id>/
    deployment.json
    reload.json
    draw-verification.json
    screenshots/before.png, after.png
    previous-deployment/        # 被替换下来的旧部署文件（在 XXMI 加载范围之外）
    report.json, report.md
  efmi-model-project.json       # 做过什么、用的哪些版本
```

报告与日志置于原生 `mod/` 之外。输入工程和工作空间保留，重复导出使用新的 `export_id` 目录；可比较导出前后的文件摘要及运行时结果。部署目录默认为 `<EFMI>\Mods\EFMI-MCP\<项目>`。

## 10. 验证方案

| 用例 | 验收重点 |
|---|---|
| 未改模型的提取→导入→导出 | 几何和权重语义一致、原生元数据保留、文件引用完整 |
| 明确修改顶点或权重后导出 | 对应 BUF 和运行时使用区域确实改变，允许合法拓扑变化 |
| 漫射材质 | 实际节点仅使用允许的漫射图；原始游戏其他贴图仍可正常导出 |
| 原生导出一致性 | 同工程、同版本、同蓝图与正常 LoyalTools 导出比较；对路径／日志等非内容差异单独说明 |
| 主控与 LOD | 包含共用 IB 和不同布局样例，核对共享网格、入口、骨映射及实际远近状态 |
| 多角色或源数据不完整 | 不错选目标，不用残缺采样生成看似完整的结果 |
| 导出失败或 `FINISHED` 假成功 | 错误阶段明确，不发布、不部署残缺产物 |
| 旧模组或旧文件冲突 | 报告实际加载范围和产物身份，避免把旧版本绘制当成新版本成功 |
| 游戏未运行、焦点失败或捕获失败 | 分开报告导出与游戏验证结果，不自动重复 F10 |
| 完整实机闭环 | 本次文件部署一致、取得新帧、目标组件与资源匹配、截图外观核对，主控／LOD 覆盖明确 |

离线回归和私有沙盒先完成，再执行真实游戏验证。游戏资源、采集归档和生成的模组不提交仓库。

## 11. 实施顺序与里程碑

| 顺序 | 工作 | 完成标志 |
|---|---|---|
| M1 | 可复用采集／提取模块、任务管理、目标选择 | 正式 MCP 可生成经过校验的工作空间 |
| M2 | 漫射材质与 Blender 工程 | 可编辑 `.blend` 的结构和材质验收通过 |
| M3 | LoyalTools 完整导出封装 | 未修改和已修改模型均完成离线往返，产物检查有效 |
| M4 | LOD 输入、映射和原生导出接入 | 有数据支持的主控／LOD 用例通过；缺失状态标记准确 |
| M5 | 部署、自动刷新、逐组件绘制核对 | 本次产物在游戏中的绘制及资源得到证据支持 |
| M6 | 回归、文档、交付 | 完整流程说明、项目记忆、版本变更、Git 提交与推送完成 |

阶段性进展必须注明已覆盖范围。单模型提取通过、主控导出通过或 F10 成功均不等于完整闭环完成。

## 12. 实施参考入口

- 本项目：`scripts/capture_merged_source.py`、`server/efmi_mcp/loyal_bridge.py`、`scripts/verify_merged_workspace_blender.py`。
- 本项目：`server/efmi_mcp/tools.py`、`server/efmi_mcp/reload_workflow.py`、`server/efmi_mcp/mods.py`。
- LoyalTools：`common/mesh_create_helper.py`、`common/efmi_merged_texture.py`、`common/efmi_merged_skeleton.py`。
- LoyalTools：`ui/ui_func_export.py`、`ui/universal/efmi.py`，以及已有 merged skeleton／LOD 导出回归。

默认使用所选 Blender 中已安装的 LoyalTools 插件；`E:\loyal tools\tools\LoyalTools` 仅在找不到已安装插件时作为后备。两处都保持只读，导入其模块时不写入字节码缓存。

## 13. 实施记录与已验证范围（2026-10-07）

实现位置：`server/efmi_mcp/` 下的 `model_capture.py`（采样）、`loyal_bridge.py`（提取、候选列表、LOD 两种匹配）、`blender_runner.py`、`model_jobs.py`、`model_workflow.py`（项目、提取、LOD、导出与产物检查）、`mod_runtime.py`（部署与绘制对照）、`model_tools.py`（MCP 工具）；Blender 内脚本为 `scripts/verify_merged_workspace_blender.py`（导入，默认仅漫射）和 `scripts/export_mod_blender.py`（原生导出）。

| 环节 | 验证方式 | 结果 |
|---|---|---|
| Blender 导入、仅漫射 | 真实 Blender 4.5.7 + 已安装插件 1.6.8，两个现有工作空间的副本 | 通过：只有漫射图接到 Base Color（及其自身 Alpha），图片路径相对 `.blend`，记录了漫射图的槽位与哈希来源 |
| LOD：JSON 匹配 | 伊冯现有工作空间的 profile，去掉 LOD 后从原 JSON 恢复 | 通过：10／11 个组件与原映射一致，另 1 个被 LoyalTools 判为不兼容并报告 |
| LOD：帧匹配（两侧都是帧分析目录） | 吉尔伯塔的近景／远景帧分析目录 | 通过：9 个组件的入口、布局和顶点组对应与 LoyalTools 当时生成的映射一致 |
| LOD：帧匹配（探针归档作为一侧） | — | **未验证**，需要游戏运行以采集归档 |
| 原生导出与产物检查 | 后台 Blender 运行插件的 Generate Mod，含／不含 LOD 映射各一例 | 通过：INI／BUF／DDS 齐全，绘制范围与索引在界内，主控与 LOD 入口均生成；`.blend` 未被改写 |
| 导出失败的识别 | 插件日志中的完成／失败语句与实际产物 | 已实现；插件日志保存时会去掉表情符号，按其文字判断 |
| 部署规则、绘制对照、任务管理、Blender／插件的选择 | `sandbox/model_workflow_test.py`，37 项，使用伪造的捕获 | 通过 |
| 工具层离线串联 | `sandbox/model_offline_integration.py`（需本机装有插件的 Blender 和一个工作空间），18 项 | 通过 |
| 实时三采样提取（MCP 工具） | — | **未验证**。底层桥接此前由脚本对梨诺执行过一次；本次改为模块并加入任务与目标选择后尚未在游戏中运行 |
| 部署 → F10 → 逐组件核对 | — | **未验证**，需要游戏运行且角色在画面中 |

与原计划的差异：

- 增加 `efmi_map_lod` 与 `efmi_model_project` 两个工具；LOD 不再只作为提取的输入参数。
- 项目目录取消 `project/` 一层（见第 9 节）。
- 贴图对照目前只比较 XXMI 创建并绑定的贴图的最高一级 mip；骨骼等运行时计算的缓冲不比较。
- 编辑后模型的往返（修改顶点或权重后导出并在游戏中看到变化）尚未做过，属于第 10 节中待执行的用例。
