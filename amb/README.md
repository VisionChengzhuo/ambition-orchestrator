# amb — Ambition 派活编排器

派活 Skill `ambition-dispatch` 的唯一可编辑源头是 <REDACTED_HOST_B> 上的 `~/amb/SKILL.md`。Ambition 的 Skill 库加载发布副本。Ops 每次改完源头后，在 VPS 保存一份发布快照和 SHA-256，按 `<REDACTED_PATH>` 的做法发 `ambition_skill_install_v1` webhook，只带这次发布的 Skill、快照路径和校验值，请 Ambition 整段替换并回执。安装回执到达后，按同目录 `probe2.py` 的做法另发 `ambition_skill_probe_v1`，从新会话加载的 Skill 原样回写正文，用 `cmp` 和 SHA-256 与源头正文核对。历次发布脚本中的旧稿路径仅供追溯，每次发布使用当次快照路径和独立事件 ID。

`amb.py` 和同目录模块仅使用 Python 3 标准库。步骤结束后触发 `amb tick`，cron 每分钟补跑。

## 安装
```bash
bash install-cron.sh   # 只追加一行：* * * * * flock -n ~/amb/.lock python3 ~/amb/amb.py tick（可重复执行）
```
`AMB_ROOT`（默认 `~/amb`）决定 `config.json`、`jobs/`、`stats.jsonl` 的位置。

要连续推很多天的研究（AutoResearch 课题）不走 amb 的单件生命周期，规则见 `~/autoresearch/SKILL.md`（Ambition 的 `autoresearch` Skill 源头），循环脚本 `~/bin/ar`；带 `campaign` 的单由 `ar` 接收结束事件。

## 命令（退出码 0 成功 / 2 校验失败 / 3 找不到任务）
| 命令 | 作用 |
|---|---|
| `amb new [--kind code\|doc] [--tier L1\|L2\|L3] [--host H] [--repo R] [--exec sol\|opus\|fable] [--asked-at ISO8601]` | 在 `jobs/_draft/<id>/` 生成 `job.json`+`brief.md` 骨架，打印 id |
| `amb submit <id>` | 校验后入队并立即唤醒调度，cron tick 补跑 |
| `amb ask --host H "问题"` | 只读单步查询，无验收 |
| `amb status [id]` | 每任务一行：id state attempt exec 更新时间 |
| `amb result <id>` | 打印 `result.md`（≤6 行） |
| `amb cancel <id>` | 停止该任务和未结束子任务的运行步骤，任务置为 `cancelled`，可重复调用；单件事的 Linear 卡设 Done，第一行写原因 |
| `amb new ... [--long] [--project <Linear 项目名>] [--under <卡号>]` | 长期卡打“长期”标签、放进自己的项目；`--long` 必须带 `--project` 或 `--under`。规则见 `SKILL.md` 第 5 节 |
| `amb new ... --goal '<目标。完成标准：…>'` | 卡面“目标”一句话。会建新卡的单必填（也可写在 brief 的 `## 目标` 段），缺了 `amb submit` 退出码 2、不建卡；`--linear` 挂已有卡的单和子任务不要求。直通任务同样有 `amb-direct-run run ... --goal` |
| `amb align <文件> --title T --source S [--card TCZ-n] [--key K]` | 不在 amb 单里的带队方（AutoResearch 的 `ar`）把章程、评审决定、裁决同时发飞书并写卡评论，不等确认；同一 source+title+key 只发一次 |
| `amb progress <id> <文件>` | 执行者或带队者报进度：编排器调 `linear-app progress` 覆盖卡面“现在到哪了”“下一步”；超长正文上传成文件，未收尾编号放在末尾供 `--resolve` 使用，不发进度评论 |
| `amb send <id> "用户原话"` | 给未结束的单追加用户原话：写入 `jobs/<id>/append.jsonl`，并实时写进本单及其子单正在运行的 exec/repair 步骤目录的 `inbox`，执行者当轮读到并写 `inbox.ack`；不在执行中的单在下一次执行、修复、规划、复核的提示词里以“用户追加原话”带上。执行结束时仍有未确认条目会记进未验证项 |
| `amb send <带队单> "原话" [--all]` | 发给带队单（Fable 统领单或 `--leader`）的话默认只进带队模型的收件箱，它派出的各步不收到；`--all` 才广播 |
| `amb redo <id> --reason "用户原话"` | 原地重来：停掉正在跑的步骤，单号、执行者、Linear 卡、依赖关系都不变，尝试次数加一并立刻起下一轮；原话记进 `append.jsonl`，新提示词带“原地重来”一段（原话和上一轮工作目录）。已结束的单用 reopen；拆了子任务的父单对具体那一步 redo |
| `amb wait <带队单> [--timeout 秒]` | 带队模型等结果：收件箱有未确认的话、它派的单结束、或本单结束时立刻返回；超时打印还没结束的单 |
| `amb new ... --leader` / `--lead <带队单>` | VPS 和开发机的 Claude 带队单只加载派单、查询、收件箱等协调工具。带队单时限由 `defaults.lead_minutes` 配置。子单结束后回执写入父单收件箱和 `jobs/<带队单>/led-events.jsonl`。子单继承测试标记。 |
| `amb reopen <id> --reason "用户原话"` | 原任务记 `reopened`，新建 `<id>rN` 并附上次结果 |
| `amb delivered <id>` | Ambition 报告结果后记录首次送达，直通任务可用 `direct:<id>` |
| `amb speed [--json] [--since <ISO 8601 时间>]` | 输出各阶段耗时、返工率和近 24 小时纠正次数 |
| `amb tick` | 推进任务、补发通知、写心跳 |

## 流程

派单前用一句话对齐目标和完成标准。执行中的补充优先追加到原任务，减少取消和重派。

`queued → exec → accept → [review] → [merge] → done`；L3：`queued → planning → subs → accept(集成) → review → [merge] → done`。单进 done 只表示这一轮交齐，Linear 卡关不关另按 `SKILL.md` 第 5 节第 8 条判断。

L3 规划采用“边规划边派”。规划者每定下一个子任务就交一个，编排器立刻派出。规划步骤也接收 `amb send` 的追加原话。规划单和带队单派出一步时，编排器通过飞书发送 1–10 行思路摘要和提示词全文。Linear 父卡的“已派出一步”评论写步骤名称和思路摘要。飞书发送成功时，评论注明“完整提示词已发到飞书”。飞书发送失败或未开启时，评论注明提示词留在任务记录里。发送失败后，飞书会在后台重试，Linear 评论只发一次。规矩见 `SKILL.md` 第 3 节“带队规矩”。用户看了提意见时的处理（send / redo / cancel / 改方向）见 `SKILL.md` 第 2 节那张表。规划拆出的子任务被单独取消、且没有别的子任务依赖它时，集成时跳过（记在父单 `skipped_subs`），父单不再卡住。有依赖时 `amb cancel` 拒绝并提示用 redo。

开关（`config.json`，每次 tick 重读）：`review_events: false` 停发思路和提示词；`lead_mode: false` 停附带队规矩、`send` 恢复广播到带队子单、子单结果不回流。两个都关等于 11-22 本次改动前的行为（增量规划 `plan-update` 本身不受开关影响）。

- 执行者在 `<work_root>/<id>/a<n>/wt`（分支 `amb/<id>/a<n>`）工作，编排器统一 `git add -A && commit`。
- 步骤在目标主机后台运行：编排器把 `runner.py`、`spec.json`、`prompt.md` 以文件写到步骤目录（argv 列表，不拼 shell），
  轮询 `pid`/`exit` 文件。进程和 exit 都没了 = 基础设施重启（不加 attempt，最多 2 次）。
- `runner_wake` 使新 runner 在写入 exit 时唤醒 VPS 上的 tick。远程步骤通过已有 SSH 连接观察 exit，连接断开后由 cron 补跑。
- 普通执行、修复和复核默认没有步骤硬时限，整单仍受 `deadline` 约束。需要步骤硬时限时，可设置 `defaults.step_minutes` 或任务的 `limits.step_minutes`（分钟）。已有任务中的 `limits.step_minutes` 仍然有效，删除此字段后才继承全局默认值。已启动的步骤继续使用启动时记录的时限。带队执行仍适用 `lead_minutes`，规划和验收各有独立时限。
- 执行、规划和复核默认在输出文件 90 秒无增长时停止，并在新步骤目录重试一次。执行重试沿用已有 worktree。可用 `defaults.model_idle_timeout_s` 或任务的 `limits.model_idle_timeout_s` 调整，设为 `0` 关闭。验收命令达到自身硬超时时也在新目录重试一次。
- 开工超过 5 分钟时，`stats.jsonl` 保存调度阶段和子进程耗时。终态更新完整记录，`amb speed --json` 的 `execution_long_tail` 可查阅记录，`stage_max_minutes` 和 `stage_medians_minutes` 可核对开工到完成的最大值和中位数。
- 每次验收/复核都用新的步骤目录（`accept/<序号>-<ID>`、`review-<序号>`），前后校验 worktree 的 HEAD 等于待验 sha 且已跟踪文件无改动；候选 sha 变了（合并 base 后）会重新验收并重新复核。
- 复核：≥ L2、code 无验收或命中 `protect` 时，由异家复核者检查。复核者改工作区判失败。
- 候选 SHA 对应的明确复核问题，原执行者在原工作树修一次，重新验收和复核。其余失败写 `failure.json`，按 `ladder` 升级，同签名连败跳级，耗尽则阻塞。
- 合并六条硬条件：当前 sha 验收全过；需复核时 PASS 且 sha 一致；base 没动 fast-forward，动了先合并再重跑验收；
  主工作区检出 base 且不干净不合并；记 `pre_merge_sha`；默认不 push（`repos.*.push`）。
- 主机不可达（ssh 退出 255/超时）：排队中的任务进 `waiting_host`，attempt 不变。运行中暂停调度器的超时计时，远程 runner 继续计时。
- L3 默认由 Opus high 规划，Fable 须显式配置。子任务按 `depends_on` 调度，父任务集成所有分支。顶层 L3 可重规划一次。顶层 L2 在执行者输出布尔值 `"needs_plan": true` 后进入规划。L1、查询和子任务始终直接执行。规划超过 `plan_minutes` 后阻塞。
- L1 文档单可以直接完成。代码没有现成验收命令时可省略 `accept`，编排器只做复核，合并要求验收命令。
- `roles.<role>.host_commands.<host>` 覆写该主机的角色命令，其他主机使用角色的 `cmd`。复核提示包含执行者总结、未验证项和输出末尾的证据。

## 通知
- `notifications_since` 可设置通知生效时间，以状态迁移时间为准。
- webhook 使用 `url` 和请求头或受限权限的 `config_file`。顶层和直通任务进入 done、failed、blocked 或 cancelled 时入队并唤醒推送。载荷字段为 `type=amb_job_event`、`id`、`title`、`state`、`summary` 和 `result_path`。每条事件独立退避，2xx 回执见 `jobs/_webhook/events/sent/`。
- 飞书 FYI 向本人发送顶层任务终态。命令模板支持 `{text}` 和作为幂等键的 `{event_id}`；启用 `require_receipt` 时须收到消息 ID。
- Linear 建卡、进展和状态约定见 `SKILL.md` 第 5 节。
- webhook/Linear 请求不跟随重定向，只接受 https（http 仅限本机），响应体 ≤1MB。
- 监工：VPS 的 `tick` 只读拉取 `supervisor.outbox` 和可选的 `supervisor.pending`。启用时间以前的记录归档。此后的 JSONL 用 `needs_human: true` 或 `action: "blocked"|"needs_decision"` 明确请求处理。重复上报同一事项时保留同一个源 `event_id`。提案文件使用 `- <ISO 时间>｜提案：<内容>`。事件 `ambition_supervisor_v1` 携带稳定 ID，原条目和送达记录保存在 `jobs/_supervisor/`，先落盘再发送，源日志轮转后仍可重试。
- 每次 tick 覆写 `heartbeat`。编排和直通共享 `stats.jsonl`，记录 `asked_at`、`dispatched_at`、`started_at`、`finished_at` 和 `delivered_at`。

## 速度巡检

`--asked-at` 接收带时区的 ISO 8601，默认建单时间。报告后调用 `amb delivered`。返工率按顶层和直通终态统计，重开链算一单。计取消、重开、多轮或未解决的复核失败。同轮修复通过只留原因，见 `result.json` 和 `stats.jsonl`。近 24 小时用户纠正从 `<REDACTED_PATH>*/corrections.jsonl` 跨快照去重。

- 顶层派发满 2 分钟仍无开始记录时，`dispatch_start_watchdog` 重派一次，记入任务状态和 `stats.jsonl`。派发时间沿用首次入队。
- `sentinel` 在 tick 中检查已就绪排队超过 2 分钟、规划超过 8 分钟、执行输出 10 分钟未增长、复核超过 5 分钟和等人超过 10 分钟。每单每阶段只记录一次，事件见 `jobs/_sentinel/events/`。
- `speed_supervisor` 通过 <REDACTED_HOST_A> 的非 root Claude 入口后台运行 Opus medium。新哨兵事件可立即触发，有活跃任务时每 15 分钟复查，每小时最多四次。只有补 tick 和启动已核实就绪的子任务可以自动执行。需要 Ambition 决定的事项经监工 outbox 转发，去重记录见 `jobs/_speed/seen.json`。

## 扩展钩子
`augment_brief(job, cfg)` 在 submit 时调用，目前原样返回；将来按 `config.context_hooks` 在任务单顶部插入“环境上下文 rev N + 卡全文”。

## 测试
```bash
python3 -m unittest discover -s tests -v   # 角色命令替换为 tests/fakes/*.sh
```

## 每单指定模型和推理档位

`amb new --exec fable --effort low` 使用 Fable 角色并显式选择 `low`。`--exec` 选择执行角色，`--model` 和 `--effort` 可各自覆盖这个角色的默认值，支持的组合由对应的 Codex 或 Claude 客户端决定。省略参数时使用配置默认值。选项写入 `job.json`，同一任务的执行重试会保留显式选择。规划器可在每个子单中分别填写 `model` 和 `effort`。

## 直通任务

`amb-direct-run run` 为每次任务创建一个新目录和会话，可在 tmux 中长期运行。没有数据依赖的任务可同时启动。有依赖时用 `--after`，每 5 秒检查前置任务的 `exit`，前置成功后立即开始，前置失败时进入 blocked。可以重复使用 `--after`。

```bash
~/bin/amb-direct-run run prepare --prompt ~/prepare.md
~/bin/amb-direct-run run publish --prompt ~/publish.md --after prepare
~/bin/amb-direct-run send publish '追加指令：完成后把验证结果写入 result.md'
```

默认用 Codex，可用 `--agent claude` 切换，也可指定 `--model`、`--effort`、`--title` 和 `--asked-at`。默认任务目录是 `~/amb-direct/<任务名>`，可用 `AMB_DIRECT_ROOT` 指定根目录。智能体沿用 VPS 直通任务的非交互权限。用 `--prompt -` 从标准输入读取指令。

直通任务将模型和档位写入 `status.json`，并在首轮和追加指令恢复会话时使用同一选择。省略的参数记为 `null`，由客户端使用其默认配置。

`send` 只追加到 `inbox` 文件，执行中的智能体可在检查点读取。尚未确认的追加指令在当前轮结束后自动送入同一会话。结果见 `out.md`，进度见 `status.json`，Codex 事件见 `log.txt`，错误输出见 `err.log`。最终退出码写入 `exit`。
