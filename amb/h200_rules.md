## H200 占卡与资源硬规则（编排器自动附加；用户 2026-11-21 定。与 brief 里其他说法冲突时以本节为准）
1. 两台 H200（任务 <REDACTED_ID>）的占卡常驻，任何任务都不得停占卡、kill 占卡进程、删 `/dev/shm/h200-keepalive.balloon`。占卡只占约 30% 显存、GPU 100%，其余显存直接可用。
2. 轻量任务（MVP、跑通、冒烟、联通、调试；预计单卡 GPU 利用率 <90% 或时长 <10 分钟）：直接压着占卡跑，不调用任何 release。
3. 正式实验（高峰期单卡 GPU ≥90%）：启动前在开发机执行 `python3 <REDACTED_PATH> release --role <launcher|worker|all> --gpus <卡> --minutes <分钟> --reason <名>`，然后直接起实验。默认是让卡模式：占卡不停，业务进程上卡后占卡自动暂停；业务 60 秒均值 <90% 或出现报警时占卡秒级顶上，约 5 分钟后再让回；业务结束占卡自动恢复。结束后执行 `keeper.py unhold --role <同上>`。只有单卡显存需求超过约 95 GB 才允许加 `--exclusive`（真停占卡）。
4. 有人说平台报警了：立即在开发机执行 `keeper.py alarm --on --reason "<原话>"`（16 卡立即全部占满），确认消警后 `keeper.py alarm --off`。
5. Pod 内有常驻资源监控：`/dev/shm` 用量达到上限（min(8,000,000,000 字节, tmpfs 容量)）85% 预警、95% 强杀实验进程并清掉其文件；内存达到 Pod 上限 85% 预警、95% 强杀占用最大的实验进程。实验自己控制 DataLoader worker 数、batch 和缓存，不得触线；`/dev/shm` 不放数据集、模型、缓存。
6. 结果里附 `keeper.py status` 的证据（16 卡利用率、有无 alarm）。细节见 <REDACTED_HOST_A> `<REDACTED_PATH>`。
7. 重开（reopen）或修复时，已成功的 H200 子任务复用已有结果，不重跑，除非用户明确要求。
