---
name: backlink-round-execution
description: 以 BacklinkHub 队列执行外链提交。
license: Proprietary
metadata:
  version: 2.1.0
  author: bobo, Hermes Agent
  platforms: [macos]
---

# 外链轮次执行

本技能只在用户主动发起外链任务时使用。BacklinkHub 负责候选顺序、去重、结果和逐站进度；
Hermes 负责调度；站点执行者使用 `ego-browser` 完成真实网页操作、关闭标签并回写结果。
本流程不创建审批，不运行测试、迁移、候选重建或全库核验。

> 执行器硬规则：本机 ego-browser 的唯一可执行文件是
> `/Users/bobo/.local/bin/ego-browser`。所有浏览器操作必须调用这个绝对路径；禁止调用
> `ego-lite`、`ego`、裸的 `ego-browser` 或 OpenCLI。不要先用 `which` 探索命令，直接执行下面的
> 绝对路径；如果该路径报错，记录 `failed_retryable/ego_browser_unavailable` 并停止当前候选。

## When to Use

用户说“提交今天的外链”“提交某个站点的外链”或指定站点和数量时使用。一次任务指令已经
授权正常提交，不为每个平台重复请求批准。

## Prerequisites

- 新任务首次调用 `backlinkhub_advance_submission_round` 时不传 `run_id`，保存工具返回的唯一
  `run_id`。后续领取和回写都使用该 ID；只有用户明确要求继续某个旧轮次时才复用旧 ID。
- 每个站点只启动一个同步的 `gpt-5.6-luna` 执行者。它在自己的会话内连续领取、提交、关闭、
  回写本站候选，直到本站达到目标、队列确实耗尽或执行器发生不可恢复的技术错误。
- 每个站点轮次只使用一个 ego 任务空间，名称固定为 `bh_<site_id>_<run_id>`。同一执行者和恢复执行者
  都复用该空间；每条候选只新开一个临时标签，候选结束立即关闭标签。
- 每条候选通常使用两次浏览器调用：第一次打开并读取精简快照，第二次填写、提交、核验并
  关闭标签。只有登录、动态表单或技术恢复确有需要时才增加调用；调用次数本身不是失败条件。
- 执行者工具集只包含 `terminal` 和 `backlinkhub`。只调用
  `backlinkhub_advance_submission_round`、`backlinkhub_record_submission_result`，以及
  `ego-browser`；不得调用原生 Hermes browser、Task、Claim、Attempt、审批或其他委派工具。
- ego 已安装并完成首次 Chrome 数据迁移。轮次空间复用迁移后的登录态，不触碰用户原有标签。
- 邮箱魔法链接继续使用本地受控脚本读取 Gmail 只读授权。脚本只返回安全状态码，并在 ego
  任务空间打开经域名校验的链接，不输出邮件正文、邮箱地址或一次性链接。

## How to Run

首次领取候选后，用 BacklinkHub 返回的真实 `run_id` 生成唯一轮次空间名，例如
`bh_site_thesitemath_round_20260822_101304_625445e6`。每次 `terminal` 都是独立 Shell，但整个站点轮次始终
复用这个空间；每条候选只保存自己打开的标签对象：

```bash
/Users/bobo/.local/bin/ego-browser nodejs <<'EOF'
const task = await useOrCreateTaskSpace("bh_site_thesitemath_round_20260822_101304_625445e6")
const tab = await openOrReuseTab("https://example.com/submit", {wait: true, timeout: 20})
cliLog(JSON.stringify({task_id: task.id, page: await pageInfo()}))
cliLog((await snapshotText()).slice(0, 6000))
EOF
```

继续操作和处理下一候选时仍调用同一个 `useOrCreateTaskSpace("bh_<site_id>_<run_id>")`，不要依赖上一个
Shell 里的变量。普通页面使用 `snapshotText`、`click`、`fillInput`、`uploadFile`、
`waitForElement`；下拉框使用 `click` 加键盘操作，或一次 `js` 设置值并触发 `input/change`。
可访问性树为空、跨域 iframe 或动态控件无法操作时，改用
`captureScreenshot` 加坐标操作，必要时使用一次受控 `js` 或 `cdp`。一个 heredoc 尽量合并
观察、填写、等待和结果判断，减少模型往返。

用户明确回复“继续”或“已释放，继续”后，如果同名空间处于 `agentDelegatedToUser`，恢复执行者
必须先调用 `takeOverTaskSpace("bh_<site_id>_<run_id>")`，再选择原候选标签继续。没有用户明确确认时
不得夺回控制。

## Quick Reference

- 打开：`openOrReuseTab(url, {wait: true})`
- 查看：`snapshotText()` 或 `pageInfo()`；输出只保留与表单、登录和回执有关的片段
- 点击：`click("@ref")`、`click("loc=...")` 或稳定 CSS 选择器
- 填写：`fillInput(target, value)`；下拉使用 `click` 加键盘，或一次受控 `js`
- 上传：`uploadFile("input[type=file]", absolutePath)`
- 等待：`waitForElement(target, {timeout: 15})` 或 `waitForLoad()`
- 关闭候选：`closeTab(tab)`；关闭后不再调用 `listTabs` 或截图
- 关闭轮次：达到目标或队列耗尽后，在单独的最终 heredoc 调用
  `completeTaskSpace("bh_<site_id>_<run_id>", {keep: false})`

## Procedure

1. 首次调用 BacklinkHub，保存 `run_id` 和第一条候选。不要重复读取历史记录，也不要为
   汇总再次调用候选工具。
2. 主 Hermes 不直接在长期会话里操作浏览器，只把站点引用交给站点级 Luna 执行者。执行者
   自己逐条调用 BacklinkHub，保存首次返回的真实 `site_id` 和 `run_id`，只处理该站点轮次。
3. 执行者创建或复用 `bh_<site_id>_<run_id>` 轮次空间，为当前候选打开一个标签。先读取一次精简快照；
   表单字段明确后合并填写、最终动作、结果核验和关闭。不要为 work item 创建新任务空间。
4. 每次读取或操作前先检查 `pageInfo()`。如果返回原生 `dialog`，只处理当前流程预期且已获
   用户授权的确认框；其他确认框默认拒绝并记录。登录时优先点击一次 Google 登录、账号选择和普通 Continue，复用迁移的 Chrome 登录态。
   只有密码、验证码、二次验证或安全挑战才记 `failed_retryable`；不得绕过安全挑战或虚构
   账号资料。
5. 页面明确发送邮箱登录/验证链接时，先记录发送动作时间，然后执行：

   ```bash
   python3 /Users/bobo/.hermes/skills/productivity/backlink-round-execution/scripts/gmail_magic_link.py \
     --session bh_<site_id>_<run_id> --after-epoch <字面时间戳> \
     --link-host "平台根域" --wait-seconds 45
   ```

   返回 `magic_link_opened` 后读取一次页面状态并继续。找不到链接、Gmail 不可用或域名不匹配
   时记 `failed_retryable`，关闭当前候选标签并领取下一候选，继续保留轮次空间。
6. 使用 BacklinkHub 返回的真实站点资料填写。免费/付费并存时选择免费方案；需要 Logo、
   截图或 PDF 时使用已有素材。最终提交最多点击一次。
7. 结果分类必须严格按页面证据：
   - `published`：有稳定公开结果页或公开页面明确出现官网链接；
   - `pending`：页面明确显示已收件、待审核或排期；
   - `attempted_unconfirmed`：已点击但没有明确成功或审核回执；
   - `failed_retryable`：登录、OAuth、邮箱验证、验证码、安全挑战、临时网络、浏览器连接、
     可补充资料或表单技术问题；
   - `failed_final`：产品明确不适用、没有免费路径、强制互惠、入口失效，或平台要求的业务
     条件客观上无法如实满足。
8. 正常结果无论成功或失败，都立即关闭本候选创建的标签；关闭后不要再调用 `listTabs` 或
   截图，再调用 `backlinkhub_record_submission_result` 回写一次。只填写结果、方法、注意事项、
   失败原因和最多两条证据；候选身份由紧邻的领取结果自动补齐。回写返回 `already_recorded`
   时视为已完成，不重新打开或提交。
9. 回写后由同一站点执行者继续领取下一候选，直到成功目标达到或队列确实没有可用候选。失败
   不占成功名额；目标数量是成功目标，不是失败次数上限。
10. 遇到 `EGO_TASK_SPACE_USER_IN_CONTROL` 时立即停止浏览器操作，不重试、不另建空间，也不要
    声称用户手动接管。统一表述为“ego-browser 报告控制权切换，可能由原生确认框缺陷或界面
    接管触发”。最终点击前中断时回写 `failed_retryable/ego_task_space_control_interrupted`；
    最终点击后中断时回写 `attempted_unconfirmed`。这两种情况都不得写 `failed_final`。
11. 用户明确要求继续后，主 Hermes 用相同 `run_id`、`site_id` 恢复一个执行者。恢复执行者先
    `takeOverTaskSpace("bh_<site_id>_<run_id>")`，再继续原轮次；不得并发创建第二个站点执行者。
12. 工具返回候选暂不可用、执行器异常或候选限制时，不把当前轮次算作完成；如实回报剩余目标。
    用户下一次主动发起新任务时使用新 `run_id`，`failed_retryable` 仍可重新尝试。
13. 达到目标或队列确实耗尽后，先确认最后一个候选已回写，再用单独的最终 heredoc 调用
    `completeTaskSpace("bh_<site_id>_<run_id>", {keep: false})`。控制权切换等待用户继续时保留该空间。
14. 最后只汇总逐站的 `published`、明确 `pending`、`attempted_unconfirmed`、可重试失败、
    永久失败和剩余目标。不在外链执行过程中运行 Git、测试、迁移或 token 统计。

## Pitfalls

- 不把“最终点击但无回执”计为成功，也不把“待审核”写成已公开发布。
- 不因为邮箱是凭据别名就判定资料缺失；使用受控凭据填充或 Gmail 魔法链接脚本。
- 不重复点击已经点击过的候选；不在结果不明时更换执行器重提。
- 不关闭用户已有浏览器标签，只关闭当前候选创建的标签和任务空间。
- 不为每个候选创建任务空间；整个站点轮次只复用 `bh_<site_id>_<run_id>`，候选之间只复用登录态。
- 不把 ego 控制权切换写成用户手动操作或平台永久失败。
- 不在同一站点/轮次为每条候选重新委派 worker；站点执行者应自己循环领取和回写。
- 不把 `failed_final` 当成所有未来轮次永久封锁；它只表示当前站点与平台组合暂不适用，后续
  资料或平台条件变化时可单独复核。

## Verification

结束前只确认：所有已领取候选都有一次结果回写；每个候选标签已关闭；正常结束的轮次空间
已通过 `completeTaskSpace(..., {keep:false})` 关闭；逐站进度和结果分类已返回。不要为验证再
打开网页或重复领取候选。
