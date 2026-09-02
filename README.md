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
│   ├── delivery/flow.yaml   role: fixture —— 机制测试夹具（10 步 / 3 guard）
│   └── authoring/flow.yaml  role: fixture —— 机制测试夹具（5 步 / 0 guard / 菱形依赖）
└── tests/                   246 个测试
```

## 快速开始

```sh
./bin/harness init                                   # 建库
./bin/harness abilities                              # 看装了哪些能力
./bin/harness open delivery --scope /path/to/repo     # 开一个 run
./bin/harness next --run <id>                         # 下一步 + 它的 directive
./bin/harness close-step --run <id> --step C01         # 关一步（不满足则 exit 3）
```

### ability 装在哪：`HARNESS_ABILITIES_PATH`

默认在引擎旁边的 `abilities/`。**但那只是默认**——消费方应该把自己的 flow 放在自己的树里：

```sh
HARNESS_ABILITIES_PATH=/path/to/their-tree ./bin/harness abilities
HARNESS_ABILITIES_PATH=/root-a:/root-b    ./bin/harness validate their-flow   # 像 PATH，os.pathsep 分隔
```

**为什么这不是便利功能。** 纯净性测试断言引擎源码不含任何领域词汇；一个冻死的 `abilities/` 路径在**文件系统层面断言了相反的事**——装一条 flow 的唯一办法是把它放进引擎自己的仓库，而那正是通用基座获得一个再也分不开的第一消费方的方式。所以这条覆盖与那些守卫是同一条性质的两种执行处。

它也让 README 早先那句话第一次**真的**成立：`cp -r` 一个 ability 到别处，`harness validate` 就能判断它对不对——在这之前，「别处」必须还是引擎树内。

四条行为，每条都有理由：

| 情况 | 行为 | 为什么不是另一种 |
|---|---|---|
| 设了变量 | **替换**默认，不并集 | 隐式并集意味着消费方永远拿不到干净集合，而一条从没人指名的根冒出来的 flow 比「必须两个根都写出来」更糟 |
| 同名出现在两个根 | **exit 2**，两边路径都点名 | first-wins 是静默的：一个没人在看的根会遮住正在改的那个，而「到底跑的是哪一个」从 spec 再也答不出来 |
| 指名的根不是目录 | **exit 2** | 报「什么都没装」会把人送去找丢失的文件，而错的只有那一个变量。**默认**根缺失则允许——只装了引擎的检出确实没有 |
| 设了但没指名任何目录 | **exit 2** | 那是笔误，不是「请用默认」 |

变量在**每次调用时**解析，不在 import 时。import 时读会静默失效于「加载之后才设置」的调用方，而症状是「那个根什么都没有」——于是变量成了最后才会被怀疑的地方。这条由一个 **in-process** 测试单独钉住：同一处冻结变异下，三条子进程测试**全部仍绿**，只有它变红。

## 退出码就是产品

| 码 | 含义 | 谁消费 |
|---|---|---|
| 0 | 允许 | — |
| 1 | 用法/环境错误 | 人 |
| 2 | **flow spec 本身非法**（fail-closed，不碰状态库） | CI |
| 3 | **REFUSED** — 规则未满足（依赖未关 / 谓词为假 / gate 无背书） | 能力自己的驱动方 |
| 4 | **BLOCKED** — 受保护动作缺 gate | 外部工具 hook |
| 5 | **INTERNAL** — 引擎自己坏了，traceback 在 stderr | 人（这是本仓的 bug） |

3 和 4 分开是因为受众不同。3 回答「你还不能关这一步」；4 回答「不要让这条命令跑」。
一个 hook 只需要判断 `== 4`，永远不用解析文本。

**5 是后加的，为了消掉一个真实盲区。** Python 未捕获异常退出 1，而 1 也是这里的用法/环境错误——
于是**任何按数字判断的东西（工具 hook，以及本仓每一条断言）都分不出崩溃与拒绝**。这不是假想：
一次变异去掉了「库比引擎新」的拒绝，它落到一条 `RuntimeError`，而断言 `== USAGE` 的测试**通过了，
并把那次崩溃称作一次拒绝**。

给二十条受影响的测试各加一句文案断言，是修同一个歧义的二十个症状。**加一个码是从源头消掉它**：
现在 1 只表示输入或环境，5 只表示引擎。`parse_args` 也在保护区内（解析器自己的缺陷不能漏成裸 1），
traceback 照旧打到 stderr（一个藏起自己位置的 bug 比一个退出码难看的更糟），而
`KeyboardInterrupt` 是 `BaseException`，**刻意直穿**——用户中断不是引擎的过错。


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

所以 `authoring` 被刻意做成与 `delivery` 四处不同——它是 `role: fixture`，存在的目的是**给抽象施压并给机制测试提供夹具**：
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

**而它自己也曾静默过期。** 禁用名单里那条「已装 ability 的名字」是**手写的**，注释写着
「the two installed abilities」——而当时已经装了 **7 个**。也就是说其中五个可以被引擎代码
点名，而这道门栓不会有任何反应。修法是**从磁盘派生名单**，而不是再补五个字符串。

派生之后有一个必须做对的判断：**不能用子串匹配**。`predicates.py` 有一句 docstring 在引用
一条真实不变量——「the revision whose checks were verified must BE the latest revision
**pushed**」——而 `push` 正是一个已装的名字。子串规则会因为一句英文散文而失败，**而一条会
被散文打红的守卫会换来一份豁免清单**，那正是这个文件存在的理由所要阻止的东西。所以禁的是
两种「意味着代码」的形态：**引号里的字面量**，和**下划线拼进标识符**。

原有的「概念词」名单保留不动，与派生名单在两个名字上重叠。这不是冗余：那些条目禁的是一个
**概念词在任何位置**（包括散文），是更强的主张，而它今天成立。

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
| `claim_corroborated` | **最新**一条该 kind 的值若属于 `claims`（一句「什么都没有」），则 `disproved_when` 指定的独立事实必须不反驳它。事实拿不到＝拒绝 | 3 |
| `gate_recorded` | 这一步自己的 gate 已被肯定应答 | 3 |
| `all_of` | 指定的若干步都已关闭；`any_of` 的每组至少一个关闭 | 3 |
| `phase_steps_closed` | 本 phase 的必需步骤都已关闭或已显式跳过 | 3 |
| `phases_summarized` | 已**到达**的 phase 都有 summary（跨层派生） | 3 |
| `no_open_violations` | 这个 run 没有 breach 级 violation | 3 |
| `all_checks` | 上面若干条的**合取**（一步可挂多条判据） | 取最强 |

`claim_corroborated` 是唯一会去问「世界」的判据，理由值得单独说：其余每一条都只读**已被记录
的东西**，所以它们共享同一个盲点——一句声称「查无」的值（`no_dependencies` / `clean` /
「这些都是噪声」）是按它自己的话被接受的。而那恰恰是最值得记下的值，因为它是唯一不需要后续
工作的答案，并且在日志里与同一个值**老老实实挣来**的样子完全一致。

「拿不到事实」与「事实说没有」走同一条出口（都是拒绝），这是它的全部要点：**「我查了，什么都
没有」和「没有东西能来查」产生一模一样的记录**，所以无法核实的那一个绝不能是代价更小的那一个。

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
python3 -m pytest tests/ -q      # 246 passed
```

分两类：

- `test_engine_purity.py`（39）—— **结构性不变量**。引擎源码里不许有 step id 形态的
  token、不许有能力专有名词、不许有能力词汇当标识符、不许 import 或硬编码路径进 abilities、
  **不许把任何已装 flow 的名字写成字面量或标识符**（名单从磁盘派生，不手写）。
  另有一条反向测试确保词汇**真的**在 spec 里（否则纯净测试可以被一个啥也不干的引擎满足），
  以及一条守卫的守卫（文件集合为空时不许静默通过——因为检查了 0 个文件而变绿是最糟的绿）。
- `test_behaviour.py`（207）—— 全部断言**退出码数字**而非文案。hook 判断的是数字；
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

### scope 租约 —— 两条流程共用一个 scope 时，谁拥有这个动作

守卫要回答的是**一个**问题：这个动作能不能进行。它靠 `(scope_kind, scope_key)` 找出该
scope 里开着的 run。**一条**时答案明确；**两条**时它无法知道动作属于谁——而这里它刻意
**放行而不猜**，理由写在 `cmd_guard` 的 docstring 里：

> 一个对无关动作误报的守卫比一个漏报的更糟：被要求去满足一个不属于你这件工作的 gate，
> 会让**伪造那个 gate** 成为唯一的出路。

所以放行是对的。问题在于**它此前只写 stderr、库里不落行**，于是「这条保证悄悄停止适用过
多少次」事后无法回答——而一个数不出来的强制缺口就是能一直存在的那个。现在它记一行
`guard_unadjudicated`，并且：

- **`run_id` 为 NULL**。把它归给两条歧义 run 中的一条，正是整个解析拒绝做的那个猜测。
- **按情况去重**（scope + action + run 集合 + 原因）。守卫每次工具调用都跑，一个未解析的
  scope 否则会一次一行；一万行相同的记录不比一行说得更多，还会埋掉别的东西。
- **只在这个异常分支里开第二个可写连接**。守卫自己的连接保持只读——为了服务一个罕见分支
  而让热路径可写，等于给每次工具调用前面放一把写锁。

#### 而要让它重新能裁决，需要一个被声明的关系

**不是 `parent_run_id`。** 血缘会立刻索要引擎不该给的答案：关父 run 要不要连带关子？子的
进度算进父的吗？子的违规出现在父的账本上吗？每个答案都是消费方的决定，引擎挑的任何一个对
某个消费方都是错的。它还**误描述了常见情况**——一条流程几天后、在另一个进程里、也可能永不
把活交给另一条，那不是包含。

真正发生的事情窄得多：**有一段时间，另一条 run 对这个 scope 有权。** 那是**租约**。

```sh
harness open <inner> --scope <same> --leased-from <outer-run> --leased-at <its-step>
harness leases          # 谁此刻持有哪个 scope，从谁那里、在哪一步接过来的
```

`status` 把关系与**该 scope 到底能不能裁决**一起说出来——后者才是一个人看到同 scope 两条 run 时
真正想知道的事，而在这之前它只能靠触发一次守卫、读它打印的警告才能发现：

```
sc1  shipcheck-asis repo=/tmp/r  step=C00  actor=alpha  → delegated to dl1
dl1  delivery       repo=/tmp/r  step=C01  actor=beta   ← leased from sc1  → delegated to dl2
dl2  delivery       repo=/tmp/r  step=C01  actor=gamma  ← leased from dl1

repo=/tmp/r: 3 runs, sc1 → dl1 → dl2  — guards adjudicate
```

再塞进一条无关的 run，判定就翻过去（而这正是强制此刻在这个 scope 上是关着的）：

```
repo=/tmp/r: 4 runs, no usable delegation (partial)  — ⚠️  guards will NOT adjudicate here
```

| 规则 | 为什么 |
|---|---|
| 只能在 delegate **开始时**授予 | 事后转交意味着权限可以中途重排，那时「当时谁拥有这个动作」取决于你什么时候问——而那正是租约要提供的性质 |
| grantor 必须**真的到过**那一步 | 委派锚在发生过的进展上，不是锚在某人打算到达的步骤上 |
| 必须是**同一个** scope | 不同 scope 本来就没有歧义，跨 scope 的租约会记下一个没有守卫会读的关系，并授予 grantor 从未拥有的权限 |
| 每条 run 每个 scope **只能出租一次** | 两次会让权限同时在两个地方，那不是权限 |
| 链必须**覆盖 scope 内全部** open run | 链外的 run 是无关的，动作可能属于它们——那正是要求 gate 不合法的情形，退回放行并记一行 |
| 链上**每一环**都要满足它对该动作的 gate | 否则委派就是绕 gate 的办法：外层说「人工确认前这里不许做这件事」，把 scope 交给一条什么都不守的流程，动作就过去了。外层的 gate 不是「无关 run 的 gate」——正是它授权了这次委派，对的就是这个 scope |
| 租约随**任一端**结束而结束 | 一条指向已关闭 run 的租约会继续给出答案，而那个答案看起来和活的一样权威 |

grantor 先关而委派没还，**记一条 `lease_outstanding` 但不拒绝**：注意到权限被交出去且没回来
是引擎的事，「那怎么办」是流程自己的事，它的 hook 可以据此提一条义务。

**声明的，绝不推断。** 从时间推出关系（「这条是在那条开着的时候开的，所以在它里面」）与用
`ORDER BY ... LIMIT 1` 解析守卫是同一个错误——都产出一个背后什么都没有的自信答案。

顺带一处**被证明**而不是被假设的事：解析器**没有环检测**。拒绝重复的 grantor 与重复的
holder 之后，声明在两侧都是单射的，于是图是若干互不相交的路径与环；环上每个点既是 grantor
又是 holder，所以不含 root——root 恰好一个时它所在的分量必是路径，别处的环走不到，落到
`partial`。一条跑不到的分支比没有更糟，**它读起来像一个隐患已经被处理了**。这条由一个测试
钉住：把环放在旁支上，如果走图会不终止，那个测试会挂住。

### `uses:` —— 一条 flow 声明它依赖引擎的哪些机制

```yaml
uses: [exclusive_groups, facts, gates, guards, hooks, obligations,
       optional_steps, phase_goals, prose, repeatable, stages]
```

**它不是开关。** 可关掉的强制不是强制：step 台账、violation 记录、evidence 行的全部价值就在于
**没人能退出它们**，而一条能声明 `state: off` 的 flow 等于在给自己发免票。本仓已经解过同一道题
——`role:` 那轮的结论是**标签换不来免票**，而且两条约束方向相反，所以两个角色都不是便宜的那个。
次要但真实的代价是算术：**N 个开关是 2ⁿ 种配置，而测试覆盖其中一种。**

它买到的是另外三样：

| | |
|---|---|
| **加载期双向核对** | 声明了没用 → 拒（那正是本仓反复删掉的「声明了没人读」）；用了没声明 → 拒（一份不完整的清单**比没有更糟**，因为它读起来像一个完整的表面） |
| **写下一条 flow 时看得见的最小面** | 必须提供什么、可以不碰什么 |
| **会指名缺了什么的版本契约** | 声明了本引擎没实现的机制 → **按名字**拒绝。这比比较两个版本号有用得多——它说得出**哪一个能力不在** |

```
$ harness validate <a flow from a newer engine>
⛔ declares capability 'leases', which this engine does not implement.
  known: gates, guards, facts, variants, hooks, obligations, hook_commands, prose,
         stages, phase_goals, exclusive_groups, repeatable, optional_steps, ability_deps
  Either the name is wrong, or this flow was written for a NEWER engine than this one
  — that is what the list is for.
```

**每一项都必须能从已加载的 flow 检测出来。** 一个引擎在 spec 里看不见的能力可以被虚假声明而无人
察觉，而不可检测的条目正是这套核对要防的「不可证伪的声称」。有一条反向测试要求 **14 项每一项都
被某条已装 flow 真实行使**——否则那个检测器本身可能是错的而没人会说。

**它抓到的第一个错误就是我自己的**：`facts` 最初检测 `facts_providers`，而那个元组**永不为空**
（一条什么都不说的 flow 也会拿到默认 provider），于是每条 flow 都被报成「在用 facts」，包括一条
连 `facts` 块都没有的。**一个分不清「声明」与「默认」的检测器，会让整套核对去要求一份没人选择过的
声明。** 正确的检测是 `facts_schema` ——「这条 flow 到底有没有事实可读」。

沉默只对**夹具**容忍：它们存在的目的就是行使引擎机制，让每一个都去枚举自己捅过的机械是纯 churn、
没有读者。`production` 是消费方会拿起来的东西，所以它的表面必须被说出来——这和 `when:` 在那里是
必填的是同一条理由。而**声明了的夹具会被按声明要求**，因为一份放着腐烂的声明比没有更糟。

### 多个 session 同时跑：隔离轴是 scope，不是 session


引擎里**没有 session 这个概念**——`run` 表 14 列里没有任何一列记「谁开的」，`engine/` 里
`session` / 进程身份 / agent 身份零命中。这是刻意的：守卫回答的是「这件事能不能对**这个东西**
做」，而那是**被触碰对象**的属性，不是触碰者的属性。

一份库（`$HARNESS_STATE_DIR/harness.db`）被同机所有 session 共用，隔离靠 `--scope` 的取值。
`DRIVING.md` 指示传 `--scope "$(pwd)"`，于是**每个 session 各自的 worktree 直接成为它的 scope**。
6 路并发共存实测通过。

**为什么不给每个 session 一份库。** 那样两个 session 的守卫互相看不见：同一个 repo 上的两个
worker 各自只强制自己的 gate，谁都不知道另一个存在——「这个 repo 上还有没有人没拿到确认」这个
守卫唯一要回答的问题就此无解。共享账本让跨 session 的歧义**可以被发现**（拒绝，或落一条
`guard_unadjudicated`）。

并发写本身没问题：WAL、每个写函数 INSERT 即 commit、`connect(timeout=5.0)` 即
`busy_timeout=5000ms`。24 次并发写实测 0 个锁错误。

#### 而「一个 scope 只有一条 open run」曾经在并发下不成立

检查与插入是两条语句、中间有间隙，而间隙里还夹着会起 provider 子进程的变体派生。实测：**6 路
并发抢同一 scope，3 轮里 1 轮开成了 2 条。**

后果落在最坏的方向：**输掉竞态的结果正是守卫停止裁决的那个状态**，所以这个失败是**拿走强制**，
而不是留下一条有人会注意到的重复行。它也解释了为什么两百多个测试从没碰到它——它们全是单进程
顺序的。

修法是把「检查 + 插入 run + 插入租约」合成一个 `BEGIN IMMEDIATE` 事务：

- **IMMEDIATE 而不是 BEGIN**。deferred 事务在**第一次写**时才拿写锁，也就是在读之后——同一个
  间隙，只是搬了位置。而 WAL 下输掉的那一方连干净的拒绝都得不到：它的快照此时已过期，于是报
  锁错误而不是被告知「scope 已被占」。
- **慢活一律留在事务外**。变体派生在调用前完成；放进去会让写锁被一个子进程的时长占住——那正是
  隔壁系统查了一天的那个锁事故，而它的修法也正是这条分离。
- **租约与 run 同一个事务**。一条已存在但委派还没写进去的 run，与一条无关的并发 run 无法区分，
  所以两次独立写入之间崩掉，看起来恰好就是租约要消除的那个歧义。

现在 10 路并发 × 6 轮，每轮恰好 1 条。守卫**不靠赛跑测**——概率性测试会偶尔放过 bug——而是用
一个持锁子进程把竞态变成确定性的，并断言它**等过了锁**（deferred 下那次读会立刻返回）。

#### `HARNESS_ACTOR`：给人看的，不是隔离轴

```sh
HARNESS_ACTOR=worker-alpha harness open ...
harness status
#  D1  delivery  repo=/repo/D  step=C01  actor=worker-alpha
```

记它是因为一个人盯着三条并发 run 时需要知道各自属于哪个 worker。**它绝不参与隔离**，而这半边
才是被测试保护的重点：一条测试断言**换一个 actor 仍然撞同一个 scope**。

引擎**发现不了** session 身份——它唯一看得见的进程是自己那次短命的 CLI 调用，所以那些数字以
`cli_pid` / `cli_ppid` 的名字记录，而不是冒充 session。没声明 `HARNESS_ACTOR` 时**不记**，而
不是猜一个。

#### 测试不许碰到真实的库

`tests/conftest.py` 把每个测试的 `HARNESS_STATE_DIR` 指向一个**刻意未初始化**的临时目录。

来由是我自己犯的：驱动 CLI 的测试把状态目录传给子进程，而一个**进程内**调 store 的测试读的是
`os.environ`——那些 fixture 从没碰过它，于是它连上了开发者自己的 `state/harness.db` 并往里写了
三条 run。指向一个不可用的目录而不是一个可用的临时库，是为了让误用**大声失败**
（`StoreNotInitialised`）；真要在进程内用库的测试必须自己说出来。**一个跑在没人选过的库上的
测试，是一个主题未知的测试。**

这道防护落地时立刻抓到一条**既有**测试也在这么干。

### schema 版本与迁移

`init()` 从「创建」变成「**创建或迁移**」，版本号存在 `PRAGMA user_version`（DB 头里的 4 字节，
不需要为它建表，也就没有自举问题）。

```sh
$ harness init                    # 全新库
   schema 1
$ harness init                    # 旧库（本仓自己的 state 就是 user_version=0）
   schema 1 — migrated: 1
$ harness init                    # 再跑一次
   schema 1
```

**`schema.sql` 永远保持最新形状**，因为它同时是那个形状的可读解释——每张表都带着它每一列的
来由。所以**全新库是被盖版本号而不是被迁移**，迁移只负责把**旧**库带上来。

**这个安排制造了一个隐患，必须说清**：一条迁移与一次 `schema.sql` 的编辑必须**效果相同**，而写
它们的过程对此毫无约束。一旦分叉，症状是静默的，而且会把用户群劈成两半——新机器正确，原地升级
的机器微妙地不正确。所以有一条测试**把库用两种方式各建一遍并比对 SQLite 报告的 schema**。那条
测试是这个安排「安全」而不只是「方便」的全部理由。

代价是每条新迁移都要在测试里记下自己的「之前形状」。这比在树里长期保留每一份历史 baseline 便宜。

| 情形 | 行为 |
|---|---|
| 新增一张表 | **不需要迁移**——重跑 baseline 的 `IF NOT EXISTS` 就会建出来 |
| 删除 / 改动一张表或视图 | **需要迁移**——重跑 baseline 对「不该再存在」和「现在长得不一样」是沉默的，于是变更只落在新机器上 |
| 库落后于引擎 | 每条命令 **exit 1** 并给出 `run: harness init` |
| 库**新于**引擎 | **exit 1，且拒绝向下迁移**——新引擎写下了这个引擎不认识的形状，在上面操作会对一个被误读的库给出看起来合理的答案。`init` 也拒绝，且不动版本号 |
| 版本号声明超出了迁移能到达的地方 | `init` 当场报错。否则每条后续命令都会以一个「跑 init」的建议拒绝，而那个建议是错的——`init` 会报成功且什么都不改 |

第一条迁移有真活干：删掉上一个 commit 从 `schema.sql` 里移除、却仍留在每个已有库里的
`v_open_runs` 视图。**机制一落地就有真实消费方**，而不是一个声明了没人读的骨架。

守卫在库不可用时**放行但大声说**——它跑在每次匹配的工具调用之前，不能因为一次待迁移把机器锁死。
它**无法留痕**，因为留痕需要它刚刚拒绝打开的那个库；这处不对称是刻意写下来的，否则看起来像疏漏。

### `role: fixture` —— 以及一个报了数轮的假缺口

`delivery` / `authoring` 有一段时间在每份报告里都像是**没做完的 ability**：`value-checked×0`，
而 `authoring` 还留着一处 `attest`（`UNCHECKED×1`）。我把它当缺口报了好几轮。

**那是分类错误。** 它们不承载工作，它们是**机制测试的夹具**——5 步和 10 步是最便宜的夹具，
gate / witness / preauth / guard / scope 歧义 / close-run / forced-close / purge 这些机制测试
（共 24 个测试函数）都跑在它们上面，替代方案是跑 101 步的真实流程。一个夹具需要的是小、稳、
覆盖机制，**不是强判据**；而 `authoring` 那处 `attest` 是全仓唯一还在演示「最弱下限」的地方，
把它读成缺口恰好读反了。

所以引擎新增一个声明字段，而不是在报告里加个例外：

```yaml
role: fixture      # 缺省 production
```

`validate` 因此不为夹具打印判据强度与产物完整性两行，`abilities` 也不把它们放进 agent 用来
路由的名录（只在末尾留一行让人能找到）。

**关键是这个标签不能是绕过判据检查的开关。** 一个只会让报告闭嘴的角色，任何 ability 都能声称。
所以它被绑在一件结构性的事上，并在加载期强制——**夹具必须不可路由**：

| 规则 | 加载期行为 |
|---|---|
| `role` 不在 `production` / `fixture` 里 | exit 2 |
| 夹具声明了 `when:`（agent 用来找能力的提示） | **exit 2** —— 免于报告就不许可被触及 |
| production 的 `when:` 短于 20 字符 | exit 2 |
| production 的 `requires:` 指向一个夹具 | 测试拒绝 —— 夹具不许成为真实工作的承重件 |
| 什么都不声明 | 落 **production**（严格的那个），因此因缺 `when:` 被拒 |

最后一条是重点：**沉默落在严格的角色上，不是宽松的那个。** 两条约束方向相反，所以两个角色
都不是「便宜的那个」——想免于判据报告，代价是失去可达性。

这条规则一落地就抓到 ~70 处：测试里所有合成 spec 都因缺 `when:` 变红。正确修法不是给它们
各编一个 `when:`，而是承认它们本来就是夹具并标出来——共享助手 `_spec()` 现在默认注入
`role: fixture`。

**另外三处如果删掉夹具会连带变成死代码**（这也是保留它们的实际理由）：`mode: command` +
`fail_closed`（引擎的「效果」缝，`delivery` 是唯一消费方）、内置 provider `git_tree`
（同上）、以及 `attest` 谓词（`authoring` 是唯一使用者）。

### provider 声明它需要什么能力，引擎决定缺失意味着什么

一个伸手到进程外的 provider 需要某样东西在那里：一个工具文件、一个可执行程序、
环境里的一个凭证、一个可连的主机。在这层之前，每个 provider 各自手写「工具没有就返回
零值」——而**它返回的零值，在实质事实上与一次真正的全清不可区分**。区分它们的唯一东西是一个
伴生布尔（作者要记得声明）**加上**一条 vacuous hook（flow 要记得写）。一个约定的两半，
中间没有任何东西把它们绑在一起。改之前实测：两个 ability 里 10 处手写分支、4 个伴生布尔、
4 条 hook，**没有任何检查确认哪一对存在**。

```yaml
@facts.provider("cr_status",
    requires=({"file": "tools/fleet_probe.py"}, {"cmd": "kinit"}, {"net": "code.amazon.com:443"}),
    schema={...})
```

引擎认识四种描述符、不认识任何一个参数的含义：

| 描述符 | 探测什么 |
|---|---|
| `{"file": "tools/x.py"}` | 路径存在（相对路径按**注册该 provider 的模块所在目录**解析） |
| `{"cmd": "git"}` | 可执行程序在 PATH 上 |
| `{"env": "SOME_TOKEN"}` | 环境变量已设且非空 |
| `{"net": "host[:port]"}` | 能建立 TCP 连接（端口缺省 443） |

**能力缺失 → 该 provider 的事实被标为「不可用」，而不是归零。** 触到一个不可用事实的条件或
判据**拒绝，而不是判 false**——因为任何算子作用在缺失值上都答 False，放它过去就等于把条件
静音，而**一个被静音的条件与一个「查了没发现」的条件不可区分**。

一句话概括这条不对称：它与 `claim_corroborated` 的「无法核实 = 拒绝」是同一条，
只是从一个谓词推广到了整个事实层。

`validate` 在**开 run 之前**就回答「这台机器能不能跑这条流程」：

```
   capabilities: 4 declared, 0 absent
   capabilities: 6 declared, 2 absent — cr_status needs cmd 'kinit'; cr_status needs net '...'
   ⚠️  steps whose criteria or hooks read those facts will REFUSE, not pass
```

这是让「读内网的 provider」可以被接进来、而 ability 仍然能在任何机器上被检查的那一块：
**缺失能力是关于环境的事实，不是关于工作的裁决**，所以它必须能这样说出来。

一处刻意没有迁移的：`scope 不是一个目录` 这支留在 provider 里。那是**真答案**（没什么可分析），
不是缺失的答案，把两者混为一谈会把一个正确的空结果变成拒绝。

### 接入源系统外围工具时的筛选判据：先问「这里有什么可比的东西」

盘点源系统 6 个「纯计算」外围脚本（约 1,860 行）后，真正接得进来的只有 **237 行**。差距不在
可移植性上——最可移植的那个（零硬编码路径、纯 stdlib）反而没接：

| 工具 | 可移植 | 有消费方 | 结论 |
|---|---|---|---|
| `analyze-run-metrics.py` 237 | 部分（路径硬编码） | ✅ 有一步在记指标 | **接了** |
| `verify-artifact-digest.py` 98 | ✅ 完全 | ❌ 本引擎没有产物摘要这回事 | 不接 |
| `verify-spec.py` 726 | ❌ 需要源系统整棵 spec 树 | ❌ 它验的是**那个引擎自己的** spec | 不接 |
| `verify-layer-pointers.py` 94 | ❌ 路径无法参数化 | ❌ 同上 | 不接 |
| `analyze-batch.py` 469 | 部分 | ❌ 它统计的是源系统自己的 history 语料 | 不接 |
| `phase4-pre-close-writeback.py` 244 | ❌ **它是写入工具** | —— | 属「效果」类，不是纯计算 |

**判据是「这里有没有它能比对的东西」，不是「它跑不跑得起来」。** 上表里 4 个不接的共同原因一样：
它们计算的对象是源系统自己的产物，而这个引擎要么有自己的等价物（`validate` / 孤儿 topic 检测 /
交叉链接检查），要么根本没有那份语料。接进来只会得到「声明了却没人读」——那正是本仓花了一整轮清掉
两次的形态。

接进来的那一个买到了什么：一步的判据从「有这么一行」变成「这些数字来自运行时」是一句可被独立记录
反驳的声称。原来的判据接受**没人产出过的数字**。

### 效果类（C3）的形态：agent 执行，引擎用只读命令独立核实

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

### 一个步骤的两半可核实性可以不对等 —— 那就分开处理，不要给不可核的那半编一个假事实

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

### 有一种工具不能 vendor：它的价值是自己累积的状态

其余每个领域工具都是逐字节复制进 ability 的，因为**算法可以搬运**。常驻经验库的读取器不行——
它的价值是一个**活的本地存储**：一个刻意不进版本控制的数据库，因为它累积的是本机的使用信号。
把读取器复制过来，只会得到一个指向空处的读取器。

所以能力声明指向它**实际所在的路径**，缺失由能力层如实报出：

```python
@facts.provider("hot_set", requires=({"file": "~/.kiro/skills/shared-kb/memory/memory.py"},), ...)
```

这确立了一条通则：**一个价值在于自身累积状态的工具不可 vendor，而能力声明是让它的缺失保持诚实
的那一半。**

顺带一条必须核对的纪律：那个 CLI 的 `hot-banner` 是纯读（只有 SELECT），所以反复调用安全；
它的兄弟 `recall` 看起来也像读，**实际会写使用记录**，除非显式关掉。provider 调的是前者。

激活横幅的三个取值同样各有反驳事实（`loaded` ← 计数为 0；`empty` ← 计数 ≥1；`unavailable`
← 其实读得到）。这条把源系统只能用散文说的那句话变成了机制：**命令失败不是编造「Hot Set: 0」
的许可。**

### 上报：把「我报了」变成可被协调方反驳的声称

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

### 依赖网络的 provider：两条硬边界

第一个 `net` provider（读评审系统）确定了两条边界，都是实测得出的：

**① 它绝不获取凭证。** 读一份评审需要已认证的会话；一个伸手进用户凭证库去取的 provider，是用
**远大得多的权限**换一个小事实。所以「这个主机要凭证」是**报出来的事实**，不是去满足的前置条件。

**② 因此它答不了什么。** 对真实主机实测：存在的评审与不存在的评审返回**完全相同**的未授权
重定向（`307 → SSO`）。所以「这个评审是否存在」在无凭证下不可推出，**没有任何事实声称它**。
声明一个 `cr_exists` 会是最诱人的谎：在 schema 里读起来合理，在输出里不可证伪。

```
能力（引擎）    主机可达吗              缺失 => 该 provider 全部事实不可用
事实（provider）答了吗 / 要凭证吗       诚实取值
不建模          这个评审存在吗          需要凭证才能答
```

代价也如实记下：这条接入让 `cr-reviewer` 的端到端**开始依赖网络**。缺失路径由一条用不可解析
主机的确定性测试覆盖（不需要网络即可证明拒绝），在场路径则如实取决于运行它的机器——而这正是
`validate` 的 `capabilities:` 一行要回答的问题。

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
