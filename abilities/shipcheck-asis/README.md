# shipcheck-asis

交付**自己的**代码改动：从读懂现状，到提交、发评审、等分析器、处理意见，直到收口。

这是这棵树上最重的一条流程 —— **102 步 / 8 phase / 24 stage / 5 个受守动作**。重量对应的是
交付的不可逆性：`git commit`、`cr -o`、关任务、拆工作树，这四件事做出去就收不回来。
轻量改动也走同一条流程，只是 29 个 optional 步骤里的大部分会被 `skip`。

> 引擎侧的机制（谓词、gate、guard、scope、义务、退出码）见仓库根 `README.md`。
> 这份文档只讲**这条流程**：它的形状、它的判据、它依赖外部世界的哪些东西。

## 这个 ability 解决什么

`when:` 的原话是「任务要改代码、要提交、要发评审 —— 交付**自己的**改动」。

它和另外几条的分界：

| 场景 | 走哪条 |
|---|---|
| 交付**自己**的改动（会提交、会发评审） | **shipcheck-asis** |
| 评审**别人**的一个 CR | `cr-reviewer` |
| 回看**已合入**的一批 commit 做复盘 | `cr-audit` |
| 只出方案文档、不改代码 | `plan` |

## scope

```
scope_kind:  repo
scope_match: path_prefix_or_payload
```

scope 的 key 是**一个仓库目录**。所以它是「位置型」的，用 `path_prefix_or_payload`：
一个动作既可能发生在这棵树里（cwd 落在前缀下），也可能**站在别处指名这里**
（`git -C /that/repo commit`）。只匹配 cwd 会漏掉后者，而后者是最容易被无意绕过的形状。

`scope_kind: repo` 是**跨 ability 共享的命名空间** —— 任何另一条同样声明 `repo` 的流程会和它
争同一个 key，同一个目录下开了一个，另一个会被拒。这是设计而非耦合：两条都作用于同一个仓库的
流程必须互相看见，而 `scope_lease` 是给它们裁决用的。

## 流程结构

| phase | 步数 | optional | 带 gate | stage 数 | phase goal |
|---|---|---|---|---|---|
| `context` | 26 | 3 | 1 | 5 | `phase_steps_closed` |
| `config` | 2 | 0 | 1 | 1 | `phase_steps_closed` |
| `coding` | 5 | 0 | 0 | 1 | `phase_steps_closed` |
| `verification` | 17 | 0 | 1 | 3 | `phase_steps_closed` |
| `execution` | 9 | 0 | 2 | 2 | `phase_steps_closed` |
| `observation` | 8 | 4 | 1 | 5 | `all_checks` |
| `revision` | 16 | **16** | 0 | 3 | `phase_steps_closed` |
| `cleanup` | 19 | 6 | 2 | 4 | `all_of` |
| **合计** | **102** | **29** | **8** | **24** | |

三处形状值得单独说：

- **`revision` 整层 16 步全部 optional。** 没有 findings 时它合法地一步都不跑。这是
  `optional:` 这个原语存在的原因：一个「只在有意见时才跑的整层」，用必需步骤表达不出来。
- **互斥分支**：`exclusive_groups: [[E22a, E22b]]` —— 关任务的「是 / 否」两条路，二选一。
  关掉一个，兄弟自动记 `skipped`，该组只算一次。
- **`repeatable`**：修订循环 `R01–R06` / `R08` 各 `budget: 3`，而 `R16`（冲突重基）
  **独立**计 3 次 —— 它解决的不是「意见」，所以不占修订那条预算。`E29` 的 budget 是 0（不限）。

## 判据与 gate

`harness validate shipcheck-asis` 的实测输出：

```
✅ shipcheck-asis: 102 steps, 8 phases, 5 guard(s); order ok
   uses (12): gates, guards, facts, hooks, obligations, prose, stages, phase_goals,
              exclusive_groups, repeatable, optional_steps, results
   criteria: derived×21 · value-checked×47 · record-exists×34 · UNCHECKED×0
   artifacts: 151 pinned across 102 steps (37 step(s) pin ≥2)
   capabilities: 11 declared, 0 absent
   goals: 8/8 phases  [context:derived/config:derived/coding:derived/verification:derived/
                       execution:derived/observation:derived/revision:derived/cleanup:derived]
   prose: directive 102/102 · guide 102/102 (own section 102) · topics cited 4
```

**`UNCHECKED×0`** 是这条流程唯一值得骄傲的数字：没有任何一步是「什么都不查」。
而 **`record-exists×34`** 是它的天花板 —— 那 34 步只强制「记下来了」，不裁决记的是什么。
根 README 的〈强度分层〉一节解释了为什么这两个数字要分开报。

**8 个 gate，其中两个 `strict_witness: true`**：

| 步骤 | 为什么它不接受降级 |
|---|---|
| `E09g` | 收口层入口。放它降级等于让整条清理层在没有人确认的情况下开始 |
| `E21` | 关任务 / 拆工作树那一步。两个动作都不可逆 |

`strict_witness` 的语义是：无 witness 直接 exit 3，**不记「带标记的通过」**。其余 6 个 gate
在没有 witness 时会记一条 `breach` 并放行 —— 那是刻意的分级，不是遗漏。

## providers 与 facts

9 个 provider，全部通过 `requires` 声明自己需要什么。**引擎不认识其中任何一个工具**，
它只知道「这个 provider 说它需要一个存在的文件 / 一个可执行命令」：

| provider | requires |
|---|---|
| `blast_radius` | `file: tools/run-assertions.py` |
| `decision_gate` | `file: tools/run-assertions.py`, `file: tools/classify-gate.py` |
| `review_comments` | `file: tools/analyzer-comment-classify.py` |
| `run_metrics` | `file: tools/analyze-run-metrics.py`, `file: ~/.kiro/sessions/cli` |
| `fleet_central` | `file: tools/fleet_probe.py` |
| `hot_set` | `file: ~/.kiro/skills/shared-kb/memory/memory.py` |
| `worktree_state` | `cmd: git` |
| `plan_writeback` | `file:` 由 `HARNESS_PLAN_DATA_DIR` 覆盖，缺省见 `providers.py` |
| `quality_slo` | `file:` 由 `HARNESS_QUALITY_SLO_DIR` 覆盖，缺省同上 |

**缺失不是崩溃，是一个 `Unavailable` 哨兵**：依赖它的条件求值成「不可判定」而不是 false，
因为 false 会读成「已经检查过、结论是没有」。

**这条流程的 provider 依赖作者本机的若干工具**（`~/.kiro/skills/...`、`tools/*.py`）。
换一台机器时它们会 `Unavailable`，流程仍能跑，只是那些事实取不到。

## guards

5 个受守动作，全部映射到具体步骤：

| 动作 | 保护的步骤 | 匹配什么 |
|---|---|---|
| `commit` | `E04` | `git … commit` |
| `cr_push` | `E04` | `cr … -o`（发出评审那一次调用） |
| `task_close` | `E21` | `TaskeiUpdateTask` 载荷里 `"status": "Closed"` |
| `worktree_teardown` | `E21` | `worktree-teardown.sh` / `git worktree remove` / 删 `shipcheck/*` 分支 |
| `plan_mv` | `E21` | `mv … pending/ … done/` |

`worktree_teardown` 有 **4 条 pattern** 而不是一条：同一件事有四种写法（直接跑脚本、
用解释器跑脚本、`git worktree remove`、删分支），漏掉任何一种就是一个可用的绕过路径。

## 环境与前置

- **`providers.py` 必须被批准**（`harness trust shipcheck-asis`）—— 但**只在 `abilities/`
  位于引擎树之外时**。树内的扩展文件刻意不钉定，理由见根 README 的〈上手〉。
- 上表里 `file:` 指向的工具需要真实存在，否则对应事实 `Unavailable`。
- `git` 必须在 PATH 上（`worktree_state`）。
- 想让「必须先开 run」硬性生效，还要 `harness require --add shipcheck-asis <目录>`，
  或在仓库里放一个 `.harness-required`。不做这一步时，guard 只在**已开 run**的情况下把关。

## 设计记录

下面这几节是这条流程在设计过程中做过的判断，原本写在仓库根 README 里。
它们讲的是**这条流程**怎么处理「效果类事实」「不对等的可核实性」「向协调方上报」——
不是引擎机制，所以随这条 ability 走。

#### 效果类（C3）的形态：agent 执行，引擎用只读命令独立核实

隔离一个工作树会建分支、建检出、复制一份构建骨架；拆除它会 `worktree remove` + `branch -D`
+ 带守卫的 `rm -rf`。**引擎一件都不做。** agent 跑脚本，引擎随后问世界到底怎样——这个分工
不是洁癖，它是「我隔离了」这句话唯一能被反驳的安排。

源实现自己写下了它要消除的失效模式，值得原样引用，因为它就是这里加判据的理由：

> provisioning **FAILS HARD on error rather than falling back to the shared tree**. A silent
> fallback would hand back exactly the shared-working-tree behaviour the caller asked to be
> isolated FROM, while reporting success.

一句没人核对的「已隔离」就是同一个退回上移了一层。所以两个取值各有反驳事实：

| 声称 | 反驳它的事实 | 依据 |
|---|---|---|
| `isolated` | `in_linked_worktree equals false` | 链接工作树的 `.git` 是**文件**（指向拥有它的仓库），源检出的是**目录** |
| `shared_declared` | `isolation_declared equals true` | 方案文档里声明的 `worktree_isolation`——否则它是跳过隔离的免费出口 |

不可逆的那一半由 guard 管，挂在 Y/N 关闭决策那个 gate 上。守卫**同时拦脚本与它包装的破坏性
原语**（`git worktree remove` / `git branch -D shipcheck/*`）——只认包装器的门，改个措辞就能绕过。

一处实测得来的约束：**本引擎的 matcher 没有引号感知**，防「命令里只是提到脚本名」靠的是
**命令开头锚定**。所以把锚点放宽到任意空白边界会同时拆掉那层保护（试过，它把
`echo 'run worktree-teardown.sh later'` 一起拦了）。正确形状是两条锚定规则：裸调用 +
枚举的解释器前缀。解释器可枚举，提及不可枚举。

#### 一个步骤的两半可核实性可以不对等 —— 那就分开处理，不要给不可核的那半编一个假事实

收口这一步同时做两件事：把任务系统里的单子关掉、把方案文档写回归档。**两者的可核实性差得很远。**

| | 能核吗 | 怎么办 |
|---|---|---|
| 方案文档归档 | ✅ 完全可核（文件系统） | 三个取值各有反驳事实 |
| 收口写回跑过了 | ✅ 可核（它留下两处痕迹） | 由「两处痕迹一个都没有」反驳 |
| 任务系统里已关闭 | ❌ **不可核** | **刻意不加核对**，强制来自 gate 上的 guard |

第三行是重点。读那个任务系统只有 MCP 一条路，而 provider 是子进程、调不到 MCP。这时候
**编一个「看起来在核」的事实比不核更糟**：它在 schema 里读着合理，在输出里不可证伪。所以那半
明确留空，理由写进 flow.yaml，而强制来自动作发生前就拦住它的 guard —— 不是事后自陈。

这与 C2 那边不声明 `cr_exists` 是同一条纪律：**不声明我算不出来的事实。**

方案文档那半有三处实现要点，每一处都是一种会静默出错的方式：

- **按 slug 解析，不用早先记下的路径。** 那个路径在收口时**按设计已经过时**——移动文件正是被检验
  的那个效果。读它要么找不到（把成功报成失败），要么还在原处（把没发生的移动报成成功）。
- **history 记录按包含匹配。** 写入方的命名随时间变过（205 条真实记录里只有 18 条带日期前缀），
  精确名查找会把每一条旧形状的现存记录报成缺失。
- **frontmatter 只读开头那一块。** 一份丢了 frontmatter 但正文引用了 yaml 的文档，否则会把
  引用里的片段读成它的状态。而 `## Shipped` 必须锚行首：方案正文常会提到 shipped
  （「will be shipped in a follow-up」），子串匹配会把写回的痕迹报成存在。

「目录即状态」这个不变量在真实语料上 **405/406** 成立，这是这条判据的依据。

另外给读真实位置的 provider 留了环境覆盖口（`HARNESS_PLAN_DATA_DIR` / `HARNESS_HISTORY_LOG_DIR`），
**这是安全属性不是便利**：没有它，测试只能读用户几百份真实方案文档，而一个粗心的测试会写进去——
相邻项目里发生过一次，一套测试扫掉了 772MB 真实存储。覆盖在**调用时**解析而非 import 时：
import 时读会把它冻结在进程生命周期里，并静默失效于「加载之后才设置覆盖」的调用方。

#### 上报：把「我报了」变成可被协调方反驳的声称

派发式工作的行为规范要求每到一个里程碑就上报。参照系统把这条写成散文 + 一个 fire-once
hook，于是「我报了」按它自己的话被接受——它自己的账上因此有 **162 次强制确认**和
**24 次记录在案的未到达**。协调方那边有没有东西到达，是唯一能反驳它的事实，而那个事实
从没被问过。

所以这里问它。收口步骤记一个 `fleet_report`，**三个合法值每一个都有各自的反驳事实**：

| 声称 | 反驳它的事实 |
|---|---|
| `sent`（我报了） | `fleet_event_count count_lte 0` —— 协调方那边什么都没到 |
| `unreachable`（连不上） | `fleet_reachable equals true` —— 它其实答得上来 |
| `not_applicable`（不是派发的活） | `fleet_applicable equals true` —— 开头记的是一个真任务 id |

**只核对 `sent` 会把另两个变成免费出口**，这是这套设计里反复出现的同一件事。测试因此断言
三个声称由**三个互不相同**的事实反驳：两个声称共用一个反驳，读起来像全覆盖，实际留下一个值
没被检查。

「这个 run 服务的是哪个任务」记在**开头**（`C01`）而不是收口处。收口时才声明是否适用，等于
让想跳过上报的人自己决定适用性。

四个里程碑 hook 是**提醒而非义务**，这也是刻意的：一个「我报了」的义务可以用一句散文销账，
那正是 162 次的形状。真正的强制在收口的三路核对上——那里没有散文可谈。

一处继承自源工具的判断：一个答得上话却答不出这个任务的端点是**第三种状态**，那个工具叫它
`indeterminate` 并注明「we could not look -> NO-OP is NOT granted」。它映射为**不可达**，
所以那种情况下 `sent` 同样被拒。

