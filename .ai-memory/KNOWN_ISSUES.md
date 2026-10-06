# EFMI MCP — 已知问题

> 最后更新：2026-10-06。

## BUG-004：真实游戏接受发送 API，但 F10 按下状态不可见（未解决）

- Endfield PID41976的两次普通VK请求没有实际刷新。用户移出Yvonne后，画面仍是该模组；用户明确确认失败。普通新帧及IB/VB读回均正常，不能用它们证明重载。一次中间的focus_refused在输入前拒绝，不算发送请求。
- 扫描码诊断同样失败：前台PID正确、无修饰键，SendInput down/up返回1、错误码0，但约200ms按住期间所有F10异步高位为false；帧160361→160664，XXMI DrawIndexed的IB/次数相同。证据 `captures/verify-reload-scancode-41976-ready/result.json`，独立结论 `assessment.json` 标明 `actual_reload_succeeded=false`；raw报告pass仅表示诊断流程完成。
- 0.1.3增加 `key_down_observed` 门槛：若API接受但未观察到按下状态，返回input阶段 `keydown_not_observed`，finally释放，不开始capture、不重发。`key_down_sent`继续保留API接受语义。此改动完成错误检测，未解决真实输入不可见问题。
- 当前Python为medium，游戏token读取access denied；不能据此推定游戏完整性级别或安全软件行为。扫描码不作为修复。继续定位正常输入路径，不自动提权、不修改EFMI、不绕过输入保护。
- 官方v1.2.2 [input.cpp](https://github.com/SpectrumQT/XXMI-Libs-Package/blob/v1.2.2/DirectX11/input.cpp) 的 `VKInputButton::CheckState` 使用GetAsyncKeyState高位，未发现扫描码或注入标志过滤逻辑。采样到高位也只是必要诊断证据，不是游戏线程已处理/XXMI重载的回执。
- `d3dx_user.ini` 时间不变不能判断失败：[IniHandler.cpp](https://github.com/SpectrumQT/XXMI-Libs-Package/blob/v1.2.2/DirectX11/IniHandler.cpp) 的 `SavePersistentSettings(false)` 未dirty时直接返回；普通重载不是强制写入。探针registry.resources是登记地址数，地址复用会覆盖，亦不是重载计数。
- 在所查v1.2.2源码及[导出表](https://github.com/SpectrumQT/XXMI-Libs-Package/blob/v1.2.2/DirectX11/d3d11Wrapper.def)中未找到受支持的外部reload接口；Core/auto_update.xcmd是启动器更新/启动操作文件，不是运行时通信。现有只改本工具约束下不能把写该文件或调用内部函数当作受支持替代。

## 限制-002：自动 F10 没有重载完成回执

- MCP服务0.1.3区分API接受、异步按下状态可见及后续新帧；探针0.1.1没有XXMI解析/重载完成回执，`reload_confirmed`始终为false。即使输入状态门槛通过，仍需核对预期draw/BUF，不能据此认定全部INI解析成功。
- 默认等待 2 秒不是完成信号；机器慢或模组复杂时可调整 `settle_ms`（250–10000），已发送 F10 后仅需更多画面可调用普通 capture，不自动再重载。捕获 timeout 单独为 1000–30000ms。
- 会停止 live、release 并清旧捕获；live 不自动恢复。正常 F10 也可能触发 XXMI 保存持久变量配置，因此有运行时及文件副作用。
- 本轮0.1.3输入21项/工作流32项、MCP39项及compileall通过。历史0.1.2私有沙盒在hunting=0下验证新增规则仅抑制目标IB（`sandbox/build/reload-xuxnsq73/reload-evidence.json`）；本轮游戏运行时未重跑实际F10沙盒。当前实机刷新测试失败，见BUG-004。
- 单次 F10 按下/释放、约 200ms 按住期间，XXMI 1.2.2 重建按键注册可发生多次内部重载。工具没有重发按下事件，但不能承诺 XXMI 内部 reload 次数恰好为一。

## 限制-003：Windows 前台输入与权限

- Windows 可拒绝 `SetForegroundWindow`；`focus_game=false` 要求游戏已处于前台。无可确认窗口、多个候选而目标不在前台、按住修饰键/F10 或焦点丢失时拒绝发送或停止按住。
- `SendInput` 可能被 UIPI/不同完整性级别拦截，不能仅凭 Win32 错误码判断具体原因；工具不自动提权。窗口检查与发送之间不是原子操作，输入成功也不是游戏实际处理的证明。
- 异常会尝试释放 F10，绝不重复按下；结果保留 `reload_requested` 为 false/true/null 区分未发送/已发送/未知。若释放也失败，应先按错误提示处理键状态，不能盲目重试。
- 当前仅支持当前安装两项默认 `no_modifiers VK_F10` 绑定；自定义或重复绑定会拒绝，不自动改配置。无需开启狩猎模式。

## BUG-001：后建辅助设备导致主画面捕获为空

- 状态：`0.1.1` 已修复，多设备四场景 `192` 项和最终整套回归通过；新版已在 PID `38252` 的伊冯画面通过非空帧与真实资源读回验证。
- 现象：探针连接、分辨率和 Present 帧号可用，但完整捕获和 live 均无 draw；此前第 `22964` 帧 draw / 资源 / 着色器均为 `0`。
- 证据：PID `39000` 的探针日志先挂接主设备并发现交换链，随后记录新设备替换旧跟踪设备；`g_dev/g_ctx` 安装逻辑覆盖旧值，而 `g_swap` 保留。
- 旧版影响：`h == g_ctx` 丢掉主画面绘制，无法识别或导出角色的 IB/VB/贴图/着色器。
- 实机结果：帧 `9113` / `14377` 非空，连续摘要正常；伊冯三组网格共 `12` 个 IB/VB 读回 CRC 一致，纹理/CB/着色器均已读取，证据见当前状态。

## BUG-002：live 停止后在途帧回填

- 状态：已修复；新增 `10` 检查，自测 `83` 通过 / `0` 失败，最终整套回归通过。
- 现象：首轮探针自测 `71` 通过 / `2` 失败；停止并清空连续摘要后，仍在进行中的旧帧可能再次写入，影响快速停止/重启和连接更换的会话隔离。
- 修复：每帧携带 generation；停止（`enable=false`）或断开连接使 generation 失效，发布时拒绝旧帧；`enable=true` 参数重配不更换 generation。

## ISSUE-003：沙盒 posed VB 浮点输出偶发末位变化

- 状态：测试场景已修，shader 加 HLSL `precise`，严格字节/哈希断言保留；最终整套回归通过。
- 现象：本轮三种加载顺序中 XXMI hash 对照各 `69` 项零差异，但冷启动 GPU posed VB 再次取哈希的一致性检查有失败。
- 证据：无 dump、甚至旧探针 + 无 XXMI 时也能观察到 `39` 个 float 各变化 `1 ULP`（最大 `5.96e-8`）；每次探针 hash 都与读回 CRC 一致，不是本轮探针回归，具体优化机理尚属推断。
- 修后验证：有/无 XXMI 各 `12` 次从 frame `1/2` 开始连续首帧捕获，`11616` 字节 posed VB 逐字节一致，CRC `1b4be723`。
- 最终结果：三种加载顺序各 `69` 项 hash 对照零差异，二次捕获 IB/VB 哈希一致均通过。沙盒浮点变化不是当前真实角色数据错误的证据。

## 限制-001：交换链选择与重建

- 当前选择首个发现的交换链，不会自动从多个候选交换链识别主画面。
- 已绑定目标后，另一个设备提供的替代交换链会被拒绝并提示重启，以免跨设备使用资源/捕获状态。
- 同设备交换链销毁和重建期间的跨界捕获尚未验证；审查发现可能得到跨帧不完整结果。重建场景不属于本轮多设备专项已覆盖的承诺。

## 已有功能限制

- 数据读取的游戏外处理开销已在T-006优化（当前梨诺完整归档1.843→1.220秒）；GPU/管道仍逐资源返回，纹理较多时仍有逐帧等待。没有批量GPU读回接口，本轮不声称解决所有场景的延迟。PNG低压缩保留像素但文件更大；可选CRC库缺失时功能可用、速度回到Python基线。

- 合并骨骼桥接的骨SRV不是draw时刻快照；当前静态编辑流程须通过三次独立捕获的几何/骨映射一致性验证。仅凭骨池非零、首尾地址相同或采集完成不能视为正确映射。
- SRV视图FirstElement/NumElements暂未记录；当前桥接按EFMI默认的完整vs-t0底层池解释偏移，不能推广到任意非零起点子视图。
- 桥接直接沿用LoyalTools的VB0–2、DrawIndexedInstanced和布局冲突规则，当前不处理Mod替换的普通DrawIndexed，也不从近景自动构造LOD。

- 探针须在创建 D3D11 设备之前由启动器加载；DLL 固定到进程退出，没有当前会话热更新能力。
- 延迟上下文中的 draw 不可见；状态可提示是否出现过。
- 普通资源读取是读取时刻内容；draw 时刻 CB 需在捕获请求中指定 `cb_data=true`。
- 只保证最新捕获的资源可读；多重采样贴图和三维贴图暂不支持读回。
- XXMI `shader_hash=bytecode` 未实现；EFMI 默认 `3dmigoto` 模式可用。
- GPU 改写缓冲后，探针按当前字节算出的哈希可能不同于 XXMI 缓存的旧值。
- mod 替换后可见的是实际提交的 draw；`origin=xxmi` 来自调用栈启发式分类，不是角色识别标签。

以上来自当前 README/源码，不应扩大为本次修复已处理的功能。
