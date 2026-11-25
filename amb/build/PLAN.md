## 先说四点判断

- **单文件可以，但必须串行开发。** 两家并行写模块再拼成一个文件，冲突成本高于收益，所以 5 个子任务按顺序在同一个 `amb.py` 上叠加，两家轮流实现。
- **测试由异家先写，实现方不得改测试。** 只做事后只读复核，等于实现方自己出题自己答。每个子任务：异家按本页接口写黑盒测试，实现方让它通过，异家再只读复核 diff。
- **构建期没有编排器可用。** 用一个一次性脚本 `~/amb/build/run-task.sh <Tn>` 串起写测试、实现、跑验收、复核，结论写到 `build/Tn/verdict.json`。Ambition 每个子任务只执行这一条命令、只读这一个文件。
- **修正我第二轮骨架里的一个漏洞。** 那版用 heredoc 把任务说明拼进 shell，说明文本里出现定界符就能注入命令。实现要求：提示词和脚本一律以文件传输（rsync/scp），任何用户文本不得进入 shell 字符串。

---

## A. 接口一页纸

**环境**：`AMB_ROOT`（默认 `~/amb`）决定 `config.json` 和 `jobs/` 的位置，测试靠它指向临时目录。

**子命令**（退出码：0 成功，2 校验失败且原因写 stderr，3 找不到任务）

| 命令 | 行为 |
|---|---|
| `amb new [--kind code\|doc] [--tier] [--host] [--repo]` | 在 `jobs/_draft/<id>/` 生成 `job.json` 和 `brief.md` 骨架，打印 id（`J-MMDD-NN`） |
| `amb submit <id>` | 校验后移到 `jobs/<id>/`，置 `queued`，并尝试触发一次 tick |
| `amb ask --host H "问题"` | 建 `kind=ask` 的任务：只读、单步、无验收 |
| `amb status [id]` | 每个任务一行：id、state、attempt、exec、更新时间 |
| `amb result <id>` | 打印 `result.md` |
| `amb reopen <id> --reason "…"` | 原任务记 `reopened`，写入 `stats.jsonl`，新建 `<id>r1` 并附上次结果 |
| `amb tick` | 推进所有非终态任务，补发未成功的通知，写心跳 |

**job.json**

```json
{"id":"<REDACTED_ID>","kind":"code","title":"…","user_words":"用户原话",
 "tier":"L2","host":"<REDACTED_HOST_A>","repo":"<REDACTED_PATH>","base":"main","exec":"sol",
 "accept":[{"id":"A1","cmd":"make test","timeout_s":600}],
 "checklist":["结果是否回应原话"],"protect":["tests/**"],
 "land":"merge","linear":"<REDACTED_ID>",
 "limits":{"max_attempts":3},"deadline":"2026-11-21T02:00:00+08:00",
 "parent":null,"depends_on":[]}
```

`submit` 的校验：

- `user_words` 非空；`host` 在配置里；`deadline` 在未来。
- `kind=code` 必须有 `repo` 和至少一条 `accept`。
- `kind=doc` 必须有 `checklist`，且档位不低于 L2。
- `land=merge` 只允许 `kind=code`，且该仓库在配置里 `auto_merge=true`。

**state.json**（只有编排器写，原子替换）

`state, attempt, exec, rung, run{step,host,dir,started}, base_sha, sha, last_sig, infra_restarts, unreachable_since, notified{webhook,feishu,linear}, history[{ts,from,to,why}]`

**状态机**

```
queued ──主机不可达──> waiting_host ──可达──> queued
queued ──> exec ──> accept ──> [review] ──> [merge] ──> done
queued ──(L3 或升级到 replan)──> planning ──> subs ──> accept(集成) ──> review ──> done
exec/accept/review/merge 失败 ──> 记 failure.json，attempt+1，按升级链换执行者 ──> queued
                              └─ 无下一级或达上限 ──> blocked
任何非终态 ──过截止──> failed            ask: queued ──> exec ──> done
```

- 需要复核的条件：档位不低于 L2，或 diff 命中 `protect`。
- 执行者在独立的 git worktree 里工作（`<work_root>/<id>/a<n>/wt`，分支 `amb/<id>/a<n>`），不碰主工作区。执行结束后由编排器统一 `git add -A && git commit`，sha 不依赖模型自觉。
- 主机不可达时暂停计时。进程和 `exit` 文件都不在，算基础设施重启：不加 attempt，最多 2 次。

**merge 的硬条件**（全部满足才合并，否则 `blocked`）

1. 当前 sha 的验收全过。
2. 需要复核时，复核 `verdict=PASS` 且其 sha 等于当前 sha。
3. base 没动过就 fast-forward。base 动过则先在临时分支合并，对合并结果重跑全部验收，通过后再 fast-forward。
4. 主工作区检出着 base 且不干净时不合并。
5. 合并前的 sha 记入结果，供回滚。
6. 默认不 push。

**result.json / result.md**（只有编排器写）

```json
{"id":"…","state":"done","attempt":2,"exec_model":"…","review_model":"…",
 "sha":"…","pre_merge_sha":"…","merged":true,
 "accept":[{"id":"A1","result":"pass","exit":0,"log":"attempts/2/accept/A1.log"}],
 "review":{"verdict":"PASS","sha":"…","findings":[]},
 "summary":"…","unverified":["…"],"needs_human":null,"evidence_dir":"attempts/2/"}
```

`result.md` 固定为 6 行以内：标题行（id、状态、第几次尝试、谁执行谁复核）、结论、证据、未验证、需要你、详情路径。`ask` 的"未验证"固定写"查询回答未经验收"。

**config.json**

```json
{"hosts":{"local":{"ssh":null,"work_root":"~/amb-work"},
          "<REDACTED_HOST_A>":{"ssh":"<REDACTED_HOST_A>","work_root":"~/amb-work","forbidden":["lark-cli","<REDACTED_IP>:17890"]}},
 "roles":{"exec.sol":{"cmd":"…{workdir}…{prompt_file}…{out_file}"},
          "exec.opus":{},"exec.opus-high":{},
          "review.opus":{},"review.codex":{"cmd":"…{schema_file}…"},"review.astra":{},
          "plan.fable":{"host":"local"},"ask":{}},
 "ladder":{"sol":["sol","opus","replan"],"opus":["opus","opus-high","replan"]},
 "reviewer":{"sol":"review.opus","opus":"review.codex","opus-high":"review.codex"},
 "repos":{"<REDACTED_HOST_A>:~/amb-sandbox":{"auto_merge":true,"push":false}},
 "defaults":{"max_attempts":3,"ssh_timeout_s":20},
 "webhook":{"url":"","secret":"","header":""},
 "feishu":{"cmd":"","enabled":false},
 "linear":{"api_key_file":"","dry_run":true},
 "heartbeat":"~/amb/heartbeat"}
```

- 角色命令只做占位符替换。测试时把 `cmd` 换成假脚本。
- 模型输出取最后一条消息里的 JSON：执行者输出 `{summary, unverified}`，复核者输出 `{verdict, sha, findings}`，规划者输出 `plan.json`。解析失败记为 `bad_output` 失败。
- `webhook.url` 为空时不发，`notified.webhook=false`，配置补上后下一次 tick 补发。
- Linear 缺凭据时把状态块写到 `jobs/<id>/linear_block.md`，并在文件头列出缺哪些字段。

---

## B. 子任务单

**通用约束**（每张任务单都带）

- 只用标准库，单文件 `~/amb/amb.py`，文件内按固定区块顺序排列：配置、存储、步骤运行器、状态迁移、通知、命令行。
- 测试全部是黑盒：`subprocess` 调 `python3 amb.py …`，用 `AMB_ROOT` 指向临时目录，主机用 `local`，仓库用临时 git 仓库，模型用 `tests/fakes/*.sh`。
- 实现方不得修改 `tests/`，由 `run-task.sh` 检查 diff。
- 每个子任务一个分支 `t/n`，验收全过且复核 PASS 后合入 main。

**T0 准备**（Sol 实现，不需要复核）

- 内容：`git init ~/amb`；保存 `crontab -l` 到 `build/crontab.before`；写 `build/run-task.sh`；写假脚本 `exec_ok`、`exec_bad`、`exec_hang`、`exec_touch_tests`、`exec_garbage`、`review_pass`、`review_fail`、`review_wrong_sha`、`plan_two_subs`。假复核者从提示词文件里 grep `SHA:` 行。
- 验收：`cd ~/amb && bash build/run-task.sh --selftest && test -s build/crontab.before`

**T1 命令行与存储**（测试 Sol，实现 Opus，复核 Sol）

- 内容：`new`、`submit`（全部校验规则）、`status`、`result`；配置加载；原子写；history。
- 必测用例：缺 `user_words` 返回 2；`code` 无 `accept` 返回 2；未知主机返回 2；`merge` 但仓库未开 `auto_merge` 返回 2；合法任务进入 `queued`；`result` 找不到返回 3；只引用标准库（用 `ast` 扫 import）。
- 验收：`cd ~/amb && python3 -m unittest tests.test_t1 -v`

**T2 步骤运行器与 L1**（测试 Opus，实现 Sol，复核 Opus）

- 内容：本地和 SSH 两种后台启动、轮询、取回、杀进程（不依赖 `timeout`、`setsid`）；worktree；统一提交；`queued→exec→accept→done`；超时；截止；`waiting_host`；基础设施重启；单个任务异常不影响其他任务。
- 必测用例：
  - `exec_ok` 加通过的验收，最终 `done`。
  - 验收失败时不得 `done`，且有 `failure.json`。
  - `exec_hang` 超时后被杀并计一次失败。
  - 执行中连续调用 tick 5 次，只启动一次 exec（history 无重复）。
  - 主机指向不可达地址时进入 `waiting_host`，attempt 不变。
  - 步骤运行中删掉 pid 进程，触发基础设施重启且 attempt 不变。
  - 说明文本含引号、`$()`、定界符时不被执行。
- 验收：`cd ~/amb && python3 -m unittest tests.test_t1 tests.test_t2 -v`

**T3 复核、升级链、合并**（测试 Sol，实现 Opus，复核 Sol）

- 内容：复核步骤与异家映射；失败签名与"同签名跳级"；`protect` 命中强制复核；升级链；合并的六条硬条件。
- 必测用例：
  - sol 失败后第 2 次尝试的执行者是 opus。
  - `review_fail` 时不合并。
  - `review_wrong_sha` 时不合并。
  - L1 任务改了 `tests/**` 时出现复核步骤。
  - 达到上限后 `blocked`。
  - base 前进后会重跑验收再合并。
  - 合并冲突时 `blocked` 且 base 未变。
  - 主工作区不干净时 `blocked`。
  - `pre_merge_sha` 正确。
- 验收：`python3 -m unittest tests.test_t1 tests.test_t2 tests.test_t3 -v`

**T4 结果与通知**（测试 Opus，实现 Sol，复核 Opus）

- 内容：`result.json` 和 `result.md`；webhook（空配置跳过并待补发、非 2xx 重试、带密钥头）；飞书 FYI（不超过 40 字，按 id 加状态去重）；Linear 状态块（只替换标记之间的内容，不发评论，dry-run 落文件）；心跳；`ask`；`reopen`；`stats.jsonl`。
- 必测用例：
  - webhook 为空时 `notified.webhook=false`；填上本地 `http.server` 的地址后，下一次 tick 恰好收到 1 次请求。
  - 返回 500 时会重发。
  - 飞书 `cmd` 换成记录脚本，同一状态只调用 1 次且文本不超过 40 字。
  - `linear_block.md` 含五行状态和缺失字段说明。
  - `ask` 结束后 `result.md` 含固定的未验证声明。
  - `reopen` 生成新任务并写统计。
- 验收：`python3 -m unittest discover -s tests -p 'test_t[1-4].py' -v`

**T5 L3 与安装**（测试 Sol，实现 Opus，复核 Sol）

- 内容：`planning` 步骤；`plan.json` 校验（每个子任务有验收或清单、主机合法、父任务验收不可改）；生成 `sub/NN` 并按 `depends_on` 串行；父任务集成验收和终审；`replan` 一级；`install-cron.sh`（只追加一行 `* * * * * flock -n ~/amb/.lock python3 ~/amb/amb.py tick`，可重复执行）；README。
- 必测用例：
  - `plan_two_subs` 时两个子任务按序完成，父任务 `done`。
  - 子任务 `blocked` 时父任务 `blocked`。
  - 非法 plan 记为 `bad_output` 失败。
  - 重复运行安装脚本，crontab 里只有一行。
- 验收：`python3 -m unittest discover -s tests -v && bash install-cron.sh && bash install-cron.sh && test "$(crontab -l | grep -c 'amb.py tick')" = 1 && diff <(crontab -l | grep -v 'amb.py tick') build/crontab.before`

---

## C. 端到端实测

**准备**：在 <REDACTED_HOST_A> 新建独立仓库 `~/amb-sandbox`（一个小 Python 模块加一个当前失败的单元测试），不碰其他目录。实测前由实现者用试调用核对四个角色的模型 ID 并写入配置。证据统一放在 `~/amb/e2e/<日期>/`，由 `tests/e2e_collect.sh` 汇总成 `report.md`。

| 编号 | 做法 | 通过标准 | 证据 |
|---|---|---|---|
| E0 模型核对 | 对每个真实角色各跑一次 `amb ask --host local "只回答 OK"` | 4 个任务都 `done`，`exec_model` 与预期 ID 一致（Fable 必须是 5.2） | `jobs/*/result.json` |
| E1 L1 | sandbox 修复任务，`exec=sol`，`land=merge`，真实 Sol | `done`；`A1` 通过；sandbox 的 main 含该 sha；`linear_block.md` 存在；webhook 记为待补发；飞书真实发送恰好 1 条 | `attempts/1/accept/`、`result.json`、dev 上 `git log` 输出 |
| E2 L2 加升级 | 同类任务，`tier=L2`；把 `exec.sol` 临时换成 `exec_bad`，其余角色是真实模型 | 第 1 次尝试有 `failure.json`（`accept:A1`）；第 2 次执行者为 Opus；Codex 复核 PASS 且 sha 一致；已合并；飞书为 dry-run | `attempts/1/failure.json`、`attempts/2/`、`state.json` 的 history |
| E3 重启接续 | E2 第 2 次执行期间，注释掉本项目那一行 cron 并杀掉 tick 进程，3 分钟后恢复 | attempt 仍为 2；history 里"启动 exec"只有一条；任务最终 `done` | history、`attempts/2/exec/pid` |
| E4 不可达 | 配一个指向 `<REDACTED_IP>` 的测试主机并提交任务；之后把映射改成 <REDACTED_HOST_A> | 先进 `waiting_host`，attempt 为 1，只有一条通知记录；改映射后自动开工 | history、`notified` |

不做的测试：不重启 VPS（会影响现有巡检），不测 Mac。因此"整机重启后接续"只由 E3 的进程级中断间接覆盖，报告里要写明这一点未验证。

---

## D. 风险与不同意之处

1. **自动合并。** 我接受，但建议按仓库开关（`repos.*.auto_merge`）：先只对 sandbox 开，<REDACTED_PLATFORM> 仓库等 E1 到 E3 通过并跑过一批真实任务后再开。自动合并的可靠性不会高于验收命令本身；验收只有一句 `make test` 而测试稀薄时，合并的就是没被检查的代码。
2. **单文件。** 同意，代价是开发只能串行。若 `amb.py` 超过约 1200 行，建议拆成包，这是维护问题，不影响接口。
3. **执行者权限。** 现有做法是 `--dangerously-bypass-approvals-and-sandbox` 和 `--dangerously-skip-permissions`。在 <REDACTED_HOST_A> 上，这意味着执行者有能力碰到同事的 lark-cli 和 17890 代理配置。
   - 我的建议：Codex 默认用 `-s workspace-write`，Claude 用 `--permission-mode acceptEdits`，只在确实需要的角色上放开。
   - 配置里的 `forbidden` 只是写进提示词的约束，不是强制隔离，这一点无法在本轮解决。
4. **webhook 未配置期间 Ambition 不会被唤醒。** 上线前需要你提供 URL、密钥和头名。在此之前只能由 Ambition 主动 `amb result`，属于临时例外。
5. **SSH 经 Worker 中转且每分钟轮询。** 每个活跃任务每分钟 1 到 2 次连接。tick 必须限制单次 SSH 超时，并用 `flock -n` 跳过重叠。中转抖动只会表现为暂停，不计失败。
6. **黑盒单测覆盖不到 SSH 路径。** <REDACTED_HOST_A> 这条路只有 E1 到 E4 覆盖，SSH 相关的缺陷可能到实测才暴露，T2 的工期要留余量。
7. **构建脚本是一次性的。** `build/run-task.sh` 没有重试和升级。某个子任务复核两轮仍 FAIL 时，交回给我重写该任务单，不要让 Ambition 介入调解。

---
## 附录 Conventions（实现者与测试者共同遵守，由 Ambition 补充）
- 角色命令模板占位符：{workdir}（执行目录）、{prompt_file}（提示词文件）、{out_file}（模型最终输出写入的文件）、{schema_file}（可选）。命令通过 argv 列表（shlex.split 模板后替换占位符），不经 shell 字符串拼接用户文本。
- 假角色脚本位于 tests/fakes/，调用方式：`<script> <workdir> <prompt_file> <out_file>`：
  - exec_ok.sh：在 workdir 追加一行到 DONE.txt；写 out_file = {"summary":"ok","unverified":[]}
  - exec_bad.sh：不改任何文件；out 同上（有效 JSON）
  - exec_hang.sh：sleep 3600
  - exec_touch_tests.sh：同 exec_ok，并创建 workdir/tests/touched.txt
  - exec_garbage.sh：out_file 写入 "not json"
  - review_pass.sh：从 prompt_file 找 `SHA: <40位或短sha>` 行，out = {"verdict":"PASS","sha":<该sha>,"findings":[]}
  - review_fail.sh：同上但 verdict=FAIL，findings 含一条；review_wrong_sha.sh：PASS 但 sha 写 "deadbeef"
  - plan_two_subs.sh：out = {"subs":[{"title":"s1","host":"local","repo":"<原样取自父任务>","exec":"sol","accept":[{"id":"A1","cmd":"test -f DONE.txt"}],"depends_on":[]},{"title":"s2",...,"depends_on":[0]}]}（repo 从 prompt_file 中的 `REPO: <path>` 行取）
- 提示词文件里必须包含单独成行的 `SHA: <sha>`（复核用）和 `REPO: <path>`（规划用）。
- 验收命令在 worktree 根目录执行，shell 为 bash -c；退出码 0 即 pass。
- 测试里 AMB_ROOT 指向临时目录；config.json 的 roles 命令换成 tests/fakes 脚本绝对路径。
- amb tick 对未到达时间/未结束步骤必须幂等；所有状态写 state.json 前用 tmp+rename。
