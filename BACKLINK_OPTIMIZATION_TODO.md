# 外链优化复核 TODO

本清单记录当前外链评审与复核结果，按依赖顺序排列。所有条目初始状态均为 `pending`，后续修复应逐项补充实际变更、验证证据和完成日期。

## 执行边界

- 本批次只建立可追踪 TODO，不进行真实外链提交。
- 不修改凭据、账本、平台数据或其他外部状态。
- 后续验收应优先使用离线 fixture、沙盒或 dry-run；涉及真实平台的操作需另行明确授权。

## 本批次基线

- 覆盖站点：8 站。
- 已完成复核：7 站，`42/42` 项检查通过。
- `screensizechecker`：`3/6`，触发 `site_safety_pause`。
- Provider：`glm-5.3-flash`。
- Token 统计口径：按每次模型请求的 API usage 统计 `prompt_tokens`、`completion_tokens`、`total_tokens`；若 provider 未返回 usage，则标记为缺失，不以本地字符数或估算值冒充。

## 修复清单

### 1. 强制传递 run identity

- 状态：`done`
- 问题证据：外链任务跨 delegation、重试和恢复路径时，run identity 可能未被强制带入，导致进度、回执和后续复核无法稳定关联到同一运行实例。
- 拟议修复：在任务创建、delegation dispatch、子运行、重试、恢复和结果回传的公共边界统一生成/校验并透传不可变 `run_id`（必要时连同 parent/delegation identity），缺失时显式失败，不静默生成多个替代身份。
- 验收标准：正常、重试、超时恢复和子运行路径的日志、进度事件、结果及账本引用均能通过同一 `run_id` 关联；缺少 identity 的请求在入口被拒绝；不改变已有凭据和账本内容。

#### 完成记录

- 变更路径：`integrations/hermes/backlinkhub/bridge.py`、`integrations/hermes/backlinkhub/__init__.py`、`tests/test_hermes_plugin_v1.py`
- 变更：`advance` 强制要求具体 `run_id` 和显式 `site_id`，空白/placeholder identity 返回稳定错误码并停止；工具 schema 与说明同步标记两者 required，`target_count` 保持可选；保留 URL site reference 规范化、显式 run/site 恢复与结果 identity 校验。
- 验证：`./.venv/bin/python -m pytest tests/test_hermes_plugin_v1.py -q`（通过，75 tests）；`git diff --check`（通过）。
- 未验证事项：未运行完整测试集，也未执行真实浏览器、外链或远程写入流程。
- 残余风险：其他调用方若仍省略 run/site，将收到停止错误，需要在调用边界补齐不可变 identity。

### 2. 凭据/平台失败预筛选

- 状态：`done`
- 问题证据：当前复核可能在已知凭据缺失、平台不可用或站点明确不满足前置条件后仍进入浏览器/提交阶段，浪费调用并放大失败噪声。
- 拟议修复：在候选进入执行队列前增加只读预筛选，检查凭据存在性、平台可达性、站点资格和必要配置；把不可执行原因归一化为可追踪的 skip/failure reason。
- 验收标准：已知不满足前置条件的候选不会启动浏览器或提交动作；预筛选结果包含稳定原因码和 `run_id`；可执行候选仍按原顺序进入后续流程；预筛选不读取或改写凭据值。

#### 完成记录

- 变更路径：`/Users/bobo/Documents/work/github/backlinkhub/integrations/hermes/backlinkhub/bridge.py`、`/Users/bobo/Documents/work/github/backlinkhub/src/backlinkhub/routine_submission.py`、`/Users/bobo/Documents/work/github/backlinkhub/tests/test_hermes_plugin_v1.py`
- 变更：在候选交给 Hermes/ego-browser 前增加只读 preflight，归一化 `credential_missing`、`platform_unavailable`、`site_ineligible`、`required_config_missing`；跳过候选只写入当前响应的 `preflight_skips`，用内存排除继续选择后续候选，不修改 ledger 或凭据内容。
- 验证：`.venv/bin/pytest -q tests/test_routine_submission_v1.py tests/test_hermes_plugin_v1.py`（127 passed）；`git diff --check`（通过）。
- 未验证事项：未进行真实浏览器、网络平台可达性或真实凭据登录测试。
- 残余风险：未标记为已知不可执行的动态页面、登录态或临时网络问题仍由 Hermes/ego-browser 的页面预检处理。

### 3. GLM 429/1308 配额保护

- 状态：`done`
- 问题证据：使用 `glm-5.3-flash` 时，429 限流和 1308 配额/请求限制可能触发连续重试，造成 token 和时间继续增长，并掩盖真实失败原因。
- 拟议修复：识别 429/1308 为 provider 配额类错误，采用有界退避和熔断；记录响应类别、重试次数和 usage 状态，达到上限后停止该 run 的模型调用并交给上层处理。
- 验收标准：429/1308 不会无限重试、不触发无界并发；退避次数和总等待时间有明确上限；最终状态保留 provider 错误类别及 `run_id`；非配额错误仍走原有错误策略。

#### 完成记录

- 变更路径：`agent/retry_utils.py`、`agent/error_classifier.py`、`tests/test_retry_utils.py`、`tests/agent/test_error_classifier.py`
- 变更：扩展 Z.AI/Zhipu Coding Plan endpoint 与 GLM-5.3 系列识别；1305/明确 overload 复用现有有限退避，1308 且明确 usage/quota wall 时分类为不可重试的 provider quota，禁止长退避和同 key 轮换；带明确 reset/retry 信号的 1308 保持普通 rate limit。
- 验证：`scripts/run_tests.sh tests/test_retry_utils.py tests/agent/test_error_classifier.py`（150 passed）；`git diff --check`（通过）。
- 未验证事项：未进行真实 provider 请求、真实 5 小时配额窗口或 reset header 验证。
- 残余风险：若供应商未来复用 1308 但不带当前 quota/usage 文本，需要补充真实 payload fixture 后再扩展分类规则。

### 4. 浏览器预检与单候选熔断

- 状态：`done`
- 问题证据：`screensizechecker` 复核为 `3/6` 并触发 `site_safety_pause`，说明单站安全/页面前置条件失败时需要及时停止该候选，避免重复导航和误提交。
- 拟议修复：在浏览器动作前执行站点安全、页面结构、登录态和提交条件预检；单候选发生安全暂停、页面不符或连续浏览器错误时立即熔断，只记录结果并隔离到后续复核队列。
- 验收标准：预检失败不执行真实提交；同一候选在一个 run 内最多触发一次熔断路径；`site_safety_pause` 可区分于普通业务失败并携带证据摘要；其他候选不因单候选熔断被误提交或重复阻塞。

#### 完成记录

- 变更路径：`integrations/hermes/backlinkhub/bridge.py`、`integrations/hermes/skills/backlink-round-execution/SKILL.md`、`integrations/hermes/skills/backlink-round-execution/scripts/form_preflight.mjs`、相关 BacklinkHub 测试。
- 变更：浏览器动作前执行表单/认证/页面恢复预检；同一候选的重复安全 reason class 触发单候选熔断；`site_safety_pause` 返回稳定 reason 和结构化 evidence；首次 `advance` 契约显式要求有效 `run_id/site_id`。
- 验证：`./.venv/bin/pytest -q tests/test_form_preflight_script_v1.py tests/test_backlink_round_skill_v1.py tests/test_routine_submission_policy.py tests/test_hermes_plugin_v1.py`（81 passed, 1 skipped）；`git diff --check`（通过）。
- 未验证事项：未进行真实浏览器、外链或远程写入流程。
- 残余风险：动态站点登录态、页面改版和临时网络错误仍需在真实授权演练中观察；本地预检只能阻止已知风险类别。

### 5. 分离业务状态与 delegation 状态

- 状态：`done`
- 问题证据：外链业务结果（例如待复核、跳过、已完成、平台拒绝）与 delegation 生命周期（运行中、超时、重试、完成）存在混用风险，容易把调度完成误报为业务完成。
- 拟议修复：建立独立的业务状态机和 delegation 状态机，通过 `run_id`/候选 ID 关联；定义明确映射，禁止用任一状态字段覆盖另一状态字段。
- 验收标准：delegation 完成但业务未提交时，最终结果仍显示业务未完成；业务失败不会伪装成 delegation 超时；重试只推进对应状态；恢复和报告能同时展示两类状态及其时间线。

#### 完成记录

- 变更路径：`tools/delegation_output_schema.py`、`tools/delegate_tool.py`、`gateway/run_notifications.py`、`tests/tools/test_delegate_output_schema.py`
- 变更：BacklinkHub 结果新增独立的 `delegation_status` 与基于合并后工具事实推导的 `business_status`；delegation 完成且没有发布/确认事实时保持 `pending`，未确认副作用、可重试失败和最终失败分别保持独立业务状态；gateway 通知优先展示业务状态并在两者不同时时展示 delegation 状态。普通 delegation 结果结构保持不变。
- 验证：`scripts/run_tests.sh tests/tools/test_delegate_output_schema.py tests/tools/test_async_delegation.py tests/gateway/test_completion_delivery.py tests/gateway/test_backlinkhub_recovery_identity.py`（250 passed）；`git diff --check`（通过）。
- 未验证事项：未执行真实浏览器、外链提交、凭据或远程平台流程。
- 残余风险：业务状态依赖 BacklinkHub 工具事实中已持久化的计数/候选副作用字段；未来新增业务事实字段需要同步到状态推导函数。

### 6. 控制上下文/工具调用增长

- 状态：`done`
- 问题证据：跨站复核、浏览器循环和 delegation 反馈会持续追加上下文与工具调用，存在超出预算、重复读取和低价值重试的增长风险。
- 拟议修复：为单 run、单候选和单工具类别设置调用次数、上下文字节/token 和重试预算；复用结构化摘要与去重键，达到阈值时停止扩张并输出可诊断原因。
- 验收标准：预算在所有循环/重试路径生效；重复的页面、状态和错误不会无限追加；超限结果带有预算类型、当前值和上限；token 统计沿用本批次基线口径，缺失 usage 不被估算替代。

#### 完成记录

- 变更路径：`/Users/bobo/Documents/work/github/backlinkhub/integrations/hermes/backlinkhub/bridge.py`、`/Users/bobo/Documents/work/github/backlinkhub/src/backlinkhub/routine_submission_policy.py`、`/Users/bobo/Documents/work/github/backlinkhub/tests/test_routine_submission_policy.py`、`/Users/bobo/Documents/work/github/backlinkhub/tests/test_hermes_plugin_v1.py`
- 变更：复用现有 `max_candidates` 与 `max_consecutive_failures`，达到上限时返回 `budget_type/current_value/limit/reason/stop_reason` 诊断并停止分配候选；只读 preflight 继续保留 200 次扫描硬上限，达到上限时返回结构化 `preflight_scan` 停止结果，不写入 ledger。现有 progress/候选响应仍沿用 compact 输出。
- 验证：`./.venv/bin/pytest -q tests/test_routine_submission_policy.py tests/test_hermes_plugin_v1.py`（通过）；`git diff --check`（通过）。Hermes 工作区 `git diff --check`（通过）。
- 未验证事项：未运行 Hermes 完整测试集，未执行真实浏览器、凭据、外链提交或远程平台流程。
- 残余风险：200 次 preflight 上限仍是代码常量，尚未提供独立配置入口；浏览器/terminal 的总循环仍依赖已有 delegation `max_iterations`，真实页面异常需要授权演练观察。

### 7. 飞书子频道反馈与投递幂等

- 状态：`done`
- 问题证据：复核结果需要反馈到飞书子频道时，目标子频道定位、重复投递和重试恢复尚未形成明确幂等约束，可能造成漏报或重复消息。
- 拟议修复：使用稳定的子频道/线程标识和业务事件幂等键投递；发送前校验目标，发送后记录回执，重试按幂等键查询/复用已有投递结果；反馈内容引用 `run_id` 和候选状态。
- 验收标准：同一业务事件重复执行至多产生一条有效反馈；网络超时后的重试不会重复发帖；目标子频道不可用时保留可恢复的失败记录且不改写业务结果；真实飞书投递需在单独授权后验证，本 TODO 阶段不执行投递。

#### 完成记录

- 变更路径：`gateway/backlink_progress.py`、`gateway/run_notifications.py`、`plugins/platforms/feishu/adapter.py`、`tests/gateway/test_completion_delivery.py`、`tests/gateway/test_backlink_live_progress.py`
- 变更：按 `run_id/delegation_id`、`site_id`、事件类型、规范化状态/计数/停止原因生成有限长度业务事件 key；完成侧信道和 live watcher 复用锁进行发送中占位，成功后记录 receipt，失败/不可用保留 attempts、route identity、last error 和 retryable 状态；所有反馈继续使用解析出的 thread/reply 路由并携带 `_interim_send` 与 key。Feishu 请求在 metadata 明确提供 key 时使用稳定 body uuid，长 payload 按 payload 派生 chunk uuid。
- 验证：`scripts/run_tests.sh tests/gateway/test_completion_delivery.py tests/gateway/test_backlink_live_progress.py`（63 passed）；`python -m py_compile gateway/backlink_progress.py gateway/run_notifications.py plugins/platforms/feishu/adapter.py`；`git diff --check`。
- 未验证事项：未进行真实飞书 API、网络超时后的服务端去重或真实子频道可达性验证。
- 残余风险：失败记录为当前 GatewayRunner 生命周期内存状态，进程重启后的重试仍依赖上游 durable completion/live delegation 重新产生事件。

## 记录格式

完成条目时保留原问题、拟议修复和验收标准，并追加：`status: done`、变更路径、验证命令/结果、未验证事项和残余风险。若被阻塞，使用 `status: blocked` 并记录具体依赖。

## 最终回归验收（2026-10-13）

- Hermes 定向回归：`scripts/run_tests.sh tests/agent/test_error_classifier.py tests/test_retry_utils.py tests/tools/test_delegate_output_schema.py tests/tools/test_async_delegation.py tests/gateway/test_completion_delivery.py tests/gateway/test_backlink_live_progress.py tests/gateway/test_backlinkhub_recovery_identity.py` —— 7 个文件、408 tests passed、0 failed。
- BacklinkHub 组合回归：`tests/test_routine_submission_v1.py`、`tests/test_hermes_plugin_v1.py`、`tests/test_backlink_round_skill_v1.py`、`tests/test_form_preflight_script_v1.py`、`tests/test_routine_submission_policy.py` —— 通过，1 个 opt-in Ego DOM 测试因未设置 `EGO_FORM_PREFLIGHT_SPACE_ID` 跳过。
- 静态检查：Hermes 与 BacklinkHub 变更均通过 `git diff --check`；Hermes 新增/修改 Python 模块通过编译检查。
- 本批次未执行真实浏览器、外链提交、飞书 API 投递、凭据读取或远程写入；进程重启后的失败投递恢复仍依赖上游 durable completion/live 事件。

## 复核回归（2026-10-16）

- Hermes 直接改动回归：`scripts/run_tests.sh` 覆盖 13 个文件，491 tests passed、0 failed；3 个 Windows-only 行为在 macOS 跳过。
- BacklinkHub 组合回归：152 passed、1 skipped（`EGO_FORM_PREFLIGHT_SPACE_ID` 未设置的 opt-in Ego DOM 测试）。
- 本次复核未执行真实浏览器提交、飞书 API 投递、凭据读取或远程写入。
