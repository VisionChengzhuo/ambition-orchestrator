## 结论先行

1. **分档**：Ambition 的三档把"流程要多重"和"谁来执行"捆在了一起，应拆成两个独立字段。用户的质疑是对的：L3 的子任务应按难度分给 Sol 或 Opus，并且默认给 Sol。
2. **实现**：编排器用 Python（只用标准库）写成一个文件，Bash 只做远端包装和检查脚本；cron 每分钟跑一次 tick，不用常驻进程或 inotify；编排器只跑在一台机器上，其他机器经 SSH 被调用；Ambition 只在终态被 webhook 唤醒一次。

---

## 1. 分档

### 1.1 Ambition 的分档问题在哪

- **"重要"和"难"不是一回事。** 生产环境改一行配置很重要但不难，需要的是复核，不是 Opus。一次性脚本的棘手重构很难但不重要，需要强执行者，未必需要复核。L2 写成"较难/重要 → Opus 干"，两种情况都会配错。
- **"Opus 5.5 强于 Sol"的证据不够硬。** 调研里约 20 条相关帖子多是个人体感，还有榜单反例（Sol 6.1 59.2，Opus 5.5 58.7）。调研里唯一稳的结论是异家互审。所以分档不该建立在"Opus 更强"上。
- **我建议的起手原则是：验收越弱，起手模型越强。** 验收全是命令时，Sol 做错了脚本会拦住，代价只是一次失败后自动升级。验收靠人或模型判断时，做错了不会被发现，才值得一开始就用强模型。

### 1.2 两个字段

**流程档（按风险和可验收性定）**

| 档 | 流程 | 进入条件 |
|---|---|---|
| L1 | 执行 → 脚本验收 | 单机、可回滚、验收全是命令 |
| L2 | L1 + 异家只读复核 | 会上线或影响用户数据；验收不能全写成命令；diff 碰了测试或验收脚本（由脚本检测，强制升 L2） |
| L3 | 规划 → 拆成子任务（各自走 L1 或 L2）→ 集成验收 → 终审 | 目标模糊、跨机器、交付物超过 3 个、不可逆、或同一任务已失败 2 次 |

**执行者（按难度定，默认 Sol）**

| 执行者 | effort | 进入条件 |
|---|---|---|
| Codex Sol | medium | 默认 |
| Claude Opus 5.5 | medium，失败重试时 high | 验收写不成命令（文档、调研、设计）；原因不明的排查；跨多个模块的改动；规划方标了 `exec: opus`；Sol 已失败一次 |

之前写的"Opus low"只适合复核和文字活。既然因为难才选 Opus，再用 low 就自相矛盾。

**复核者：永远和执行者不同家**

| 执行者 | 复核者 |
|---|---|
| Sol | Opus 5.5 low，只读 |
| Opus | Codex Sol medium，只读；不可逆或生产变更用 Astra low |

**规划者**：Fable 5.2 或 Astra，effort high。规划只跑一次，错了所有子任务都要返工，不值得省。

二选一我没有证据。我自己是 Fable，这条建议请打折看。实际按"哪台机器上哪个 CLI 稳定可用"选一个做默认。不可逆的 L3 再让另一家只读挑一次计划的毛病，其余情况不做双规划。

### 1.3 回答用户：L3 里 Codex 能不能执行

能，而且应该是默认。理由：

1. **规划的作用就是把难目标变成边界清楚、验收明确的小任务，这正是 Sol 适合的输入。** 规划之后还全部交给 Opus，等于规划白做。
2. **异家互审要求两家都参与执行。** 全由 Opus 执行，Codex 就成了唯一复核方和唯一额度瓶颈。
3. **标错的代价低。** 子任务标成 Sol 但其实难，脚本验收失败后自动升到 Opus，只损失一次尝试。

两处我不完全同意"按难度细分"：

- **难度只分两级（`sol` 或 `opus`），不要更细。** 规划模型对难度的估计本身不可靠，写死规则是"除非命中上表条件，否则 sol"。
- **紧耦合的子任务不要拆给两家。** 改同一模块、互相依赖接口的几个子任务交给同一执行者串行做，否则容易风格和接口漂移（调研里有人提到，证据弱，但防范成本低）。

**L3 终审**：全部子任务完成后跑父任务的集成验收，再由与规划者不同家的模型做一次只读终审。清单第一条是"结果是否回应用户原话"。

**升级链**（沿用总尝试上限 3）：

- Sol 起手：Sol → Opus（附失败证据）→ 重新规划后 Opus → blocked。
- Opus 起手：Opus → Opus high（附失败证据）→ 重新规划 → blocked。
- 复核 FAIL 先退回原执行者改一轮，仍 FAIL 才升级。
- 连续两次失败签名相同，直接跳到下一级。

**用自己的数据修正分档**：编排器每次尝试记录模型、任务类型、是否一次通过。攒够约 30 个任务后由脚本出一张表，据此改配置里的进入条件。这比 X 上的体感可靠。

---

## 2. 交接硬编码

### 2.1 语言：Python 编排，Bash 包装

纯 Bash 不够。编排器要做 JSON 读写和校验、状态迁移、超时、原子写、失败签名、HTTP 回调，Bash 写这些靠 jq 和引号转义，`set -e` 的坑会让状态机静默走错，而这里最不能静默出错。

- **Python 3 标准库**：单文件约 300 行，无依赖。只需装在编排器所在的一台机器上。
- **Bash**：只用于远端启动包装、验收命令和 `<REDACTED_PATH>` 的检查脚本。
- **Node**：没有优势，还多一个运行时和 node_modules。除非以后要用 SDK 直接调模型，否则不选。

### 2.2 编排器放哪、谁调度

**放 box，前提是 box 24 小时在线（请确认；否则放 VPS）。** 理由：

- box 已经有到各机器的 SSH 通路。放 VPS 意味着给一台公网机器新增通往 Mac 和 dev 的钥匙，攻击面变大。
- 终态唤醒 Ambition 是本机的事。

VPS 的 `<REDACTED_PATH>` 巡检保持独立，不依赖编排器。互相监控只用已有的单向通路：编排器每次 tick 往 VPS 推一个心跳文件，`<REDACTED_PATH>` 加一个检查看它是否超过 5 分钟没更新。

**调度：cron 每分钟一次 tick 加 flock。**

```
* * * * * flock -n ~/amb/.lock python3 ~/amb/amb.py tick
```

- 每次 tick 无内存状态，只读各任务的 `state.json`，看当前步骤是否结束，结束就启动下一步。编排器崩溃或 box 重启后，下一分钟自动接上。
- `amb submit` 结束时立即触发一次 tick，不用等一分钟。
- 不用常驻进程：内存状态会丢，还要自己做保活。
- 不用 inotify：它跨不了机器，重启会丢事件，而任务本身以十分钟计，一分钟轮询延迟无所谓。
- systemd timer 等价可用。你们已经在用 cron，就少引入一样东西。
- Astra 提的 SQLite 加租约暂时不需要：只有一个编排器进程，flock 已经保证不会重复领取。

### 2.3 目录和格式

原则：**机器读的用 JSON，模型读的用 Markdown，两者分开。** 不用 YAML，标准库解析不了。

```
box:~/amb/
  amb.py   config.json            # config：主机别名、角色→命令模板、限额
  jobs/<REDACTED_ID>/
    job.json    brief.md          # Ambition 经 amb submit 写入，之后只读
    state.json                    # 只有编排器写
    plan.json                     # L3：规划产出
    sub/01/ sub/02/               # L3 子任务，结构同普通任务
    attempts/1/
      exec/   {prompt.md, out, log, exit}
      accept/ {acceptance.json, log}
      review/ {out, exit}
      failure.json
    result.json  result.md        # 编排器生成；Ambition 只读 result.md
目标机:~/amb-work/<REDACTED_ID>/a1/<step>/   # 远端工作区，按尝试编号隔离
```

**job.json**

```json
{
  "id": "<REDACTED_ID>", "title": "修复 <REDACTED_PLATFORM> 导出超时", "linear": "KML-212",
  "tier": "L2", "host": "dev", "repo": "<REDACTED_PATH>", "exec": "sol",
  "accept": [
    {"id": "A1", "cmd": "make test-export"},
    {"id": "A2", "cmd": "./scripts/export_bench.sh samples/500mb.<REDACTED_PLATFORM> --max-seconds 120"}
  ],
  "protect": ["tests/**", "scripts/export_bench.sh"],
  "limits": {"step_minutes": 45, "max_attempts": 3},
  "deadline": "2026-11-20T02:00:00+08:00"
}
```

`brief.md` 放用户原话、解释与假设、可改和不可动的范围。

**result.json**（编排器写，执行方无权写）

```json
{
  "id": "<REDACTED_ID>", "state": "done", "attempt": 2,
  "exec_model": "claude-opus-5-5", "review_model": "<Sol 完整模型 ID>",
  "sha": "a1b2c3d",
  "accept": [{"id": "A1", "result": "pass"}, {"id": "A2", "result": "pass"}],
  "review": {"verdict": "PASS", "sha": "a1b2c3d"},
  "summary": "导出改为流式写入，500MB 样本 48s",
  "unverified": ["未在生产数据上跑过"],
  "needs_human": null
}
```

几条硬规则：

- **模型不写交接文件。** 执行、复核、规划三种角色都把最后一条消息输出为 JSON，由编排器从 stdout 解析并校验必填字段。解析失败算一次 `bad_output` 失败。这样复核方可以是真正的只读沙箱。
- **每次尝试一个 git 分支**（`amb/<REDACTED_ID>/a2`）。验收脚本记录 `git rev-parse HEAD`，复核结论里必须带同一个 sha，不一致就不算通过。
- **旧尝试无法提交终态。** 编排器只读当前尝试编号的目录。
- **配置里写完整模型 ID，不写别名。** dev 上的 `fable` 别名指向 5.1 就是例子。每次尝试把实际模型 ID 记入结果。
- **默认交付到分支为止。** 合并和上线由 job.json 里的字段显式指定，不可逆的落地动作仍然交给人。

### 2.4 示例流程（L2，Sol 起手）

| 时间 | 发生了什么 | 由谁完成 |
|---|---|---|
| 14:00 | 用户说"导出老超时，修一下"。Ambition 写好 job.json 和 brief.md，执行 `amb submit`，在聊天里回"已接单" | Ambition |
| 14:00 | 探测 dev 可达，在 dev 上后台启动 Sol，状态变为 exec | tick |
| 14:18 | 发现 `exit` 文件，取回输出，在 dev 上启动验收脚本 | tick |
| 14:21 | A1 通过，A2 失败（140s）。写 failure.json，尝试编号加 1，执行者换成 Opus，提示词附上 A2 的输出 | tick |
| 14:47 | Opus 完成，验收全过，记录 sha=a1b2c3d。启动 Codex 只读复核，输入是 brief、`git diff base..a1b2c3d`、acceptance.json | tick |
| 14:53 | 复核 PASS 且 sha 一致。写 result.json 和 result.md，覆写 Linear 状态块，发飞书一行，调 webhook | tick |
| 14:53 | 被唤醒，读 result.md，回话："导出修好了：500MB 样本 48s，测试全过，commit a1b2c3d。Sol 首次未达标，已自动换 Opus。未在生产数据上跑过。" | Ambition |

14:00 到 14:53 之间 Ambition 没有被调用过。

L3 的区别只有两处：第一步是规划角色产出 `plan.json`（子任务列表，每个带 host、repo、exec、depends_on、accept）；编排器校验后把子任务生成到 `sub/` 下，按依赖串行跑同一套状态机。校验内容是每个子任务至少有一条验收、主机在配置里、父任务验收未被改动。

### 2.5 跨机器

- 所有机器对编排器来说只是 `~/.ssh/config` 里的别名。Mac 那条配 `ProxyCommand cloudflared access ssh --hostname %h`，编排器代码里没有任何机器特例。
- **远端步骤一律后台启动并写退出码文件，由编排器轮询。** 不依赖 SSH 长连接，Cloudflare 隧道断了任务照跑。
- 不依赖远端的 `timeout` 和 `setsid`（macOS 默认没有）。超时由编排器按 pid 去杀。
- 探测不通时：
  - 未开始的任务进 `waiting_host`，通知一次。
  - 执行中的任务原地暂停计时，记录"不可达"，不推断为关机。
  - 恢复后若 `exit` 文件在，就继续；若进程和 `exit` 都不在（机器重启过），按基础设施故障重启该步骤，不计入尝试次数，最多 2 次。

### 2.6 Ambition 在哪个点收到结果

只有三种事件调 Grok Bot webhook 唤醒 routine：

- 进入终态（done、blocked、failed）。
- 首次进入 `waiting_host`。
- 截止时间到了仍未进入终态。

载荷只有 `{job_id, state, result_path}`，Ambition 读 `result.md` 后回话。webhook 没返回 2xx 时 `notified` 保持 false，下一次 tick 重发。Linear 状态块和飞书 FYI 由编排器直接发，不经过 Ambition。

### 2.7 关键代码骨架

以下是骨架，不是成品。`codex exec` 和 `claude -p` 的具体参数（模型、effort、只读沙箱、输出最后一条消息）我没有在你们的 CLI 版本上核对，落地时先用 `--help` 确认，再写进 config.json 的命令模板。

```python
#!/usr/bin/env python3
# amb.py —— 用法：amb.py submit <job_dir> | amb.py tick
import json, os, subprocess, sys, time, hashlib, pathlib, urllib.request

ROOT = pathlib.Path.home() / "amb"
CFG = json.load(open(ROOT / "config.json"))
TERMINAL = {"done", "blocked", "failed"}
LADDER = {"sol": ["sol", "opus", "replan"], "opus": ["opus", "opus-high", "replan"]}
REVIEWER = {"sol": "review.opus", "opus": "review.codex", "opus-high": "review.codex"}

def ssh(host, script, timeout=30):
    try:
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, "bash", "-s"],
            input=script, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None

def save(job, st):                                   # 原子写
    tmp = job / "state.json.tmp"
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    os.replace(tmp, job / "state.json")

def wd(job, st):
    return f"~/amb-work/{job.name}/a{st['attempt']}/{st['step']}"

def start(job, st, step, host, cmd, prompt=""):
    """在目标机后台启动一步：写 prompt 和 run.sh，结束时原子写 exit。"""
    st.update(step=step, host=host, started=time.time())
    d = wd(job, st)
    ssh(host, f"""mkdir -p {d} && cd {d}
cat > prompt.md <<'AMB_EOF'
{prompt}
AMB_EOF
cat > run.sh <<'AMB_EOF'
echo $$ > pid
( {cmd} ) > out 2> log
echo $? > exit.tmp && mv exit.tmp exit
AMB_EOF
nohup bash run.sh >/dev/null 2>&1 &""")
    save(job, st)

def poll(job, st):
    r = ssh(st["host"], f"cd {wd(job, st)} && (cat exit 2>/dev/null || "
            "(kill -0 $(cat pid) 2>/dev/null && echo RUNNING || echo GONE))")
    if r is None or r.returncode == 255:
        return None                                  # 不可达，不是失败
    return r.stdout.strip()

def fetch(job, st):                                  # 取回 out/log，作为证据落盘
    local = job / "attempts" / str(st["attempt"]) / st["step"]
    local.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rsync", "-a", f"{st['host']}:{wd(job, st)}/", str(local)])
    return local

def last_json(text):                                 # 模型最后一条消息里的 JSON
    i = text.rfind("{")
    while i >= 0:
        try:
            return json.loads(text[i:text.rfind("}") + 1])
        except ValueError:
            i = text.rfind("{", 0, i)
    return None

# ---------- 状态迁移 ----------
def begin_exec(job, spec, st):
    role = CFG["roles"]["exec." + st["exec"]]        # 命令模板里写完整模型 ID
    prompt = (job / "brief.md").read_text() + prior_failures(job, st)
    cmd = (f"cd {spec['repo']} && git checkout -B amb/{job.name}/a{st['attempt']} "
           f"&& {role['cmd']}")
    start(job, st, "exec", spec["host"], cmd, prompt)

def after_exec(job, spec, st, rc, local):
    if rc != 0:
        return fail(job, spec, st, "exec_crash")
    lines = [f"cd {spec['repo']}", "echo SHA $(git rev-parse HEAD)"]
    lines += [f"( {a['cmd']} ) >&2; echo {a['id']} $?" for a in spec["accept"]]
    start(job, st, "accept", spec["host"], "\n".join(lines))   # 验收由脚本跑

def after_accept(job, spec, st, rc, local):
    res = dict(l.split() for l in (local / "out").read_text().splitlines())
    st["sha"] = res.pop("SHA")
    failed = sorted(k for k, v in res.items() if v != "0")
    if failed:
        return fail(job, spec, st, "accept:" + ",".join(failed))
    if spec["tier"] == "L1" and not touched_protected(job, spec, st):
        return finish(job, spec, st, "done")
    role = CFG["roles"][REVIEWER[st["exec"]]]        # 异家、只读
    start(job, st, "review", role.get("host", spec["host"]),
          f"cd {spec['repo']} && {role['cmd']}", review_prompt(job, spec, st))

def after_review(job, spec, st, rc, local):
    v = last_json((local / "out").read_text()) or {}
    if v.get("verdict") == "PASS" and v.get("sha") == st["sha"]:
        return finish(job, spec, st, "done")
    fail(job, spec, st, "review", evidence=v)

def fail(job, spec, st, kind, evidence=None):
    sig = hashlib.sha1(kind.encode()).hexdigest()[:8]
    adir = job / "attempts" / str(st["attempt"])
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "failure.json").write_text(json.dumps(
        {"kind": kind, "sig": sig, "evidence": evidence}, ensure_ascii=False))
    st["rung"] += 2 if sig == st.get("last_sig") else 1   # 同样的失败跳过同级
    st["last_sig"] = sig
    ladder = LADDER[spec["exec"]]
    if st["attempt"] >= spec["limits"]["max_attempts"] or st["rung"] >= len(ladder):
        return finish(job, spec, st, "blocked")
    st.update(attempt=st["attempt"] + 1, exec=ladder[st["rung"]], step=None)
    save(job, st)                                    # 下一次 tick 起新尝试

def finish(job, spec, st, state):
    st["state"] = state
    write_result(job, spec, st)                      # result.json + result.md
    update_linear_block(spec, st)
    send_feishu_once(spec, st)
    save(job, st)

def notify(job, st):                                 # 唤醒 Ambition，失败则下轮重试
    if st.get("notified"):
        return
    body = json.dumps({"job_id": job.name, "state": st["state"],
                       "result_path": str(job / "result.md")}).encode()
    try:
        req = urllib.request.Request(CFG["webhook"], body,
                                     {"Content-Type": "application/json"})
        st["notified"] = urllib.request.urlopen(req, timeout=10).status < 300
    except OSError:
        pass
    save(job, st)

AFTER = {"exec": after_exec, "accept": after_accept, "review": after_review}

def tick_job(job):
    spec = json.load(open(job / "job.json"))
    st = json.load(open(job / "state.json"))
    if st["state"] in TERMINAL:
        return notify(job, st)
    if time.time() > deadline_ts(spec):
        return finish(job, spec, st, "failed")       # 无声失败兜底
    if st["step"] is None:
        if ssh(spec["host"], "true") is None:
            return mark_waiting_host(job, st)        # 只通知一次，不计失败
        if st["exec"] == "replan":
            return begin_plan(job, spec, st)
        return begin_exec(job, spec, st)
    res = poll(job, st)
    if res is None:
        return                                       # 不可达：暂停，不计时
    if res == "RUNNING":
        if time.time() - st["started"] > spec["limits"]["step_minutes"] * 60:
            ssh(st["host"], f"kill $(cat {wd(job, st)}/pid)")
            fail(job, spec, st, "timeout:" + st["step"])
        return
    if res == "GONE":
        return infra_restart(job, spec, st)          # 不计尝试，最多 2 次
    AFTER[st["step"]](job, spec, st, int(res), fetch(job, st))

if __name__ == "__main__":
    if sys.argv[1] == "tick":
        for job in sorted((ROOT / "jobs").iterdir()):
            try:
                tick_job(job)
            except Exception as e:                   # 单个任务出错不拖垮其他任务
                (job / "orchestrator_error.log").open("a").write(f"{time.ctime()} {e}\n")
        push_heartbeat_to_vps()
```

未展开的辅助函数（`prior_failures`、`review_prompt`、`touched_protected`、`write_result`、`begin_plan`、`infra_restart`、`mark_waiting_host` 等）都是纯文件操作或一次 API 调用，没有设计上的难点。

---

## 需要你确认的三件事

1. **box 是否 24 小时在线。** 不是的话，编排器放 VPS，并接受 VPS 持有通往 dev 和 Mac 的钥匙。
2. **默认规划者选 Fable 5.2 还是 Astra。** 我没有证据区分，建议按 CLI 可用性定，由你决定。
3. **默认是否只交付到分支、合并由人做。** 我建议是，至少在跑出一批可信的验收数据之前。
