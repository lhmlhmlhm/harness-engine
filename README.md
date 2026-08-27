# harness-engine

一个通用的 **agent 流程注册/强制** 引擎。你用 YAML 声明一条流程，换回来一组**会拒绝**的命令，
每个拒绝带一个工具可以分支的退出码。

引擎自己**不认识任何具体能力**。ship-check 式的交付流程、文档收敛流程、设计流程，
对它来说都只是 `abilities/<name>/flow.yaml` 里的一份数据。

```
harness-engine/
├── bin/harness              入口（8 行，行为全在 engine/）
├── engine/                  【基座】不含任何能力词汇
│   ├── schema.sql           5 表 1 视图，引擎自己拥有
│   ├── store.py             SQLite 访问层，所有跨界值都是不透明 TEXT
│   ├── flow.py              flow spec 加载 + 校验（= 插件层）
│   ├── predicates.py        完成谓词注册表（= 扩展点）
│   ├── proof.py             gate 防伪 witness 注册表
│   └── harness.py           CLI
├── abilities/               【能力】一个 folder 一个能力，纯数据
│   ├── delivery/flow.yaml   交付生命周期（10 步 / 4 阶段 / 3 guard）
│   └── authoring/flow.yaml  文档收敛（5 步 / 3 阶段 / 0 guard / 菱形依赖）
└── tests/                   44 个测试
```

## 快速开始

```sh
./bin/harness init                                   # 建库
./bin/harness abilities                              # 看装了哪些能力
./bin/harness open delivery --scope /path/to/repo     # 开一个 run
./bin/harness next --run <id>                         # 下一步 + 它的 directive
./bin/harness close-step --run <id> --step C01         # 关一步（不满足则 exit 3）
```

## 退出码就是产品

| 码 | 含义 | 谁消费 |
|---|---|---|
| 0 | 允许 | — |
| 1 | 用法/环境错误 | 人 |
| 2 | **flow spec 本身非法**（fail-closed，不碰状态库） | CI |
| 3 | **REFUSED** — 规则未满足（依赖未关 / 谓词为假 / gate 无背书） | 能力自己的驱动方 |
| 4 | **BLOCKED** — 受保护动作缺 gate | 外部工具 hook |

3 和 4 分开是因为受众不同。3 回答「你还不能关这一步」；4 回答「不要让这条命令跑」。
一个 hook 只需要判断 `== 4`，永远不用解析文本。

## 一条 flow 长什么样

```yaml
version: 1
ability: delivery
scope_kind: repo            # 这个 run 独占哪个维度

guards:                     # 外部 hook 会问的动作 → 保护它的那一步
  commit: D02

phases:
  - id: deliver
    title: 交付

steps:
  - id: D02
    phase: deliver
    title: 提交前确认
    deps: [D01]
    gate: affirm            # none | affirm | preauth:<config-key>
    completion:
      type: gate_recorded   # 注册在 predicates.py 的谓词名
    directive: |
      🚦 停在这里，把改动摊给人看，然后结束这一轮。
```

`step id` / `phase` 名 / `guard` 的动作名 / 喂给模型的 `directive` —— **全部是数据**。
引擎代码里一个都不出现。

## 五条设计约束，以及它们各自的来由

这个项目的设计不是凭空来的。每条约束都对应一个**在真实系统里观察到的失败**。

### ① 第一天就装两个形态不同的能力

**来由**：此前有过一次同类尝试——建了通用表（含 `agent_name` 字段）、留了 roadmap
说「让第二、第三个 agent 接进来」。结果第二个消费方始终没出现：那张「通用」表至今
**0 行**，而唯一的消费方把自己的名字**硬编码**进了写入语句。

**只有一个消费方的框架不是框架**——没有第二个用例施加压力，抽象只会朝那唯一的实现塌陷。

所以 `authoring` 存在的目的就是**给抽象施压**，它被刻意做成与 `delivery` 四处不同：
不同的 scope 维度（document 而非 repo）、**零 guard**、**菱形依赖**（B1/B2 从 A1 分叉、
在 D1 汇合，检验拓扑排序真的在工作）、以 `attest` 为主（诚实承认引擎在这些步骤上只是账本）。

### ② 能力词汇永不进引擎代码

**来由**：同一个系统里积累了 **575 处** step id 引用、**102 个**唯一 id，散落在约
**50 个函数**里。没人是恶意加的——每一处在局部都合理（「这个检查只对提交那步生效」）。
但汇总结果是引擎再也无法与它的第一个能力分离。

**领域词汇进代码是一道单向门。** `tests/test_engine_purity.py` 是那道门栓：它 grep
引擎源码，命中即让构建失败。

它已经在开发过程中抓到两次真实泄漏。第二次尤其有意思——它抓到 `steps_in_phase`，
逼我回答一个真问题：**`phase` 算引擎词汇还是能力词汇？** 结论是引擎词汇（引擎定义
「有序的 phase 包含 step」，与 run/step/gate 同级），而 `layer` 是某个能力对它的叫法，
那才该禁。这个判断被写进测试的注释里，因为下一个读代码的人否则会猜错。

**这个测试失败时，正确做法永远不是加豁免**，而是把知识移进 `flow.yaml`。
一个会增长的豁免清单就是同一种病换个马甲。

### ③ 退出码强制，不是散文强制

**来由**：那个系统的 `layers/*.md` 里有 **158 条** MUST/必须/绝不 类语句，而它自己的
文档写着：

> "This paragraph is NOT the enforcement (**prose cannot bind an agent under context
> pressure**). The enforcement is an exit code."

它的失效记录也很直白：某个散文步骤「every agent that 'did' it found nothing and
**silently fell**」到硬编码兜底，而 banner 还宣称执行了。

所以这个引擎的卖点就是把「声明流程」翻译成「退出码」。`directive` 里的散文是**给模型的
指令**，不是强制手段——强制来自 `close-step` 会不会返回 3。

### ④ gate 必须有 agent 无法伪造的背书

**来由**：观察到**一轮之内三个 affirm gate 被伪造**。有效的修法只有一个——拿一个
agent 不是作者的信号去核对。

`proof.py` 因此是一个 witness 注册表：默认的 `transcript` witness 数运行时写的
transcript 里的人类发言数，并与**上一个 gate 记录的游标**比较。所以「同一轮里记两个
gate」会被发现（第二个看到的游标已经消费掉那次回复了）并 exit 3。

**降级必须被记录。** 没有 witness 时 gate 直接拒绝；操作者可以显式选 `manual`
降级，但那会写 `witnessed: false` **并落一条 violation** —— 因为 `no_open_violations`
谓词读 violation 表，否则一个带未背书 gate 的 run 就能合法宣称自己干净，把降级洗白。
**这个漏洞是测试抓出来的，不是设计出来的。**

### ⑤ 作用域歧义必须大声失败，绝不静默挑一个

**来由**：一个提交守卫在 5 个并发会话下要求了**另一个无关计划**的 gate。
文档记录的后果是：唯一走得通的路是**伪造那个 gate**，所以那次合法的提交被放弃了。

**一个自信地错的阻断比不阻断更糟**——它把伪造变成唯一出路，而伪造污染的正是 gate
存在的意义。

所以：`open` 在同 scope 已有 open run 时**拒绝**（在第二个 run 出现前就阻止歧义，
最便宜的时机）；而 `guard` 遇到多个候选时**放行 + 大声警告并列出全部候选**，绝不猜。

## 完成谓词

`close-step` 不因为「agent 说做完了」就放过，而是跑 flow 指定的谓词：

| 谓词 | 语义 | 强度 |
|---|---|---|
| `attest` | 最弱：只记录声明。诚实标注它不是检查 | 0 |
| `evidence` | 存在 ≥N 条指定 kind 的证据（可加 `match` 子串） | 1 |
| `evidence_equals` | **最新**一条该 kind 的值精确等于 `value` | 2 |
| `evidence_in` | **最新**一条该 kind 的值 ∈ `values` | 2 |
| `evidence_all_in` | **每一条**该 kind 的行都携带 ∈ `values` 的裁决（带空集下限） | 2 |
| `fields_agree` | 两个 kind 的最新值相等（可再要求 ∈ `value_in`、可有 N/A 分支） | 2 |
| `gate_recorded` | 这一步自己的 gate 已被肯定应答 | 3 |
| `all_of` | 指定的若干步都已关闭；`any_of` 的每组至少一个关闭 | 3 |
| `phase_steps_closed` | 本 phase 的必需步骤都已关闭或已显式跳过 | 3 |
| `phases_summarized` | 已**到达**的 phase 都有 summary（跨层派生） | 3 |
| `no_open_violations` | 这个 run 没有 breach 级 violation | 3 |
| `all_checks` | 上面若干条的**合取**（一步可挂多条判据） | 取最强 |

加一个是纯增量：写个函数挂 `@predicate("name")`，不动引擎其它部分。
**保持它们领域无关**——谓词可以知道「存在 kind=K 的证据」，永远不该知道 K 对某个能力意味着什么。

### 强度分层，以及为什么要如实报它

`validate` 打印每个 ability 的判据强度分布：

```
✅ shipcheck-asis: 101 steps, 8 phases, 4 guard(s); order ok
   criteria: derived×13 · value-checked×52 · record-exists×36 · UNCHECKED×0
   artifacts: 124 pinned across 101 steps (30 step(s) pin ≥2)
   goals: 8/8 phases  [context:derived/config:derived/...]
```

分层是从 `requirements()` 派生的，不是手维护的表——所以新加谓词会**按它索取什么**被自动分类，
报告不会和注册表脱节。

**为什么不只报「非 attest 的有几步」**：一条流程可以 100% 非 attest，同时几乎全是自陈。
`record-exists` 强制了一个显式记录动作、留下可审计的内容行，这比 `attest` 强，
但它**不是机器裁决**——把两者合报成「machine-checked: 101/101」是真话，
而它听起来比实际意思强得多。

### 升级 record-exists 的纪律：值必须来自散文

从 57 步降到 39 步的做法不是给每步编一个允许值集合，而是只处理**散文自己指名了结果**的步骤：
「命中则缓存」「已提议或显式跳过」「残留 = 0，或残留都已解释」「无编译错误」「历史保持单条」
「每条意见归类为『该处理』或『固有噪声』」。凭空发明枚举比留在 `record-exists` **更糟**——
它看起来更强，而它检查的是一个我编的东西。

升级形态一律是 `all_checks[evidence(内容 kind), 裁决检查]`，**两个 kind 而不是一个**。
只换成枚举会用「强度」换掉「内容」：`evidence_in(kb_entry, [written, skipped])` 能通过，
而账本上再也看不出写进去的是什么。

剩下 36 步中占比最大的两类是真天花板：**纯存在性产物**（散文的判据是「这份表/块/文档产出了且清晰」，
没有可枚举的值）和**全称但无裁决词汇**（「同类点已穷尽」「下游影响已列全」——
断言的是穷尽性，需要一个外部预言机而不是账本里的值）。

### 完整性是与强度**正交**的第二个维度，而强度会掩盖它

一步产出三样东西、只钉住最强的**一样**，在强度报告里得分很好，同时三分之二的产出没人查。
所以 `validate` 另报**钉住的产物数**：

```
artifacts: 124 pinned across 101 steps (30 step(s) pin ≥2)
```

两个数字缺一不可——只看强度会漏掉「一条判据代替了好几条」，
只看数量会漏掉「判据全是自陈」。两条都有防回退基线测试。

**这个维度上的两个陷阱**（都是散文自己指出来的）：

- **完成标准与失败条款矛盾时，失败条款赢。** `R06` 的完成标准要求「描述已随之刷新」，
  而正文明说「没更新就发一行告警……但**不阻断**」；`E24` 要求「分数已落库」，
  失败时说「写分失败只记 warning、不阻断收口」。把这两项写成硬产物会让流程卡在
  散文明确允许通过的地方。正确做法是把**处置**记成枚举（`refreshed|warned`、
  `persisted|warned`）——比要求它成功更符合意图，因为散文要的是
  「分数缺失是审计信号」，而一个没被记录的 warning 不是信号。
- **不是每个「看起来像合取」的完成标准都是合取。** `E18b` 的完成标准只有「评分已 emit」
  一项；散文里的「等待中先不 emit」是**时序前提**，而它已由 `deps` 结构性保证，
  不需要也不应该塞进完成判据。

### `all_checks`：一步只能挂一条判据是个真缺陷

四个独立的转写复核在不同步骤上各自撞到同一件事：一步产出三样东西时，
只有最强的那**一样**被钉住，其余不检——而这在日志里和「全查了」长得一模一样。
`all_checks` 是合取，且**刻意只有 AND**：这一层加 `any_of` 会让一步能被它最弱的那条满足，
而真正有意思的错误不是「没有判据」，是「一条判据代替了好几条」。

## phase goal：层验收判据

`goal` 是一层的验收条件，用与 `completion` **同一套词汇**（一个注册表，不是两个——
参照系统把它们分开维护，结果同一个不变式被实现了两遍，在两个地方读同样的字段）。

关键在于它**接在哪**：

```
close-run  要求  phases_summarized
   ↑
summarize  在 goal 不满足时 exit 3
   ↑
goal       层验收判据
```

goal 因此落在**关闭 run 的必经路径上**，而不是旁边。加一条独立的强制命令做不到这点——
没有任何东西迫使谁去跑它。`assert-goal` 是同一个问题的只读版本，随时可问。

**goal 不能是 `attest`**（exit 2）：单步 attest 是一个可辩护的下限，
一整层靠一句声明验收是给里面所有东西盖章，而它在日志里和查过的层一样。

## 测试

```sh
python3 -m pytest tests/ -q      # 44 passed
```

分两类：

- `test_engine_purity.py`（21）—— **结构性不变量**。引擎源码里不许有 step id 形态的
  token、不许有能力专有名词、不许有能力词汇当标识符、不许 import 或硬编码路径进 abilities。
  另有一条反向测试确保词汇**真的**在 spec 里（否则纯净测试可以被一个啥也不干的引擎满足），
  以及一条守卫的守卫（文件集合为空时不许静默通过——因为检查了 0 个文件而变绿是最糟的绿）。
- `test_behaviour.py`（23）—— 全部断言**退出码数字**而非文案。hook 判断的是数字；
  如果重构保留了措辞却改了码，强制就静默消失，只有这些断言会发现。

四条关键守卫做过变异验证（去掉守卫 → 测试必须变红）：guard 的 exit 4、witness 的同轮
重复检测、close-step 的谓词校验、同 scope 并发预防。

## 对照结论：真实流程转写的结果

`abilities/shipcheck-asis/flow.yaml` 是一条**成熟真实流程**的忠实转写——从
`state_machine.py` 的 `CANONICAL_STEPS` 注册表程序化导出（**111 步 / 8 层 / 24 stage**），
gate 取自 `LAYER_MANDATORY_GATE` + `LIFECYCLE_TRANSITION_GATES`，deps 取自
`STEP_PRECONDITIONS` + `step-manifest.yaml` 的 score-emit 链。手写 111 步必错，所以没手写。

它**通过校验并能跑**，这一点本身有意义：spec 语言在 111 步的规模上不塌。但结论是
**结构能表达，控制流不能。**

### 引擎赢的地方（两条，都是实测）

**① load-time 校验抓到了转写里的真实建模错误。** 第一版 exit 2 报
`guard 'task_close' points at step 'E21', whose gate is 'none'`。原因是源系统里
「哪些 step 是 gate」**散在三张表**（`LAYER_MANDATORY_GATE` /
`LIFECYCLE_TRANSITION_GATES` / manifest 的 `gate_type`），我只读了第一张就漏了 E21。
把 gate 归拢到「每步一处声明」的收益，就是这种漏会在加载时炸而不是在运行时静默。

**② 转写过程暴露了引擎自己的一个缺陷，已修。** 源系统的第三层 `stage` 在这份 spec 里
无处可去，我写成 `stage:` 时**被静默忽略**了。同理 `gaet: affirm` 会静默产出一个无 gate
的步骤——正是引擎已经在防的「看着有保护、实际是空」那一类。现在未知 key 一律 exit 2。

### 引擎输的地方（四条，全部是真实构造，引擎表达不了）

| 缺口 | 真实流程里长什么样 | 现在被迫降级成 |
|---|---|---|
| **第三层结构** | layer → **stage** → step，24 个 stage | ✅ v2 已实现 `stage:` |
| **可选的整层** | revision 层 16 步，只在有 findings 时跑 | ✅ v2 已实现 `optional:` |
| **互斥分支** | `E22a` 关单 Y 分支 / `E22b` N 分支，二选一 | ✅ v2 已实现 `exclusive_groups:` |
| **per-step 的降级策略** | `LIFECYCLE_TRANSITION_GATES = {E09g, E21}` 必须 fail-closed | ✅ v2 已实现 `strict_witness:` |
| **loop + 迭代预算** | revision 3 次预算 + `conflict_rebase_count` 独立上限 | ❌ 仍未做（见下文，故意的） |

另外两个较小的：
- **条件步骤**：`C07b` 是「仅 Complex」——步骤是否适用取决于 run 级变量，引擎无此概念。
- **单向棘轮**：`COMPLEXITY_RANK = {Fast:0, Standard:1, Complex:2}`，只许升不许降。引擎没有 run 级的棘轮维度。

### 这份对照给出的结论

引擎当前的形状适合**线性带 gate 的流程**（`delivery` / `authoring` 都是），
而一条成熟流程有 **loop / branch / conditional / 第三层分组**。

**这条对照没有做的事**：它不是移植。没有一行代码从 ship-check 复制过来，
`~/.kiro/` 一个字节都没改。它只回答「spec 语言够不够用」，答案是「结构够、控制流不够」。

### 四个控制流原语（v2 已实现）

v1 的四个缺口都补上了，那条 111 步的真实流程现在**能真正跑到关闭**：

```
驱动结果：closed=82  skipped=29  gated=6  人类发言=4
close-run → exit 0（未用任何 --force-* flag）
audit → unwitnessed: 0，无 forced_close
```

| 原语 | 语法 | 语义 |
|---|---|---|
| **`stage:`** | 步骤上 `stage: pre-flight`，phase 上 `stages: [...]` | 第三层分组。stage 必须被它的 phase 声明，拼错 → exit 2 |
| **`optional: true`** | 步骤上 | 可以合法地永不运行。`close-run` 只要求非 optional 的步骤；`skip` 命令**只对 optional 生效**（必需步骤能被运行时跳过，流程就变成谁想跳就跳） |
| **`exclusive_groups:`** | 顶层 `- [E22a, E22b]` | 互斥分支。关掉一个 → 兄弟自动记 `skipped`，该组只算一次。**依赖单个分支是 fatal**（另一分支被选时依赖方永远不可达） |
| **`strict_witness: true`** | 有 gate 的步骤上 | 这个 gate 不接受降级：无 witness 直接 exit 3，不记「带标记的通过」 |

转写里的实际用法（全部有源码依据）：24 个 stage 来自注册表；29 个 optional 步骤 =
revision 整层 16 步（只在有 findings 时跑）+ resume/仅-Complex/外部系统相关/housekeeping；
`[E22a]/[E22b]` 是互斥组；`strict_witness` 打在 `LIFECYCLE_TRANSITION_GATES = {E09g, E21}` 上。

`status` 现在按 stage 渲染，`●` 已了结 / `◌` 可选未动 / `○` 仍欠：

```
  observation (observation（8 步）)
    ●●             autosde
    ●              dry-run
    ●●●            analyzer-wait
  owed    nothing — closeable
```

### 实现过程中又抓到一个真 bug

手写 Y/N 分支时用了 `- id: YES` / `- id: NO`，结果 **YAML 1.1 把它们解析成布尔值**——
step id 变成了 `True` / `False`（错误信息里 `known: A, True, False, Z`）。
`str()` 会把 `True` 转回字符串 `"True"`，于是它「能跑」但 id 不是作者写的那个，
而文件里其它地方对 `NO` 的引用全部解析失败。

`ON`/`OFF`/`Y`/`N` 同理，`07` 会变成 int 7。这正是这个加载器存在的理由——静默改变语义。
现在非字符串 id 一律 exit 2，并提示加引号。

**这个 bug 是「真的去用它」抓出来的，不是想出来的。** 光写 delivery/authoring 两个
玩具流程永远碰不到它。

### loop + 迭代预算（已实现）

`repeatable` + `budget: N` + `on_exhausted: refuse | escalate`。

**「预算用尽怎么办」由 flow 声明，不由引擎决定。** 计数器一引进来引擎就必须回答这个问题，
而在引擎里回答等于把一个能力的答案烙给所有人。校验会拒绝「声明 escalate 但那一步没有 gate」
的 spec——那样人的决定没地方记录。

转写里的用法有散文依据：修订循环 `R01–R08` 预算 3 次，`R16` 冲突重基**独立**计数 3 次
（散文原话：「它解决的不是『意见』，因此不占这条预算」）。两者的耗尽处理都是 `refuse`——
散文要求「拒绝继续……换方法或叫人，而不是偷偷把上限调大再来一轮」，
而升级路径由**独立的 R07 步骤**表达，不是引擎的 escalate 模式。

### violation 的两种性质必须可区分

同一个账本上混着两件事，而它们方向相反：

| severity | 含义 |
|---|---|
| `blocked` | 一次尝试被**拒绝**。保障生效了，这行是审计痕迹 |
| `breach` | 有东西**带标记地通过了**。保障没生效 |

不区分的后果是具体的：终点判据 `no_open_violations` 会因为**引擎工作了**而让 run 关不掉，
于是「不去尝试」比「守规矩」更划算。更具体的是，转写里 `unwitnessed_gate` 一个 code
既用在「拒绝伪造」也用在「接受降级」上——语义相反，而下游分不出来。
现在前者叫 `gate_refused_unwitnessed` 且是 `blocked`。

默认值是 `breach`：忘传的调用方拿到**严重**的那个，安全的默认是多报 breach，
绝不是把 breach 静默降级成审计注记。

## 运行时条件与义务

引擎的条件层**不绑定任何具体事实**。四个注册表现在形态一致：

| 注册表 | 扩展什么 | 内置 |
|---|---|---|
| `predicates` | 「这步做完了吗」 | 5 个 |
| `witnesses` | 「gate 真有人确认吗」 | 2 个 |
| `fact_providers` | 运行时事实从哪来 | 3 个 |
| `operators` | 条件怎么比 | 8 个 |

```yaml
facts:
  provider: git_tree      # 声明 changed_files:list[str] / vcs_branch:str / dirty:bool / change_count:int

hooks:
  - id: wide-change-attestation
    trigger: phase_end     # phase_start | phase_end | step_close | gate_recorded
    phase: build
    when:
      all_of:
        - { fact: change_count, count_gte: 8 }
        - { fact: dirty, equals: true }
    mode: contract         # contract（引擎什么都不执行）| command
    obligation: true       # 未销账 → close-run exit 3
    contract: |
      改了 {{fact.change_count}} 个文件，逐个说明范围，然后
      harness discharge --run {{run_id}} --hook wide-change-attestation
```

### 为什么是结构化条件而不是表达式字符串

**表达式的失效模式是静默为假。** `facts.workspace`（少个 s）求值为假 → hook 静默不触发
→ 一年后有人问「为什么这个通知从来没发过」。而**条件静默为假比条件不存在更糟**。

让它安全的那块拼图是**事实提供者必须声明 schema**：

- 拼错的键 → **exit 2** + `did you mean 'changed_files'?`
- `int` 上用 `matches_any` → **exit 2**，类型不符
- 未注册的算子 → **exit 2** + 建议

嵌套布尔（`all_of` / `any_of` / `not`）composable 到任意深度，而且**整棵树在跑之前就能校验完**。
迷你语言做不到这一点，而它省下的字符不值一个解析器。

### 为什么不提供 eval / AST 白名单

因为它会摧毁一条已建立的性质：**ability 是可搬运、可离线校验的单元**。
`cp -r abilities/foo` 到别处，`harness validate foo` 就能判断它对不对。
一旦 yaml 能放代码，你就得先**信任**给你 ability 的人，而不只是校验它。

需要新逻辑的正确出口是**注册一个 operator 或一个 fact provider**——python，被当代码 review。
**扩展注册表，不要往数据里塞代码。**

### 义务：让「不会被忘记」可强制

`mode: contract` 的 hook **引擎什么都不执行**——真实系统 15 个 hook 里 14 个是这样，
因为那些活（调某个 API、上报某个里程碑）需要流程引擎不该持有的工具。
引擎的职责不是去做，而是保证它没被忘掉。

`obligation: true` 触发时写一行，`close-run` 在有未销账时 **exit 3**。
这替代了参照系统用哨兵文件做的事（它磁盘上现在有 **1,027 个**）。文件在那边有充分理由——
打印出来的契约会被输出截断吞掉，所以必须落到耐久介质；DB 行是同一个想法，机械少得多，
而且 `close-run` 用一次查询就能问出来，不用遍历目录。

### 事实按 per-trigger 求值并快照

一个边界求值一次，该边界上所有 hook 共享；**求得的值存进 obligation 行**。

per-run 缓存会在长流程里过期——开头读到的「改了哪些文件」到结尾什么也不说明。
存快照则让事后审计能对着「当时的世界」看：

```
✅ discharged  open-questions-outstanding   raised at phase_start 'review'
    evidence: 列了 1 条
    facts at that moment: closed_steps=['A1','B1','B2'], evidence_kinds=['open_question','section'], ...
```

条件不命中时打印**逐叶追踪**，因为「hook 为什么没触发」否则无法回答，
而一个无法回答的否定就是条件悄悄腐烂的方式：

```
⃝ hook suspiciously-no-questions: condition not met
    not: ✘
      ✔ evidence_kinds contains 'open_question'   (actual: '[2 item(s)]')
```

### 通用性是被测试证明的，不是声称的

两个 ability 用**完全不相交的事实**：

| ability | provider | 事实 |
|---|---|---|
| `delivery` | `git_tree` | `changed_files` / `vcs_branch` / `dirty` / `change_count` |
| `authoring` | `run_progress` | `closed_steps` / `evidence_kinds` / `gate_count` / `violation_count` |

`authoring` 完全不碰文件系统。**如果引擎有任何一处暗设「事实是文件形状的」，它就跑不起来。**
配套三条测试：两个 ability 不许共用任何一个事实名；求值层（`conditions.py` / `operators.py`）
不许出现任何事实名；内置算子不许含领域词干。

## 与现有 skill 的关系

**零接触。** 这里没有任何代码读写 `~/.kiro/`，也没有从任何现有 skill 复制代码。
两个 flow 是**参照**那些流程的形状写的，不是它们的移植。

如果要把某个真实能力搬进来，正确顺序是：先写它的 `flow.yaml`（纯数据，几小时），
用 `harness validate` 校验，跑一个真实 run 看退出码语义是否吻合——**吻合再谈迁移**。
先立框架、后找消费方，就是上面 ①。
