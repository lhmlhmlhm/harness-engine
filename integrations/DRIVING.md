# 驱动 harness —— 给 agent 的调用契约

这是**你**执行任务时怎么用 harness 的说明。不是项目介绍(那是 README),这里只讲调用循环、
退出码怎么读、以及哪些事不许做。

引擎路径:`~/Desktop/study/project/harness-engine/bin/harness`(下文简称 `harness`)。

## 一句话模型

流程不是散文里的一串 MUST,是一个**状态机**:每一步做完要**记录证据**,引擎按声明的判据决定
它能不能关闭。**你说做完了不算做完**——判据对状态库求值为真才算。

## 退出码就是产品

| 码 | 含义 | 你该做什么 |
|---|---|---|
| **0** | 通过 | 继续 |
| **1** | 用法错（含未知/拼错的 flag） | 修命令 |
| **2** | spec 非法 | 报给用户,不要绕 |
| **3** | **被拒绝** | 读 stderr,它会写清缺什么 + 补救命令 |
| **4** | **被守卫拦住** | 有一道门没记录。**去拿人的真确认,不要改措辞绕过** |

`3` 和 `4` 不是错误,是产品。它们的 stderr 会给出确切的补救命令——照着做,不要自己发明。

## 调用循环

```bash
harness init                                   # 一次就够（幂等）
harness abilities                              # 有哪些能力

# 开一个 run（scope 是这个能力独占的维度：代码库路径 / 方案标识 / …）
harness open <ability> --scope "$(pwd)" --run <id>
#   多变体的能力会打印 variant 及其来源（derived / explicit / default）
#   变体不可派生的能力必须显式给：--variant <值>

# 主循环
harness next --run <id>                        # 下一步是什么 + 判据要求什么
harness evidence --run <id> --step <s> --kind <k> --value <v>
harness close-step --run <id> --step <s>

# 遇到 gate 的步骤（next 会打印 gate 类型）
harness gate --run <id> --step <s> --decision affirm --evidence "<人的原话>"

# 每个阶段走完
harness summarize --run <id> --phase <p> --note "<一句小结>"

# 收尾
harness close-run --run <id> --result completed
harness audit                                  # 有没有留下 violation
```

## `next` 打印的四层,各自什么时候读

```
▶ D14  🚦 Decision gate (B/W)
  gate       affirm
  completion all_checks
    · this step's own gate, answered affirmatively        ← 判据要求什么，照它做
    · record evidence kind 'decision' ∈ ['clean','blocked','warn']
  ── directive ──                                        ← 无条件读，2 行，够你动手
  产出：一个门禁决策 —— 干净放行 / 阻塞 / 警告
  完成：决策经工具记录，并通过本层目标校验
  ── guide (phase-level, section 'D14') ──               ← 只给指针，需要细节才取
  verification.md   →  harness show --run x --step D14
  ── topics (read only if you hit trouble) ──            ← 踩坑才读
  mutation-verification-traps   →  harness show ... --topic ...
```

**不要预先 `show` 全部 guide。** 分层的意义就是 directive 够你动手,细节按需取。一个 101 步的
能力有 1,196 行散文,预载它会把上下文烧光。

## gate:两轮,不是一轮

`gate: affirm` 的步骤需要一个**你无法伪造的背书**——引擎会数「距上一次 gate 之后有没有新的
人类发言」。

正确节奏是**两轮**:

1. 这一轮:把要确认的东西摊开给人看,然后**结束这一轮**。
2. 人回话后的下一轮:`harness gate ... --decision affirm --evidence "<人的原话>"`。

在同一轮里自问自答会被识别为伪造并拒绝(exit 3),且**每次拒绝都留痕**。

⚠️ **`--evidence` 写人的原话,不要写你的转述。** 转述会悄悄改语义——把「可以,但先看看 X」
压成「可以」,一个带条件的同意变成无条件放行。

`gate: preauth:<key>` 的步骤可以由配置预授权:`harness config --run <id> --set <key>=true`
之后 `--decision preauth`。**key 没开就记 preauth 会被拒**——预授权不可推断。

## 变体(多形态的能力)

有些能力有两个形态,`open` 时钉死,之后不变。**属于另一个形态的步骤不是「可跳过」,是「不存在」**
——`enter` 和 `skip` 都会拒绝。

如果某一步记录的值与 run 的变体不符(判据 `evidence_matches_variant`),说明**这个 run 一开始
就开错了形态**:关掉它、用正确的 `--variant` 重开。**不要把记录的值改成和它一致**——那是把
「走错形态」改成「记录说我没走错」。

## 义务

条件命中的 hook 会产生**义务**。`close-run` 在有未销账义务时 exit 3:

```bash
harness obligations --run <id>
harness discharge --run <id> --hook <hook-id> --evidence "<你做了什么>"
```

义务的正文会告诉你要做什么。它落在状态库里,所以输出被截断也丢不掉。

## 不许做的事

- **不许 `--force-steps` / `--force-obligations`。** 两个 flag,授权的是两件不同的事:
  **把活留着没做** vs **把记下的承诺留着没兑现**。任一都记一条 breach 级 violation,
  要用得**先问用户**。
  没有合并的 `--force`——两者互不代替,授权一个不会顺手赦免另一个(拒绝信息会点名另一个 flag)。
- **不许为了让判据通过而改判据。** flow.yaml 是 spec;它挡住你说明的是工作没做完,不是 spec
  错了。真觉得 spec 错了 → 报给用户。
- **不许改措辞绕过守卫。** exit 4 意味着有道门没记录。绕过的正确方式只有一个:去拿人的确认。
- **不许自己去 `sqlite3` 改状态库。** 每条状态变更都有对应命令;直接写库会绕过全部校验。

## 什么时候该开 run

**任务要改文件、要提交、要建单** → 先 `harness open`,再动手。

守卫在没有 open run 时**放行**(它跑在每次工具调用之前,不能因为一个 bug 卡死正常工作)。
所以「不开 run」不会被拦住——但那样这条流程的全部保证都不存在,而事后看不出区别。
**这是你的责任,不是工具的。**

## 卡住时

```bash
harness status --run <id>       # 走到哪了、欠什么、有没有 violation
harness assert-goal --run <id> --phase <p>   # 只读：这一层的验收判据满足了吗
harness validate <ability>      # spec 本身是否合法 + 判据强度分布
```

`status` 的 `owed` 行直接告诉你还欠几步。`assert-goal` 是 `summarize` 会拒绝的那同一个问题的
只读版本——先问它,比试着 summarize 再读拒绝快。
