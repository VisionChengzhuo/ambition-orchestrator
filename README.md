# Ambition Orchestrator

Ambition 是一个由 Python 标准库实现的任务编排器。它接收代码、文档和只读查询任务，在本机或通过 SSH 在配置的主机上运行执行者，并记录验收、复核与交付结果。

## 架构

- `amb.py` 提供命令行、磁盘任务队列、状态机和步骤运行器。`tick` 推进队列，读取目标主机的执行状态，并在主机恢复可达后继续任务。代码在独立 Git worktree 中执行，文档使用单独的工作目录和 Git 仓库。
- L1 用于直接执行，L2 增加复核，L3 先规划再按依赖运行子任务。顶层 L1/L2 仅在执行者明确输出 `"needs_plan": true` 时转 L3。升级链用尽时返回带原因的 `blocked`。规划默认限时 20 分钟。
- 复核提示包含执行者总结、未验证项和输出末尾。没有现成验收命令的代码任务会走复核。自动合并需要验收命令、当前提交通过验收、所需复核通过，以及目标仓库开启 `auto_merge`。推送需要单独启用。
- 可选的 webhook、飞书和 Linear 通道随 `tick` 发送通知。监工模式从配置的主机拉取决策请求，发送前在本地保存原条目，供失败后重试。

## 安装与试跑

使用有 Python 3.12、Git 和 Bash 的 Linux 主机。将仓库检出到 `~/amb`。可选的定时调度需要 cron 和 flock，远程执行需要 SSH。

```bash
cd ~/amb
cp -n config.example.json config.json
python3 amb.py --help
id=$(python3 amb.py new --kind doc --tier L1 --host local --exec sol)
```

在 `jobs/_draft/<id>/job.json` 填写 `title` 和 `user_words`，再在同目录编辑 `brief.md`。提交后可手动推进两次，第一次启动执行，之后收集结果。若执行尚未完成，可以继续运行 `tick`。

```bash
python3 amb.py submit "$id"
python3 amb.py tick
python3 amb.py tick
python3 amb.py status "$id"
python3 amb.py result "$id"
```

示例使用随仓库提供的本地测试执行者和复核者，执行成功后会生成 `DONE.txt`，便于验证流程。实际任务请配置模型命令。需要每分钟自动推进时，运行 `bash install-cron.sh`。该脚本使用默认的 `~/amb` 布局。单独部署时可通过 `AMB_ROOT` 指定配置和任务状态目录。

## 配置

从 `config.example.json` 创建本地 `config.json`。仓库的示例关闭外部通知和自动合并。

| 配置 | 用途 |
| --- | --- |
| `hosts` | 本机的 `ssh` 为 `null`。远程主机使用部署者的 SSH 配置别名，并在该主机上提供 `work_root`。 |
| `roles` | 设置 `model` 和 `cmd`，需要时用 `host_commands` 按主机覆盖。执行者接收提示词文件和标准输入，可以写入 `{out_file}` 或输出到标准输出。接入查询需配置 `ask`，接入 L3 需配置 `plan.fable` 或指定 `planner`。 |
| `ladder`、`reviewer` | 设置失败后的执行者顺序和对应复核角色。 |
| `repos` | 以 `<host>:<repo>` 为键设置仓库权限。验收命令以执行账号运行，应来自可信任务。模型角色也使用目标主机上的执行账号权限。 |
| `webhook`、`feishu`、`linear`、`supervisor` | 按部署需要启用。Webhook 凭据可通过权限受限的 `config_file` 读取。Linear 默认 dry-run，启用真实写入时使用 `api_key_file`。 |

执行者的最后一条消息应包含 `{"summary":"结果摘要","unverified":[]}`。复核者输出 `{"verdict":"PASS","sha":"<候选提交 SHA>","findings":[]}`。步骤提示词会提供对应的完整协议。

## 派活 Skill

[ambition-dispatch](skill/ambition-dispatch/SKILL.md) 定义如何按难度选择执行者、填写任务、读取进度和回报证据。将该目录放入所用代理的 skill 搜索路径，并确认代理能够调用 `amb.py`。其通知处理约定也可用于已配置的 webhook 接收端。

```bash
python3 -m unittest discover -s tests -v
```
