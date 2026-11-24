# Ambition Orchestrator

Ambition 是一个由 Python 标准库实现的任务编排器。它接收代码、文档和只读查询任务，在本机或通过 SSH 在配置的主机上运行执行者，并记录验收、复核与交付结果。

## 架构

- `amb.py` 提供命令行、磁盘任务队列、状态机和步骤运行器。`tick` 推进队列，读取目标主机的执行状态，并在主机恢复可达后继续任务。代码在独立 Git worktree 中执行，文档使用单独的工作目录和 Git 仓库。启用 `runner_wake` 后，新运行器写入 `exit` 时触发一次 tick。远程步骤通过 SSH 等待该文件，连接中断时由每分钟 cron 补跑。
- L1 用于直接执行，L2 增加复核，L3 先规划再按依赖图运行子任务。规划器校验依赖下标和循环依赖。无依赖的子任务并行，有依赖的子任务基于全部前置结果继续。父任务集成全部分支并集中复核文档子任务。顶层 L1/L2 仅在执行者明确输出 `"needs_plan": true` 时转 L3。升级链用尽时返回带原因的 `blocked`。规划默认限时 20 分钟。
- 复核提示包含执行者总结、未验证项和输出末尾。没有现成验收命令的代码任务会走复核。自动合并需要验收命令、当前提交通过验收、所需复核通过，以及目标仓库开启 `auto_merge`。推送需要单独启用。
- 可选的 webhook、飞书和 Linear 通道随 `tick` 发送通知。监工模式从配置的主机拉取决策请求，发送前在本地保存原条目，供失败后重试。

## 安装与试跑

使用有 Python 3.12、Git 和 Bash 的 Linux 主机。将仓库检出到 `~/amb`。可选的定时调度需要 cron 和 flock，远程执行需要 SSH。

```bash
cd ~/amb
cp -n config.example.json config.json
python3 amb.py --help
id=$(python3 amb.py new --kind doc --tier L1 --host local --exec sol --once --goal '验证本机执行链路，任务完成后生成 DONE.txt。')
```

在 `jobs/_draft/<id>/job.json` 填写 `title` 和 `user_words`，再在同目录编辑 `brief.md`。提交后手动运行一次 `tick` 启动任务，后续步骤完成时会唤醒调度器。也可手动再运行 `tick` 收集已完成的结果。

```bash
python3 amb.py submit "$id"
python3 amb.py tick
python3 amb.py tick
python3 amb.py status "$id"
python3 amb.py result "$id"
```

示例使用随仓库提供的本地测试执行者、规划器和复核者，执行成功后会生成 `DONE.txt`。也可用 `--tier L3` 试跑文档依赖链。实际任务请配置模型命令。若使用 Claude 包装器，可在 `hosts.<host>.claude_launcher` 设置其启动命令。需要每分钟自动推进时，运行 `bash install-cron.sh`。该脚本使用默认的 `~/amb` 布局。单独部署时可通过 `AMB_ROOT` 指定配置和任务状态目录。

## 配置

从 `config.example.json` 创建本地 `config.json`。仓库的示例关闭外部通知和自动合并。

| 配置 | 用途 |
| --- | --- |
| `hosts` | 本机的 `ssh` 为 `null`。远程主机使用部署者的 SSH 配置别名，并在该主机上提供 `work_root`。 |
| `roles`、`planner` | 设置 `model` 和 `cmd`，需要时用 `host_commands` 按主机覆盖。执行者接收提示词文件和标准输入，可以写入 `{out_file}` 或输出到标准输出。接入查询需配置 `ask`。新部署用示例的 `planner: plan.opus`，将模拟命令替换为 Opus high 入口。可将 `planner` 切换到已配置的 `plan.fable`。沿用未设置 `planner` 的旧配置时，编排器仍查找 `plan.fable`。 |
| `ladder`、`reviewer` | 设置失败后的执行者顺序和对应复核角色。 |
| `repos` | 以 `<host>:<repo>` 为键设置仓库权限。验收命令以执行账号运行，应来自可信任务。模型角色也使用目标主机上的执行账号权限。 |
| `webhook`、`feishu`、`linear`、`supervisor` | 按部署需要启用。Webhook 凭据可通过权限受限的 `config_file` 读取。Linear 默认 dry-run，启用真实写入时使用 `api_key_file`。 |
| `runner_wake`、`sentinel` | 分别启用步骤退出唤醒和按规则巡检。 |
| `speed_supervisor` | 默认关闭。启用时设置 `true`，并配置 `kml-dev` 主机和该主机上的 `/opt/amb/claude-full`。 |

执行者的最后一条消息应包含 `{"summary":"结果摘要","unverified":[]}`。复核者输出 `{"verdict":"PASS","sha":"<候选提交 SHA>","findings":[]}`。步骤提示词会提供对应的完整协议。

## 速度巡检

- 哨兵检查就绪后排队超过 2 分钟、规划超过 8 分钟、执行输出 10 分钟未增长、复核超过 5 分钟和等人超过 10 分钟的任务。每单每阶段只记录一次，事件位于 `jobs/_sentinel/events/`。
- 督促模块可响应新哨兵事件，有活跃任务时每 15 分钟复查，每小时最多运行四次。命令在配置的主机上通过 `timeout` 运行，限时 300 秒，通过标准输入接收提示和任务元数据。部署时使用关闭工具的 Opus medium 命令，输出最后一行 JSON，包含 `actions` 和 `reports` 数组。
- 督促模块只自动补 tick 和启动重新核实就绪的子任务。报告经启用的监工 outbox 转发，需设置 `supervisor.host`、`supervisor.outbox` 和启用时间 `supervisor.since`（ISO 8601）。去重与重试状态位于 `jobs/_speed/seen.json`。

## 直通工具

将仓库中的 `amb-direct-run` 安装到 PATH，或者从检出目录以 `./amb-direct-run` 调用。用于维护编排器和配套 skill、文档，也可在编排器不可用时运行。每次启动使用新目录和会话。有依赖时每 5 秒检查前置任务的 `exit`，全部成功后开始，任一失败时退出。可以多次传入 `--after`。

```bash
./amb-direct-run run prepare --prompt ./prepare.md
./amb-direct-run run publish --prompt ./publish.md --after prepare
./amb-direct-run send publish '把验证结果写入 result.md'
```

`run` 可在 tmux 中持续运行。默认使用 PATH 中已配置的 Codex，可用 `--agent claude` 切换，并用 `--model` 和 `--effort` 指定模型与推理强度。`--prompt -` 从标准输入读取。工具以非交互权限运行智能体，沿用启动账号的权限，输入应来自可信维护者。

任务目录默认为 `~/amb-direct/<任务名>`，可通过 `AMB_DIRECT_ROOT` 设置根目录。`send` 将指令写入 `inbox`，执行者可在执行过程中读取。当轮结束后，未确认的指令进入同一会话的下一轮。结果见 `out.md`，状态见 `status.json`，Codex 事件见 `log.txt`，错误输出见 `err.log`，最终退出码见 `exit`。

## Skills

[ambition-dispatch](skill/ambition-dispatch/SKILL.md) 定义如何按难度选择执行者、填写任务、读取进度和回报证据。将该目录放入所用代理的 skill 搜索路径，并确认代理能够调用 `amb.py`。其通知处理约定也可用于已配置的 webhook 接收端。

[autoresearch](skill/autoresearch/SKILL.md) 定义怎样组织跨多轮推进的研究课题。安装方式相同。用到的 `ar` 运行时需由部署者另外配置。

Linear 应用集成可通过 `linear.app_cmd` 接入，飞书结果投递可配置 `AMB_JOB_DELIVER_HELPER`。部署时可将 GPU 规则文件路径设置在 `AMB_H200_RULES`，由编排器按任务内容引用。

使用 pytest 运行框架测试。

```bash
python3 -m pytest -q
```
