# 交付：deliver 与 endgame

`delivery` 能力后两个阶段的操作契约。不可逆动作集中在这里，所以两个 gate 都在这。

## Step: D01 影响面复核

> - **输入**：`K01` 的改动 + `K02` 的验证结果
> - **输出**：调用方清单 + 每个「为什么不破」的说明
> - **完成标准**：`all_of [K01, K02]`（引擎核对，不是自述）

列出这次改动会波及的调用方，对每个说明为什么不破。

要点是**方向**：不是「我改的地方测过了」，而是「**依赖我改的地方**的那些地方为什么还好」。
前者 `K02` 已经覆盖了；这一步的价值全在后者。

找调用方用工具，不要靠记忆——一个没被 grep 到的调用方就是一次线上事故。

## Step: D02 提交前确认（gate）

> - **门类型**：`affirm` —— 需要真人确认，且必须有 witness 背书
> - **完成标准**：`gate_recorded`
> - **守卫**：`commit` / `publish` 两个动作由本 gate 保护（未记录 → `harness guard` exit 4）

🚦 **停在这里。** 把改动摊给人看，然后**结束这一轮**。

不要在同一轮里自己记录这个 gate。witness 会发现「距上次 gate 没有新的人类发言」并
exit 3 拒绝。**这不是障碍，这就是这个 gate 的全部意义**——一个 agent 记录自己的
确认，等于自己批自己的作业。

人回复之后：

```
harness gate --run <id> --step D02 --decision affirm --evidence "<他们的原话>"
```

`--evidence` 写**原话**，不要写你的转述。转述会把「可以，但先看看 X」变成「可以」。

## Step: D03 提交并发布

> - **输入**：`D02` 已记录
> - **输出**：`commit_sha` 证据
> - **失败时**：`guard` 返回 exit 4 → 说明 `D02` 没记录，回去走 gate，不要绕过

提交 + 发布。`D02` 未记录时外部 hook 会拿到 exit 4 直接拦住这一步。

被拦住时**不要去改 guard 或伪造 gate**。拦你是对的，见 topic `never-forge-a-gate`。

## Step: E01 等待外部检查结论

> - **输出**：`checks_terminal` 证据
> - **失败时**：还在跑 → 继续等，不要落证据
> - **完成标准**：证据存在

等外部检查（CI / 静态分析 / review）走到**终态**再往下。

「还在跑」不是终态。把未跑完当通过，是最常见的假完成——而且它特别隐蔽，因为那一刻
看起来什么都没错。判断终态的原则见 topic `terminal-not-optimistic`。

## Step: E02 关单确认（gate）

> - **门类型**：`affirm`
> - **守卫**：`close_task` 由本 gate 保护

🚦 关单不可逆，先让人确认。同 `D02`：必须是真实的人类回复。

## Step: E03 收尾

> - **门类型**：`preauth:autopass_cleanup` —— 可用 config 预授权，替代人工确认
> - **完成标准**：`no_open_violations`（这个 run 必须没有任何 violation）

清理临时产物、归档。

预授权路径：先 `harness config --set autopass_cleanup=true`，之后
`harness gate --step E03 --decision preauth` 才会被接受。

**不要从进度推断预授权。** 「都走到这一步了应该算授权了吧」这种推断会 exit 3 并记一条
violation——而这一步的 completion predicate 恰好是 `no_open_violations`，所以推断一次
就把自己锁死了。这个咬合是故意的。
