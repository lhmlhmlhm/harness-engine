# harness-engine

一个通用的 **agent 流程注册/强制** 引擎。你用 YAML 声明一条流程，换回来一组**会拒绝**的命令，
每个拒绝带一个工具可以分支的退出码。

引擎自己**不认识任何具体能力**。ship-check 式的交付流程、文档收敛流程、设计流程，
对它来说都只是 `abilities/<name>/flow.yaml` 里的一份数据。

```
harness-engine/
├── pyproject.toml           打包（唯一外部依赖：PyYAML）
├── LICENSE                  MIT
├── bin/harness              入口（几行 argv 转发，行为全在 engine/）
├── engine/                  【基座】不含任何能力词汇
│   ├── __init__.py          版本号的唯一出处（pyproject 动态读它）
│   ├── brief.py             驱动契约的渲染器（`harness brief` 的产出）
│   ├── registry.py          四个注册表共享的那件事：名字归谁、引用怎么解析
│   ├── trust.py             扩展文件是代码：钉内容、报告、以及它不声称的东西
│   ├── policy.py            哪里的流程是强制的（防遗漏，不防颠覆）
│   ├── outputs.py           一步交回什么：读文件的一部分，且刻意不承重
│   ├── schema.sql           引擎自己拥有的表
│   ├── store.py             SQLite 访问层，所有跨界值都是不透明 TEXT
│   ├── flow.py              flow spec 加载 + 校验（= 插件层）
│   ├── predicates.py        完成谓词注册表（= 扩展点）
│   ├── proof.py             gate 防伪 witness 注册表
│   └── harness.py           CLI
├── abilities/               【能力】一个 folder 一个能力，纯数据
│   ├── delivery/flow.yaml   role: fixture —— 机制测试夹具（guard / hook / 义务那一路）
│   └── authoring/flow.yaml  role: fixture —— 机制测试夹具（菱形依赖、attest）
└── tests/
```

<!-- BEGIN GENERATED — python3 integrations/render-readme.py --write -->

**这张表是生成的**（`python3 integrations/render-readme.py --write`），且**只含引擎自己的
事实** —— 没有一行来自 `abilities/` 里装了什么。把 `abilities/` 清空到只剩一个 sample，
下面每个数字依然成立；这是有测试钉住的，不是习惯。

| 引擎实测 | | 出处 |
|---|---|---|
| 基座 | 16 个模块 · 9,171 行 | `engine/*.py` |
| 入口 | 10 行（行为全在 `engine/`） | `bin/harness` |
| 引擎自己拥有的表 | 9 张 | `engine/schema.sql` |
| spec 格式 MAJOR | 2 | `flow.SPEC_MAJOR` |
| 完成谓词 | 15 个 | `predicates._REGISTRY` |
| gate 防伪 witness | 2 个 | `proof._WITNESSES` |
| 运行时事实 provider | 3 个 | `facts._PROVIDERS` |
| 条件操作符 | 8 个 | `operators._OPERATORS` |
| 一条 flow 可声明的引擎能力 | 17 项 | `flow.ENGINE_CAPABILITIES` |
| `scope_match` 模式 | 4 种 | `flow.SCOPE_MATCH_MODES` |
| `guard` 裁决闭集 | 7 种 | `harness.GUARD_VERDICTS` |
| 只有散文的命令 | 7 个（各带理由） | `harness.PROSE_ONLY` |
| 还没有 `--json` 的命令 | 空集（每个写命令都能用 `--json` 作答） | `harness.NO_JSON_YET` |
| 测试 | 492 个 | `pytest --collect-only` |

<!-- END GENERATED -->

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


## 上手

分三层：**必须的三步**、**看你的 flow 才需要的两步**、**runtime 接线**。
第三层不属于引擎，但漏了它引擎就只是一个没人调用的 CLI。

### 一 · 必须的三步

```sh
pip install harness-engine                            # ① 装出一个 `harness` 命令
export HARNESS_ABILITIES_PATH=/path/to/your/flows     # ② 指出你自己的 flow 树在哪
harness init                                          # ③ 建库（幂等，安全重复）
```

做完这三步就能驱动了：

```sh
harness abilities                                     # 装了什么，以及每个的 when:
harness open <your-ability> --scope <key> --run <id>  # 开一个 run
harness next --run <id>                               # 下一步，以及它要求什么
harness evidence --run <id> --step <s> --kind <k> --value <v>
harness close-step --run <id> --step <s>              # 判据不满足则 exit 3
```

**②不能省，因为装出来的东西里没有任何 flow，这是刻意的。** `abilities/` 不进包：树里那两条是
测试夹具（`role: fixture`），而消费方的 flow 是消费方的——把任何一边塞进包里，就是往一个
「核心主张是自己不含领域」的基座里放进一个领域。所以全新安装会如实说「一个都没装」，并等你指出
自己的树在哪。

**上手的工作量不在这三条命令上，在把你的 flow 写到能载入。** 引擎不替你猜，而是逐条说缺什么：

```
❌ notes: INVALID — …/flow.yaml: no usable `when:`
          (needs at least 20 characters saying when to reach for this ability)
❌ notes: INVALID — …/flow.yaml: exercises 'results' without declaring it in 'uses'
✅ notes  (写一份笔记)   scope=notebook  steps=2 (2 required, 0 optional)
```

前两条都是真实的第一次尝试。**路由必须由 flow 自己声明**（`when:`），而**用了哪个机制必须在
`uses:` 里承认**（双向核对）——这两条都在载入期致命，因为一个说不清何时该用的 ability 和一个
悄悄用了未声明机制的 ability，都会在**别的地方**才暴露。

三个环境变量，全部在**调用时**解析：

| | |
|---|---|
| `HARNESS_ABILITIES_PATH` | flow 的根，`os.pathsep` 分隔，**替换**默认而非并集 |
| `HARNESS_STATE_DIR` | 状态库的位置 |
| `HARNESS_ACTOR` | 记进 run 的调用方标识，**仅供诊断，绝不参与隔离** |

### 二 · 两个条件步：看你的 flow 有什么，不是每次上手都要做

#### `harness trust` —— 只有 flow 带 `providers.py` 时，而且**必须做**

带扩展代码的 flow **在批准之前一律 INVALID**，因为批准的是「要以引擎权限运行的代码」，那是机器
所有者的决定，不是引擎能替他做的：

```
$ harness validate delivery                        # 尚未批准
❌ delivery
…/delivery/providers.py
  is code from outside this engine's own tree and has not been approved here.
  digest:             sha256:0909ef597d5292d8…
  imports (advisory): __future__, engine, pathlib, subprocess, sys
  Loading this flow IMPORTS the file, so reading its spec would run it.
  Review it, then:    harness trust delivery

$ harness trust                                    # 批准【之前】先能审
⚠️  delivery         unknown   sha256:0909ef597d5292d8…
     …/delivery/providers.py
     imports (advisory): __future__, engine, pathlib, subprocess, sys
1 file(s) will refuse to load. Approve one with: harness trust <ability>

$ harness trust delivery
approved delivery
  …/delivery/providers.py
  sha256:0909ef597d5292d8ad394d0b0ea3fd1d6a2e54dd6216f031946276903cf3491a
```

批准是**按内容**钉定的：那个文件改一个字节，批准就失效并重新询问。所以它防漂移，不是沙箱。

**上面那段的前提，不写出来就复现不了：`abilities/` 必须在引擎自己的树之外。** 树内的扩展文件
**刻意不钉定、也不能被批准** —— 能改它的人本来就能改引擎，而一份由引擎保管的记录不可能比一个
能编辑引擎的人活得更久。所以把能力目录放在引擎树里时，`trust` 无事可做，`validate` 直接 ✅；
上面那段之所以成立，是因为 `HARNESS_ABILITIES_PATH` 指向了别处 —— 而那正是上手第一步要求的装法。

#### `harness require --add` —— 只有 flow **有受守动作**、且你想强制「必须先开 run」时

这一步**不属于上手**。它的正确时机是：你已经用了一段时间、确认某个 scope 的流程真的必须走。
而它对很多 flow **根本无从下手**——它拦的是**受守动作**：

| flow 的形状 | 受守动作 | 声明强制有意义吗 |
|---|---|---|
| 只产出文档 / 只做分析 / 只读 | 0 | ❌ 无从下手。跳过一个 run 的代价是「没有记录」，不是「一个不可逆动作没被把关」 |
| 会提交、发评审、关任务、删工作树 | ≥1 | ✅ 这是它唯一的用武之地 |

**空声明时行为与这个机制存在之前逐字节相同**，这条有测试和变异钉住：

```
$ harness require                                  # 全新机器
(nothing is declared mandatory here)
With nothing declared the guard enforces the gates inside an open run and allows
everything when nothing is open — exactly as it did before this existed.
```

**不自动，而且刻意不自动。** 让引擎推断「这里该强制」，等于替机器所有者做一个改变所有人提交行为
的决定。

### 三 · runtime 接线：不在引擎里，但漏了之后**看起来一切正常**

引擎只是一个 CLI。让 agent 主动调它、并且在它不配合时仍然被约束，需要 runtime 侧三件事：

| 要接的 | 怎么接 | 不接的后果 |
|---|---|---|
| **驱动契约进上下文** | `harness brief --write` 的输出作为 agent 的常驻 resource | agent 不知道有这套东西 |
| **拦截面** | runtime 的 pre-tool hook 调 `harness guard-tool`，把它的 exit 4 翻成 runtime 的「阻断」 | **门完全不生效**——这是唯一不依赖 agent 配合的机制 |
| **证人** | 四个变量，见下 | 「人确认过」退化成 agent 自签：记一条 violation 然后**放过** |

后两条最容易漏，**因为漏了之后一切看起来正常**——没有报错、没有警告，只是保证不在了。

#### 证人要四个变量，而其中两个是「你的转录长什么样」

```sh
HARNESS_WITNESS=transcript
HARNESS_TRANSCRIPT=<这一次会话的转录文件>
HARNESS_TRANSCRIPT_ROLE_PATH=<哪个字段说出「这一条是谁说的」>    # 点号路径，如 kind 或 data.role
HARNESS_TRANSCRIPT_HUMAN=<该字段里哪些值意味着一个人>            # 逗号分隔，如 Prompt
```

**后两个的默认值对一个真实 runtime 是错的，这是实测出来的。** 引擎默认在 `role` / `author` /
`from` 三个字段名里找，人类值认 `user` / `human`。而一个真实 runtime 每行都是
`{kind, data, version}`：角色在 `kind` 上，人类回合拼作 `Prompt`。用默认去读那份转录会数到
**0 个人类回合**，于是**每一道门都被拒**——而且会一边拒一边说「这份转录里根本没有人类回合」，
那句话对解析是真的，对会话是假的。

**修法不是让引擎学会那个 runtime 的拼法**，而是用同一条缝：**知识从外面声明，引擎只施加它**。
这和 `guards.<action>.matches` 把「什么命令算一次提交」放在 flow 里、适配器永不学习工具含义是
同一个形状。第三个 runtime 用第三种拼法时，引擎同样不用改。有一条测试直接断言引擎源码里**没有
任何 runtime 的拼法被硬编码**。

**两种失败被分开说，因为它们的修法不同：**

```
声明的路径找不到该字段  →  "no record names a side … This is 'I cannot see who spoke',
                            NOT 'nobody spoke'"，并报出用了哪条路径
找到了但没有值意味着人  →  "N record(s) name a side, and none of them means a person"，
                            并报出【实际出现的值】—— 那就是全部的修法线索：
                            Values present: 'AssistantMessage'×268, 'ToolResults'×250, 'Prompt'×18
```

那份诊断**有上限**（最多 5 个值、每个 24 字符）：一条写错的路径可能指向**内容**，而把一段会话
无界地倒进日志，比它要解释的那个问题更糟。

#### 这段声明该住在哪里：不在引擎里，也不在 agent 手上

```
❌ 引擎里          转录路径是某个 runtime 的磁盘布局，而 proof.py 的文档正是禁止它知道
❌ agent 的配置    kiro-cli 的 agent JSON 没有顶层 env（env 只存在于 mcpServers 内部）
❌ 静态 plist      路径要 $KIRO_SESSION_ID，而它在会话产生之前不存在
❌ brief 教的 alias  那条 alias 是【agent 自己】用不用的 —— 一个被约束者可以拒绝的证人不算证人
✅ PATH 上的一个 shim  在调用时才展开会话 id，且不在 agent 的逐次控制之内
```

shim 只在**转录文件真的存在**时才声明 transcript 模式。指向一个不存在的路径会让每道门以「那不是
一个文件」被拒，读起来像机制坏了而不是不在；什么都不声明时引擎回落到自己的默认，**而那个降级会被
记成一条 violation**，于是「没接证人」出现在 `harness audit` 里而不是无声通过。

agent 仍然可以绕过它——直接调引擎自己的 `bin/harness`。那是**颠覆而不是遗漏**（与 required-flows
记录划的是同一条线），而且是一次可见的动作，不是一次静默的缺失。

两件配套的事实：

- **hook 必须覆盖哪些工具是可以问出来的**，不用猜：`harness brief` 会把它渲染出来（由已装 flow
  的 `guards.<action>.matches` 推导）。**一个不在 runtime matcher 列表里的工具永远不会被检查，
  而那个失效是静默的。**
- **写适配器不用重新想「什么算正确」**：`harness adapter-contract` 以数据形式发布 io 契约、
  译码用例、韧性用例，以及命令面（每条命令的参数/类型/必填/退出码表）。参考实现在
  `integrations/kiro-pretooluse.py`——它**只做翻译**：把 stdin 上的事件转成 `guard-tool` 的参数、
  把 exit 4 转成 runtime 的阻断码，其余什么都不做。「什么命令算一次提交」这类知识住在各 flow 的
  `guards.<action>.matches` 里，所以适配器永不学习任何工具调用的含义——**每个新 runtime 只需重写
  这一层，而判断逻辑白拿。**

### 上手自检

```sh
harness abilities            # 每条都 ✅ ？（❌ 会说是 when:/uses:/trust 哪一个）
harness trust                # 还有 unknown 的扩展文件吗？
harness require              # 这里声明了什么强制（通常应当是「什么都没有」）
harness brief                # 契约生成得出来吗；里面的 hook 工具清单接上了吗
harness validate             # 全部 spec 合法？exit 2 = 不合法（CI 就读这个码）
```

**`requires-python = ">=3.10"` 是跑出来的，不是猜的。** 整套 476 个测试在 **3.10.16** 上跑过并全绿
（2026-09-11 实测，415 秒，与 3.12 上的 476 一致；`mcp` 需钉 1.x，见下条）。所以下限写 3.10。
它以下**没有被验证过**——想往下调，先在那个版本上把套件跑一遍，而不是改这一行。
声明一个未经测试的下限，和引擎在别处拒绝的「不可证伪声称」是同一件事。

**这句话本身没有守卫，所以它带日期。** 测试数会随开发漂移，而「在 3.10 上跑过」这件事无法被
`pytest` 自证——套件跑在**一个**解释器上，不会顺手验证另一个。既然守卫做不到，就退到第二好的
办法：把测量时间写进句子，让读者能判断它有多旧。一个不带日期的「跑过并全绿」是无法被证伪的。

**`mcp` 必须钉 1.x。** `integrations/mcp-server.py` 用的是 `@server.list_tools()`，而 `mcp` 2.x
删掉了 `Server.list_tools`——今天 `pip install mcp` 装到的是 2.x，服务端会在启动时
`AttributeError` 崩掉。那 5 个测试用 `pytest.importorskip("mcp")` 守着，所以**没装 mcp 的机器
看到的是 skip 而不是失败**，而装了 2.x 的机器会看到 3 个失败。这是参考实现的约束，不是引擎的：
引擎自己不 import `mcp`。

#### 状态库的默认位置在引擎之外

```
$XDG_STATE_HOME/harness-engine/harness.db
（未设时）~/.local/state/harness-engine/harness.db
```

**曾经的默认是 `<engine>/../state`，那对开发引擎的人合适，对其他所有人都是错的**：装进
site-packages 之后那个目录常常不可写，而在它可写的地方，它把用户的 run 台账放进了一个**下次升级
就会被替换**的包目录里。一个把消费方数据存在自己安装目录里的基座，不是一个可安装的基座。

`XDG_STATE_HOME` 正是规范给这类数据的答案：跨运行持久、不是配置、也不是可被随意删除的缓存。
macOS 没有原生 XDG 位置，而为了算一个「按平台正确」的路径去引入一个依赖，对一个**唯一外部依赖是
一个 YAML 解析器**的基座是错的交换——所以到处用同一条规则，不同意的人有环境变量。

**而默认位置搬家时，旧位置上已有的库会被大声拒绝，不会被静默孤立**：

```
⛔ a store exists at the OLD default location and would now be ignored:
  old: <engine>/state
  new: ~/.local/state/harness-engine
  Pick one, explicitly:
    keep using it    ->  export HARNESS_STATE_DIR=<old>
    move it across   ->  mkdir -p <new> && mv <old>/harness.db* <new>/
    start fresh here ->  HARNESS_STATE_DIR=<new> harness init
```

两个替代方案都更糟：读旧的，这次搬家就是个空操作；读新的，则**每次查询都会像那些被记录的 run
从未发生过一样作答**。它只在**没人说过库在哪**（没有覆盖）且**确实有东西可丢**（旧位置有库、新位置
没有）时触发——这两条同时成立只描述一种情形：一个早于这次搬家的源码检出，而那也正是唯一能处理它的
听众。打包安装的消费方没有那个目录，因此永远看不到它。

第三条出路（在新位置全新开一个）**必须可达**，因为旧库存在时连 `init` 也会被拒——所以那条消息把
它写了出来，并有一条测试断言那句话是真的。

## spec 格式与声明面

一条 flow 能声明什么，以及引擎在载入期就拒绝什么。这一节全是**载入期**的事——跑起来之前就该失败的，不留到跑起来之后。

### spec 格式的版本：MAJOR 是可读性声明，MINOR 只说加了键

```yaml
version: 1        # = 1.0
version: "1.4"    # 更新的 minor：结构不变，可读
version: 1.10     # ⛔ 拒绝 —— 见下
```

| 情形 | 行为 |
|---|---|
| 同 MAJOR，MINOR ≤ 本引擎 | 正常读 |
| 同 MAJOR，MINOR **更新** | **读它**——结构没变，拒绝它就是拒绝一份本来读得懂的东西。但它带的未知键仍然致命，**而消息会说这是版本差距** |
| 不同 MAJOR | 拒绝，**并报出能力图景**（见下） |
| `version: 1.10` 这种裸小数 | 拒绝：YAML 把 `1.10` 与 `1.1` 读成**同一个 float**，两个不同的 minor 会静默合成一个 |

**没有为未来 MAJOR 准备 reader，这是刻意的。** 接受一个更新的 major 并按当前 major 读，就是静默
误读它——而一个不可证伪的声称正是这个引擎在别处到处拒绝的东西。等真有 major 2 时，支持它是
「每版一个 reader + 每版一份固定 spec」，机械且被守着。

#### 三处此前会误导人的地方

**① 版本检查曾在未知键拒绝之后。** 于是一份来自更新格式、带了新键的 spec 会先以「unsupported
key(s)」被拒，消息里全是关于拼写错误的建议，**版本一个字都没提**——写它的人被告知去检查拼写。
**只改顺序就修好了**，而这条由一条测试钉住：一份同时是「更新 major」且「带未知键」的 spec，
它的报错里**那个键名根本不该出现**（键名是唯一精确的判别式；像 "does not know" 这样的措辞
在正确消息里也有一份，断言它会为了错误的理由通过——我第一版就是这么写错的）。

**② 未知键的措辞现在有两种，且不许互换。** 同格式的拼写错误照旧讲拼写；更新 minor 的未知键讲
版本差距。**两个方向都有断言**，因为只查一边的话，「永远讲版本」也能通过。

而 minor 更新的未知键**仍然致命**——理由不是版本，是那条不许忽略未知键的老纪律：一个被静默
丢掉的键读起来像生效了，而它可能正是这条 flow 依赖的那个约束。

**③ 不可读的 MAJOR 现在会说清是哪种缺口。** 同一个版本号底下藏着两个不同的问题：

```
Its `uses:` names only mechanisms this engine HAS (gates, prose), so this is a
format gap and not a machinery gap: a newer engine can read it unchanged.
```
```
Its `uses:` also names mechanisms this engine does NOT implement: leases.
So a newer reader alone would not be enough — the flow needs machinery that is absent here.
```

**版本号本身分不出这两者**，而 `uses:` 分得出——这也说明 `uses:` 是比版本号更细的那个向前兼容
机制：**它按名字告诉你缺什么。**

### 基座不认识任何外部工具

一个内置 provider 曾经 shell 出去调具体的版本控制工具，理由写在它自己的注释里：「what did I
touch」是条件最常问的东西，所以它作为便利 ship 了。

**那是基座里的领域知识，而且它绕过了基座自己的缝。** 引擎有 `requires={"cmd": ...}`，存在的意义
就是让需要外部工具的 provider 声明它，从而让「我没法看」不再长得像「什么都没找到」——而一个内置
provider 说不出这句话，因为那会让**引擎自己**依赖一个工具。

**它此前通过了纯净性守卫**，而那是最值得记住的部分：守卫豁免了 `facts.py` 的「不许命名事实」规则
（理由正当：声明 schema 就是 provider 的全部工作），但**豁免静默地覆盖了更多**——一个工具名不是
一个事实名。两道新守卫把它关上：

| 守卫 | 形态 |
|---|---|
| 内置 provider 不许 shell out | **结构性**：`engine/facts.py` 里不许出现 `subprocess` / `Popen` / `os.system`。对所有工具成立，不只是那个恰好发生的 |
| 引擎不许命名具体外部工具 | **尖锐形态**：引号字面量或下划线标识符。**不能用子串**——`git` 出现在 `legitimately`（引擎里 8 次）、`legitimate`、`isdigit` 里面，子串规则会因为英文散文而失败，而一条被散文打红的守卫会换来豁免清单 |

内置只剩三个，共同形状是**只报引擎已经持有的东西**：什么都不报、run 自己的身份、run 自己的进度。
测试把它钉成**精确集合**而不是下界——要防的正是「新加一个伸手到外面的内置」，而下界注意不到多出来
的那一个。

守卫第一次运行就抓到两处：一处描述符示例用了具体工具名（同段其它三个都用占位符），一处
`run_progress` 的 docstring 因为这次搬迁**已经过期**。两处都改了，没加豁免。

### 注册按 ability 命名空间（spec 格式 2.0）

四个注册表曾经是**一个平坦的进程全局命名空间**。两个互不相识的 flow 各自注册 `open_findings`
——各自领域里最自然的那个词——会互相打断，而**加载顺序是字母序，谁都没选过它**：哪一个能用是由拼写
决定的。上一轮把消息改成点名双方，那让诊断变诚实了，但**冲突本身还在**：基座容不下两个独立挑了同
一个明显词的 flow。

现在注册归**做出它的那个文件所属的 ability** 所有，键是 `<ability>.<name>`；引擎自己的注册不属于
任何人，保持裸名。

| 情形 | 结果 |
|---|---|
| 两个无关 ability 各注册 `open_findings` | **共存**（`alpha.open_findings` / `beta.open_findings`）。那个消除不掉的冲突消除了，因为它从来不是一个名字，而是两个 |
| 一个 ability 把一个名字注册两次 | 仍然拒绝，**而这一个是真缺陷**：一个作者、一个命名空间、两次声明。消息明说是「你自己的」，否则「改掉你的」会被读成旧建议「改掉别人的」 |
| ability 注册引擎已有的名字（如 `all_of`） | **保留字，拒绝**。遮蔽会让同一个词在它自己的 flow 里是它的版本、在别人的 flow 里是引擎的版本，**而两份 spec 里都看不出来** |

**spec 怎么引用**——裸名 = 我的，或引擎的（这两者不相交，所以无歧义）：

```yaml
facts: {provider: open_findings}          # 我的，或引擎的
```

借别人的**必须说出来**，而且那个 ability 必须被声明：

```yaml
requires: [alpha]
facts: {providers: [alpha.open_findings]}
```

限定形态不是装饰。旧平坦命名空间下，一份从没提过 `alpha` 的 spec 里裸写 `open_findings` 也能解析
——**它能用是因为别的东西恰好先加载了，那是一个靠运气成立的依赖**。写出拥有者让依赖在**使用现场**
可见，引擎于是能拿它跟 `requires:` 对账，而不是靠指望。

**解析刻意不是搜索。** 一个回落到「谁有这个名字就用谁」的 resolver 会把命名空间刚消除的那场加载顺序
抽奖原样请回来。四条拒绝各自点出该做什么：裸名借用（说出是谁的）、限定但未声明（`requires:` 加上）、
自我限定（写成裸名——否则一个 ability 看起来依赖它自己）、`dep` 里没这个名字（列出它有什么）。

**「它属于谁」这条提示只有在拥有者恰好也载入时才给得出**，而那是消息精度依赖加载顺序。所以解析不到
时还有一条**恒为真**的指路：裸名永远到不了别人那里，写 `<ability>.<name>` 并声明。测试把两者分开钉住
——单独校验一份 flow 时只有恒真那条，全量校验时必须点名。

**这是一次 MAJOR 变更**，因为裸名借用曾经合法而现在不合法。所以 1.x 的 spec 被拒——而 MAJOR 拒绝是
一个手里拿着 1.x spec 的作者唯一会看的地方，于是它**说清改了什么、以及哪一半不用动**：

```
spec format 1.0 cannot be read; this engine reads 2.0.
  WHAT CHANGED IN 2.0: registrations are namespaced per ability, so a reference to
  ANOTHER ability's predicate/provider/operator must be written with its owner and
  declared — `providers: [<owner>.<name>]` plus `requires: [<owner>]`. Your own names
  and the engine's stay bare and need no change.
```

对称地钉住了它**不该**声称知道的：一个本引擎从未定义过的版本，只拒绝、**不编造迁移说明**。

迁移代价之所以低，是因为**跨 ability 引用本来就该罕见**：一条 flow 借另一条注册的东西时必须
`requires:` 它，而那条声明就是引用的上限。所以一次 MAJOR 迁移通常是「每条 flow 改一行 `version:`」，
加上给那少数几处借用补上限定。你自己那棵树有多少处，`harness abilities --json` 数得出来 ——
这里不写一个数字，因为它是**你装了什么**的性质，不是引擎的性质。

**并且「名字是否被占」的权威仍然是注册表本身**，owner 映射只是说明性的。两个必须保持同步的字典
就是一个等着发生的 bug，而它当场发生过：清理代码从注册表移走了一个名字、却留在 owner 映射里，
于是下一次注册被判为「与无人冲突」。现在不同步只会让消息退化，**不会凭空造出一个冲突**。

### completion spec 也是闭集了

flow.yaml 有 13 组闭集，未知键一律 exit 2 并列出合法键——**除了 completion spec 内部**。那里
`validate_spec` 只检查必填键在不在，不拒绝未知键。后果不是「它会坏」，而是**它会变弱**：

```yaml
completion: {type: evidence, kind: k, min_counts: 2}
                                      ↑ 多一个 s，被静默忽略，判据降级成「任意一行」
                                        而 yaml 读起来仍然像要求两行
```

修法是把每个断言**真正读的键**声明出来——`predicate(name, requires=(...), optional=(...))`——于是
`{"type"} | requires | optional` 就是那个断言的闭集：

```
evidence          requires=(kind,)              optional=(min_count, match)
all_of            requires=(steps,)             optional=(any_of,)
fields_agree      requires=(kind_a, kind_b)     optional=(absent_ok, value_in)
no_open_violations                              optional=(include_blocked,)
phase_steps_closed                              optional=(allow_optional_open,)
phases_summarized                               optional=(exclude,)
```

**是 per-predicate 的闭集，不是所有断言的并集**（有变异钉住这条）：`match` 属于 `evidence`，写在
`evidence_equals` 上要被拒；`kind` 在 `attest` 上要被拒。

```
step 'W01': completion type 'evidence' has unsupported key(s): min_counts
  supported: kind, match, min_count, type
  A key nothing reads does not fail — it makes the criterion WEAKER than it
  looks. `min_counts` where `min_count` was meant is a silent downgrade to
  "any one row", with the yaml still reading as though it demanded several.
```

还有一条**从实现侧交叉核对**的测试：走每个断言的源码抽出它读的 `spec` 键，断言全部已声明——否则
机制会拿自己的实现去拒绝自己。

### flow 声明自己可以怎样结束：`results`

`--result` 此前接受**任意字符串**，默认 `completed`。证据就在这个仓库自己的测试里：

```
"--result", "completed"  ×5      "--result", "done"    ×4
"--result", "aborted"    ×3      "--result", "abort"   ×2
```

**四个词表达两件事**，而 `history` 能显示它们却无法聚合。

```yaml
results:
  values: [completed, abandoned]
  default: completed
```

**形状照 `variants:` 来（values + 显式 default），不用裸列表**——裸列表会让**顺序**决定默认值，而
「顺序有含义」是一条在 yaml 里看不见的规则。

| 情形 | 行为 |
|---|---|
| flow 声明了，`--result` 省略 | 用 **flow 的** default（不是引擎的词，有变异钉住） |
| flow 声明了，`--result` 不在 values 里 | exit 1，列出合法值与默认值，**且不会在拒绝前先关掉 run** |
| **flow 没声明** | 与从前逐字节相同：任意字符串，省略则 `completed` |

最后一行是刻意的：把声明变成必填会是一次 spec 格式破坏，而别处写的 2.0 flow 可能并没有这个洞。所以
未声明仍然合法，而 `abilities --json` 报出哪些 flow 处于那个状态——**沉默是可见的，不是被假设的**。

词汇表在 `abilities --json` 里可读，所以 driver **在关 run 之前**就知道合法值，而不是只能从一次拒绝里学到。

8 个已装 flow 全部声明了 `[completed, abandoned]`。**这个集合刻意保守**：`abandoned` 是引擎已经半知道
的那个区别（强制关闭与干净关闭不是一件事）。更贴合各 flow 的词汇（`shipped` / `drafted` / `pushed`）现在
可表达了，但那是领域判断，不该由我替这些 spec 决定。

### 一步交回什么：`output`

写命令此前只回散文——`close-step` 打印「关了、下一步是谁」，而 hook 触发、尤其是**一条 obligation
被挂上**，只在散文里宣布过。事后可以 `obligations --json` 列出来，但**债被交到手上的那一刻**对任何
非人类的东西都是不可读的。

现在 `close-step --json` 回一个信封，而 step 可以声明它**交回什么**：

```yaml
steps:
  - id: K02
    completion: {type: evidence, kind: test_result, match: "pass"}
    output:
      from_evidence: test_log        # 路径来自 driver 记录的那条 evidence
      select: {anchor: "FAILURES"}   # 或 lines: 120-180 / regex: …；三者只能给一个
      max_bytes: 4096
```

```
$ harness close-step --run o2 --step K02
✅ closed K02 (验证（含变异验证）) — 1 evidence row(s) of kind 'test_result'
   📤 output delivered: 118 bytes from anchor 'FAILURES'  [/…/pytest.log]
```

**它刻意不承重，而这是整个设计的支点。** 内容来自 driver 记录的路径——如果交回失败能改变一步能否
关闭，形状就会变成：driver 写一个文件说「通过了」，引擎读回来，然后有人把这当成验证。所以
`output` 永不出现在 `completion` 里，**交回失败绝不改变步骤是否关闭**（有变异钉住：把失败改成阻断
会变红）。

**路径来自 evidence 行，不来自 spec。** spec 不可能知道这次 run 的产物路径（取决于 scope、在哪里
干活、哪个任务），写死一个在第二台机器上就是错的；而来自记录行还意味着「交回了什么」在账本里有答案，
不是引擎发明的。**flow 自带的文件是另一回事，早有通道**——散文层，按指针取。这一个是给「run 发生前
不存在」的内容。

**读不到不等于是空的。** 状态是闭集：

| 状态 | |
|---|---|
| `delivered` | 读到了，附 `bytes` / `sha256` / `truncated` |
| `no_source_recorded` | 那个 kind 一行都没记，所以没有路径可读 |
| `source_missing` | 记了，但那不是一个可读文件 |
| `selector_no_match` | 文件读到了，选择器在里面什么都没匹配到 |
| `unreadable` | 打开失败，附异常 |

**截取复用散文层的实现**：`prose.extract_section` 已经在做「按标题截一段并在下一个同级标题处停」。
不为 output 再造一套——「一段在哪里结束」只有一条规则，两条会打架。有变异钉住这条（让它跑过下一个
标题会变红）。

**账本存摘要不存内容**：`step_log` 记一行 `event='output'`，带来源 + `sha256` + 字节数 + 是否截断。
于是「那一刻交回的是什么」可回答，而账本不会变成 blob 存储。和扩展内容钉定同一个形状。

顺带修掉一处：**`requirements()` 漏报 `match`**。`match` 是 `evidence` 断言真的会读的键，但它没出现
在机器可读的要求清单里——于是清单**低报**了判据。一份漏了一条要求的要求清单比没有更糟，因为它会被
信任。

## 账本的不变量、审计与强制

账本一旦写下就不许被悄悄改写，而账本能被反问「这里最常出什么问题」。以及：**哪里的流程是强制的**——防遗漏，不防颠覆。

### 已结束的 run 不接受写入

跑一遍完整流程时发现的：引擎**没有「这个 run 已经结束，别再往里写」这个概念**。对一个已关闭的
run 发写命令，全部成功：

```
enter        -> 0  ▶ entered A1
evidence     -> 0  ✅ evidence recorded          ← 往一个已结束的账本里追加行
close-step   -> 0  ✅ closed A1
gate         -> 0  ✅ gate D2 = affirm
summarize    -> 0  ✅ summarized phase 'gather'
close-run    -> 0  ✅ closed run w1 → abort      ← 把 result 从 done 改成了 abort
```

最尖锐的是最后一条：`done` 变成 `abort`，**带一条成功消息、没有 violation、结束时间戳刷新**，之后
`history` 显示 `abort` 就像它一直如此。**一份事后能被静默编辑的记录不是记录。**

修法是**一处检查，在共享入口**（`_run_or_exit`），而**默认是「必须 open」，读命令显式豁免**：

| 方向 | 一个新命令忘了处理时 |
|---|---|
| opt-in（写命令要主动要求检查） | **静默**往已结束的 run 里写 —— 正是要堵的洞 |
| **默认要求 open**（读命令主动豁免） | 新的读命令在已关闭 run 上被拒 → **它自己的测试立刻红** |

**吵在错的地方胜过静在错的地方**，所以选后者。

**不会挡住正当重试**：`close-run` 先释放 lease 再关闭，进程若崩在两者之间，run **仍是 open**，重试
照样通过这道检查。

拒绝用 exit 1（USAGE，与 `open` 遇到重复 run id 的先例一致），并指出两条真正可行的路：`status` 读它，
或 `purge-run` 删它的行（而删除是有记录的）。测试除了断言「被拒绝」，还对**整个账本取指纹**断言
「一字未变」——「它拒绝了」和「它什么都没改」是两个声明，只有第二个才是要紧的那个。

### 有些 guard 是 hook 永远看不见的，而这件事此前不可见

`guards` 有两种形式，短形式 `action: STEP` 是**刻意保留**的：它声明一个**只有主动询问才能查到**的
guard，而那是某些动作唯一可能的机制（人在一个 driver 根本不碰的 UI 里点的发布）。所以它**不该被
载入期拒绝**——拒绝会误伤一个正当情形。

但它此前在两个该显示它的地方都不可见：

- `harness brief` 那节列的是**工具**（从 `matches` 推导），零匹配规则的动作贡献不了工具 → 读者会
  把工具清单当成全部受守面
- `harness abilities` 只报 guard 的**数量**

现在两处都报：`abilities --json` 加 `guard_reach`（`hook` / `ask_only`），文本形态标注
`(1 ask-only: publish)`，契约里加一句「Not every guarded action is in that list」并列出它们。

**穷尽核实过：5 个生产 flow 的 11 个 guard 动作全部可经 hook 触发；3 个仅可主动询问的都在 fixture
`delivery` 里。** 有测试钉住这个状态——不是禁止，是让第一个依赖它的生产 flow 成为**某人做的决定**，
而不是一次没人看见的漂移。

### 账本终于能被问「这里最常出什么问题」

`audit` 按 `(run_id, step_id, code)` 聚合——它回答「**这一个** run 里出了什么」，回答不了「这里**一直**
在出什么」。而后者需要的记录**早就都在**，只是从来没人问过它。

```sh
harness audit --recurring          # 跨 run 聚合；--json 同样支持
```

```
over 4 run(s) recorded here

Violations, across runs:
  ability          step     code                        runs    of  share  rows
  aaa              W01      budget_exhausted               2     3    67%     2

Gates recorded WITHOUT a witness, across runs:
  (the same events are above as violations — these are counted against the GATES
   recorded at the step, not the runs that reached it, which is the sharper
   denominator for a gate: a step can be reached often and gated rarely.)
  ability          step     unwitnessed  gates  share
  gg               W01                2      3    67%
```

**没有分母的计数是一个会引出错误结论的数字。** 「12 次」放在 200 个 run 旁边读作「偶尔」，放在 12 个
run 旁边读作「每次都失败」。所以每一行都带着它出自的总体，而且**两个小节用的是不同的分母**，因为它们
问的不是同一件事：

| 小节 | 分母 | 为什么是这个 |
|---|---|---|
| 违规 | **碰过这一步的 run**（不是该 flow 的全部 run） | 一个从未到达的步骤不构成「本来可以出问题却没出」 |
| 无证人的 gate | **在这一步实际记录过的 gate 数** | 一个步骤可以被频繁到达而很少被 gate；用「到达过的 run」会低估 |

两个小节会包含**同一批事件**（无证人 gate 既记 violation 又在 gate 表留痕），所以文本形态明写了这层
关系——否则读者会把一个发现数两遍。有变异钉住这句说明。

**排序以绝对次数领先，不以比例。** 只按比例排会把一条 1/1 顶到一条持续的 2/3 之上，而前者是噪音、后者
才是发现。比例仍然印出来，所以小样本是**可见的**，不是被一个百分数藏起来。

**`step_id` 一定连 ability 一起分组**：步骤 id 是每个 flow 自己的，只按 step 聚合会把两个作者各自的
`W01` 并成一条计数翻倍的「发现」，而那个步骤在两个 flow 里都不是它描述的样子。

**分母为零时印「—」而不是 0%。** 没有可除的东西不等于「一个都没有」——印 0% 会陈述一件账本没有说过的
事，而这是一个 rollup 唯一能悄悄编造事实的地方。

**被 purge 过的 run 不在总体里**，输出会报出 purge 事件数：账本是**剩下的**，不是**曾经发生的**。

### 哪里的流程是强制的：防遗漏，不防颠覆

守卫强制的是一个 run **内部**的门。它强制不了「run 被开过」——什么都没开时**没有 scope 可以拿来
比对一个动作**，于是它放行。引擎自己把这件事作为一条判断规则 ship 出去，因为一段请求 agent 去开
run 的散文绑不住 agent。

`<state_dir>/required-flows` 补上缺的那一维：一个条目说「在**这个** scope 里，**那个**流程是强制
的」，于是没有 run 声明 scope 时，守卫有了一个可比对的 scope。比对用的是 run 用的**同一个函数**
（`scope_covers`），只是 scope key 来自声明而不是来自一行记录。

```
第一问（本来就有）：任何 open run 覆盖这次调用吗？
    命中 + 有 gate → 放行     命中 + 无 gate → 拒绝     没命中 → 落到第二问
第二问（新增）：清单里有条目的 scope 覆盖这次调用，且那个 ability 认为这是受守动作吗？
    是 → 拒绝，并给出要开的那条命令
    否 → 放行（= 从前的行为）
```

**先说它做不到什么。** 有 shell 就有写权限，agent 能删掉清单——这个机制**防遗漏，不防颠覆**：

| | |
|---|---|
| ✅ 防遗漏 | 「忘了开 run」从**无痕**变成**被拦**。这是常见情形 |
| ❌ 防颠覆 | 删条目是可能的。这是罕见情形，而且**删除是一次明显的动作，不是一次省略** |

一个能被约束对象关掉的 fail-closed 机制，若被当成「强制」宣传就是表演。按它真实的样子讲——**「忘了」
和「决定了」之间的区别**——它值得有。

**空清单 = 与从前逐字节相同。** 这是硬要求：在有人写下第一个条目之前，这段代码只能放行，bug 半径为
零。（顺带删掉了一个「空清单就提前返回」的分支——`required` 为空时循环本来就不执行、照样落到末尾的
放行，那个分支改不了任何结果。）

**条目不存 `scope_kind`。** flow 自己声明了它，存第二份就会过期；而一个 kind 写错的条目会**永远匹配
不到任何东西**，静默无效——正是这个引擎要拒绝的那种「声明了却是惰性的」形状。所以它在使用现场从
flow 推导，并在写入条目时报出来给人看。

**被点名的 flow 读不出来时**（spec 坏、扩展未批准），默认**大声放行**——一份读不出来的 spec 不该
让一个 scope 里的所有工作停摆，这是这个 hook 所遵循的铁律。条目可以标 `strict` 改成拒绝，由**声明
的人**决定「我查不了」意味着什么，而不是由引擎替他决定。

**驱动契约跟着变诚实了。** 那条判断规则原文是「守卫在没有 run 时放行一切」——有声明之后这句话在那些
scope 里**为假**。现在规则带上了例外，而本机的实际清单作为一个小节渲染出来（**portable 副本刻意不
带**：那份签进了 git 并被字节守卫钉住，一个随机器变化的小节会让除作者以外所有人的守卫变红）。

**还没有被记录的豁免。** 一次正当的例外目前只能靠**删条目**，那是对文件的一次改动而不是一条有记录的
决定。设计里的下一步是让豁免复用引擎已有的两回合证人机制（`proof.vouch` + cursor），这样跳过流程会
留下一条 `harness audit` 能看到的记录，而不是让人去改文件。

## 命令面、集成与信任

命令面本身是数据，所以适配器不必把它重写一遍；扩展文件是代码，所以读一份 spec 会运行它。两件事都关于**边界在哪、谁为越界负责**。

### 命令面作为数据：适配器不该重写它一遍

`harness adapter-contract` 的 `surface` 键把整个命令面**从引擎自己的 argparse 解析器推导出来**：
每条命令的参数、类型、是否必填、枚举值、以及它有没有结构化输出。附带一张退出码名字表，好让适配器
不必自己抄一份**唯一真正是契约的东西**。

**为什么发布而不是让每个适配器自己写**：一个手写 tool schema 的适配器等于给命令面做了第二份副本，
而副本做的第一件事就是变旧。这个项目已经在同一件事上输过两次——手写的驱动文档漂离了引擎，于是改成
生成；生成的那份在某台机器上又比引擎旧了，一行一行地旧。从这里推导出来的 schema **说不出解析器
没有的 flag**。

`integrations/mcp-server.py` 是它的第一个消费者：stdio MCP 服务器，**每次工具调用 shell 出去跑
一次 CLI**。于是四条性质原样保留——扩展代码的 trust 每次调用都重查（不是每次重启）、spec 漂移每次
都比对、崩溃不留半成品、没有缓存所以没有失效 bug。代价约 200 ms/次，而模型一轮是几十秒；**延迟从来
不是做这件事的理由，schema 才是。**

**四条命令刻意不是工具**，理由与命令一起作为数据发布：

| 命令 | 为什么模型不该拿到它 |
|---|---|
| `guard-tool` | 由 **runtime** 在一次它可能阻止的调用之前发起，**从不由模型发起**。模型不会去调一个目的是阻止自己的工具——把它交出去等于用「需要 agent 配合的机制」换掉「唯一不需要 agent 配合的机制」 |
| `trust` | 批准的是**要以引擎权限运行的代码**。能自己批准自己代码的模型会让内容钉定变成装饰 |
| `init` | 创建/迁移状态库。让模型建库会把一个指错的 state dir 藏在自动修复后面，而错位置上的空库看起来就像一块干净的白板 |
| `purge-run` | 删已关 run 的行。测试残留清理，不是驱动 |

排除在**两侧**各查一次：引擎不把它们放进 surface，服务器启动时若在工具列表里发现任何一个就**拒绝
服务**。只由生产方保证的排除，离消失只差一次重构。

**退出码是产品，不是错误。** 3（拒绝）和 4（阻断）以数据形式返回（`{"exit": 3, "meaning":
"REFUSED", …}`），**不标记为工具故障**——把拒绝报成故障会诱导模型重试这次调用，而不是去读缺了什么。

**`--json` 用但不暴露**：能结构化作答的命令一律带上它，同时不把它放进 schema——那个参数唯一能做的
事，就是让模型选一个更难读的形态。

协议一致性刻意**委托给 `mcp` 包**而不是手写：手写等于声明一个这里的测试无法检查的一致性，而一个
无法验证的声明比一个依赖更不值钱。测试用 **SDK 自己的客户端**跑 initialize / tools/list /
tools/call，不是我拿 socket 自问自答。

### 扩展文件是代码：读一份 spec 会运行它

一个 flow 可以带 `providers.py`，而**装载它就是 import 它**。所以「校验一份你还不信任的 spec」这个
动作本身，会在引擎进程里以引擎的权限运行写这份 flow 的人的代码。这不是一个待打的补丁——它是扩展缝
的价格，而那道缝正是让领域知识留在基座外面的东西。

**先说这件事不做什么，否则它会被误读成保护：**

| 不是 | 为什么 |
|---|---|
| **不是沙箱** | 这里没有沙箱。用 Python 在进程内写一个是**一个守不住的声明**——逃逸路径众多且公开，交付它等于把一个可见的风险换成一个不可见的风险 |
| **批准不是安全判断** | 它记录你对某份文件**内容**做过的决定。被批准的文件以引擎能触及的一切运行 |
| **import 列表只是参考** | 从语法树读出，所以运行时拼出 import 名的文件不会出现在里面。它服务于人的审阅，**从不参与判定** |

**它做的、且可证伪的那件事：钉内容。** 来自引擎自身树之外的扩展必须按摘要批准一次，此后**文件一变就
重新询问**而不是直接运行。于是「我装的某个 ability 被更新了、它的代码变了」不再是静默的——而这是这整片
区域里，引擎保存的记录**真能**确立的唯一一件事。

```
$ harness validate ext
❌ ext
/outside/ext/providers.py
  is code from outside this engine's own tree and has not been approved here.
  digest:             sha256:8a5bff28…
  imports (advisory): engine, json, subprocess, sys
  Loading this flow IMPORTS the file, so reading its spec would run it. An ability
  with no providers.py is purely declarative and needs no approval at all.
  Review it, then:    harness trust ext
```

`harness trust` 报告**不经由 flow 装载**得出——审阅必须发生在批准之前，而装载它就是运行它。所以摘要与
import 列表是直接读文件得到的（有变异钉住这条：让报告走 `flow.load` 会变红）。

**引擎自身树内的 ability 不钉，而这是刻意的。** 能改 `<tree>/abilities/x/providers.py` 的人也能改
`<tree>/engine/facts.py`，**引擎保存的记录活不过一个能编辑引擎的人**。钉它是表演，而信任功能里的表演
正是最坏的失败模式——所以检查只在边界真实存在的地方：代码来自引擎自己的代码不在的地方。按 pip 装好之后
引擎树里没有任何 ability，于是每一个 flow 都是外部的、都要钉。

**没有为它新加退出码，这也是刻意的。** 这个引擎当初拆 3/4 的判据是「一个机器消费者需不需要按它分支」，
而信任拒绝与坏 spec 对适配器来说动作**完全相同**（都 allow、这个 flow 都用不了）。不同的只有**人**的
下一步——那属于散文，于是改的是驱动契约里 exit 2 的建议：它此前只说「上报上游」，一个撞上未批准扩展的
driver 会去开一张单然后卡住，而正确动作是这台机器的主人跑一条命令。加一个没人分支的码，就是本项目一直
在删的那种「只有一种取值的键」。

**加载标记在检查之后才置位。** 先置位会让一次被拒的 import 把该 ability 记成「已加载」，同一进程里的
第二次尝试就直接放行——**一个响过一次就不再响的守卫**。这条也有变异钉住。

### 读和写都能用 JSON 作答

```sh
harness next --run <id> --json      # requirements 是真数据，不是列
harness status --json               # 含每个共享 scope 能不能被裁决
harness open <ability> --scope <k> --json   # 省略 --run 时，生成的 id 作为字段回来
harness evidence ... --json         # rows_now：这一步这个 kind 现在有几行（min_count 就数它）
harness gate ... --json             # witnessed 与 violation_recorded 分开报
harness close-run --run <id> --json # result_source：flow_default / explicit / engine_fallback
```

退出码本来就是契约，但**细节此前只有人类散文**。不是 shell 的东西——第二个运行时的适配器、一个
面板、一个监控——只能拿正则去啃格式化文本。而 `next` 的整个卖点是「把这一步要求什么**以数据形式**
说出来」，它此前说成了列。

而读侧补齐之后剩下一个更别扭的不对称：**driver 可以不解析文本就知道一步「要求什么」，却必须解析
文本才能知道自己「做了什么」**——包括 `open` 刚刚生成的那个 run id。

#### 拒绝不会沉默，而这是结构性的不是纪律性的

`--json` 是一个承诺：**stdout 承载答案**。而最需要它的是失败那条路——9 个写命令从 **26 处**拒绝，
另有三个共享助手（`_run_or_exit` / `_load_flow_or_exit` / `_scope_taken`）**代它们**拒绝，位于命令
作者根本不会编辑的函数里。「每加一处拒绝都要记得同时 emit JSON」这种规则，忘一次之后对 JSON 调用方
读起来就是**一个空答案**——而空 stdout 最自然的解读是「成功了」。

所以它挂在 `_err` 上——那本来就是所有拒绝**为了说出任何话**都必经的唯一漏斗：

```
{ "command": "close-run", "code": 3, "code_name": "REFUSED",
  "why": "⛔ REFUSED: 5 step(s) still open: A1, B1, B2, D1, D2\n    Pass --force-steps ..." }
```

那句话**故意以散文形式**留在 `why` 里：它是「缺什么 + 补它的命令」，重编码成字段等于同一条消息的
第二份副本，随时可以和 stderr 打印的那份漂开。有**机器可用的区分**的站点（如 `close-step` 的
`deps_open` / `criterion_unmet`）自己先 emit 带 token 的信封，backstop 对它们不触发（有变异钉住
「不会盖掉更精确的那一份」）。

**唯一服务不到的情形**：命令行本身写坏时 argparse 在任何东西读到 `--json` 之前就退出了。它留在散文，
而这是诚实的——「请用 JSON 回答我」这个请求本身就是没解析成功的那部分。

#### 成功侧的沉默是 exit 5，不是安静的 0

`_err` 让 backstop 能替一次拒绝说话；成功没有对应物，因为**答案根本没被构造出来**。所以一个接受了
`--json` 却在成功路径上什么都没产出的命令，现在报 **INTERNAL（5）**——这个 CLI 给「这是我们自己的
缺陷」保留的码。

这不是假设：**`next --json` 在「全部步骤已关」那条路上一直打印散文**，从这个 flag 存在的第一天起，
因为没有任何测试走过那条路，也没有任何东西说过。它是被这轮那个「只读 JSON 的 driver」测试撞出来的。

#### 每个命令都被分类，缺口是具名的

`PROSE_ONLY`（各带理由，名字见下表）与 `NO_JSON_YET`（**现已为空集**，且有测试钉死它为空）
**分区整个解析器**，有测试断言这一点。于是
一个新命令**无法靠遗漏**加入沉默阵营——它不会被分类，而那会红。

分类也在 `adapter-contract` 里发布，因为「按决定没有机器形态」和「还没有机器形态」对适配器不是同一
件事：

| | 命令 | |
|---|---|---|
| `PROSE_ONLY` | `brief` `show` `validate` `adapter-contract` `guard-tool` `init` `purge-run` | 各有理由，如「退出码就是答案，旁边再放一份 payload 会引人去解析 payload」 |
| `NO_JSON_YET` | *（空）* | 表**留着不删**：它是下一个没有机器形态的命令必须落进去的地方。删掉它等于把那个命令放回分区测试要消灭的那片沉默里 |

### `guard` 的裁决是闭集，因为它的 allow 互不等价

`guard` 回答一个问题：这个动作可以进行吗？而它的 **allow 有五种**，一个裸 `allowed: true` 恰好抹掉
这个引擎一直在守的那条区别——**「这里没有东西要守」和「我看不见要守什么」不许长得一样**。

```
blocked           有 run 守这个动作，而它要的门没有记录          exit 4
no_run_in_scope   这个 scope 没有 open run                    ← 没有东西要守
not_guarded       有 run，但它们的 flow 都不声明这个动作
gate_recorded     它要的门已经记录了
unadjudicated     多个 run 共享 scope 且无可用租约 → 归属不可判定（并记一条 violation）
store_unusable    账本缺失或 schema 太新：**无法**守，而且连「无法守」都记不下来
view_incomplete   至少一个 open run 的 flow 读不出来           ← 我看不见要守什么
```

有测试断言**每一个声明的裁决都可达**（声明了却没人能产出的裁决是死重），而每个裁决自带一句说明，
所以调用方不用从名字去猜含义。

#### 顺路补掉 ask 侧的同一个洞

`guard-tool`（hook 侧）上一轮补过「读不出 flow 的 run 被静默跳过」；**`harness guard`（主动询问侧）
还留着同一行 `continue`**。同一个 run、同一个动作、同一个账本：

```
读得出   → verdict=blocked          exit 4
读不出   → verdict=not_guarded      exit 0     ← 修之前：一个【引擎无法支撑】的声明
读不出   → verdict=view_incomplete  exit 0     ← 现在
```

一旦这个命令以数据作答，`not_guarded` 就不再只是「少说了一句」，而是**引擎在陈述一件它没法背书的事**。

#### 信封不许报告它没检查过的东西

第一版我在**第一个阻断处就返回**，于是 `unreadable` 会是空列表——因为链上更后面的 link 根本没被看。
**空列表读起来是「一切都可读」**。改成先扫完整条链再裁决（链只有一两节，成本为零），于是阻断信封
同时带两个都为真的事实：

```
verdict    blocked
blocked_by {'run': 'la', 'step': 'G01', 'link': 1, 'of': 2}
unreadable [('lb', 'zzz_b')]
```

而 `unadjudicated` 那条**根本不带 `unreadable` 键**——它在读任何 flow 之前就判定了，凭空加一个
汇报「未尝试的工作」的键是同一个谎的另一个方向。两条都有变异钉住。

实现是**一份数据两种渲染**（`_emit`）：另建一份机器形态等于把同一个查询实现两遍，而其中一个先长出
新字段的那一刻它们就漂了。

`validate` 刻意没加：CI 要的是它的**退出码**，而那早就是契约；它的细节是给人调试看的，是这批里
价值最低的一个。

### 一个只有一种取值的键说不了任何事

`on_exhausted` 在「拒绝」与「升级到人工 gate」之间选择。**从来没有任何 flow 选过升级**——已装 flow
里 8 处声明，每一处都在复述默认值。删掉没人用的那个分支会留下一个单值键，所以**键也一起删了**。
预算用尽现在无条件拒绝。升级要回来，走和任何东西一样的路：一个取值、它的分支、以及一条选择它的
flow。

## 接线与运维

把引擎接到一个真实 runtime 上，然后回答「已经发生过什么」和「能力装在哪」。

### 接一个 agent：一段生成的 prompt + 一份契约

```sh
harness init                  # 顺带把本机契约写到 store 旁并打印路径
harness brief --write         # 或单独刷新；agent 的 resources 指这个文件
harness adapter-contract      # 适配器必须满足的用例（JSON）
```

**两份副本，别指错**：`$XDG_STATE_HOME/harness-engine/brief.md` 是给 agent 的（本机真实路径，
可直接跑）；`integrations/DRIVING.md` 是给人在仓库里看的参考副本（`--portable`，路径是占位符）。
签入的那份开头会自述身份并指向 `--write`，因为**这条真的错过一次**——一个 agent 配置指着签入
副本，于是 agent 被告知 alias 一个占位符。`--write` 与 `--portable` 同时给会被拒。

**新鲜度靠 `init` 而不是靠记性**：它每次都重写那份契约。一份没人重新生成的生成物，就是一份多
几个步骤的手写文件。

**`brief` 是生成的，不是手写的**，因为手写的那份如实漂了：两天之内它写着一个已经搬走的状态库
位置、一张少一档的退出码表、一条只在一台机器上成立的路径、以及两个都不对的计数。而它旁边的
`CAPABILITIES.md` 有 4 条测试守着、始终正确——**唯一没有守卫的那份就是烂掉的那份**。

凡是引擎自己知道的，现在都派生：真实存在的子命令、真实的退出码、三个环境变量、可路由的 flow
及其 `when:`、以及 **hook 的 `matcher` 该覆盖哪些工具**（此前要人手写一段 heredoc 去算）。

**并且它按安装裁剪**：小节只在已装 flow 真的用到那个机制时才渲染。本树 17 节；一个只有 gates
的单 flow 安装 8 节。这不是为了短——**agent 读到的每一段用不上的机械，都是花掉的预算，也让
真正适用的那几段更难找到**。

不可派生的只有 6 条判断规则（记录不等于做完 · 背书要两轮 · 引用人的原话 · 不要预载全部散文 ·
挡住你的 spec 不是要改的 spec · 没有东西强迫你开 run）。它们作为**数据**住在 `engine/brief.py`
里，于是**纯净性守卫会扫到它们**——一条只能用某个消费方的词汇讲出来的规则会被那道扫描打红，
所以能活下来的规则就是对每个消费方都成立的规则。

签入的 `integrations/DRIVING.md` 由 `--portable` 生成（占位路径，不带任何一台机器的绝对路径），
并有一条测试断言它与命令输出**逐字节一致**。它不能漂。

#### 换一个 agent 运行时：发布用例，不生成代码

适配器天生一个方言一份、语言各异（一个是读 stdin 的 python hook，另一个是某个类型化运行时的
插件），**代码生成不了；但它必须满足的用例可以发布**。`adapter-contract` 给出三段：

| | 内容 |
|---|---|
| `translation` | 引擎退出码 → allow/block，5 条。**只有 BLOCKED 变成拦** |
| `resilience` | 输入非 JSON / 无工具名 / 引擎缺失 / 调用超时 / 适配器自身崩溃 —— 5 条全部放行，其中「引擎缺失」还必须**在 stderr 说出来** |
| `end_to_end` | 从已装 flow 派生的真实 action / gate 步骤 / 工具 / 正则 |

`end_to_end` 如实标着「载荷要你自己构造」：**一个匹配任意正则的字符串没法从正则反推**，而一个
碰巧不匹配的假载荷会让用例通过却什么都没证明。承认交不出的那一半，比交一个看起来完整的空壳好。

这些用例**不是摆设**——本引擎自己的适配器测试就在迭代它们，用一个按码退出的桩引擎把「翻译」
这一件事单独隔出来验。

### 已经发生过什么：`history`

关掉一条 run 从来不删任何东西，但此前**没有任何命令列出它们**——账本完整而不可读，对一份记录
来说等于不存在。

```sh
harness history                      # 结束的 run：结果 / 步数 / gate 数 / violation / 欠的义务 / actor
harness history --actor <name>       # 某个 driver 的
harness history --actors             # 跨 agent 的一个地方：各自多少 run、多少 violation
```

`--actor` 的过滤**在 SQL 里**而不是取回后再筛：先取最新 N 条再丢掉别人的，得到的是「最新 N 条里
属于他的」，而不是「他的最新 N 条」。有一条测试专门把数据摆成「过滤在 limit 之后就什么都返回
不了」的形状。

`audit` 的 violation 行也带上了 actor——**记了却没有任何地方报，就是声明了没人读**。而 scope 级
的那种（`guard_unadjudicated`，不属于任何 run）**不会被归给任何 driver**，这也有一条测试钉住：
它属于一个 scope，那正是它一开始就不带 run 记录的原因。

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

**`scope_kind` 的值也从磁盘派生，而这条此前是缺的。** 一个 run 的隔离轴是
`(scope_kind, scope_key)`，而 `scope_kind` 在载入时**只被校验非空**——没有闭集。这正是同一个引擎
能同时服务「关于一个仓库的流程」「关于一次评审的流程」「关于一本笔记的流程」的原因：那些词一个都
不在引擎里（实测 7 个已装值在引擎源码里出现 **0 次**）。

但**没有任何东西在维持这一点**：上面那条从磁盘派生的名单派生的是 ability 的**目录名**，而
`scope_kind` 的**值**是另一个字符串，且没有一个出现在手写禁用名单里。也就是说一句
`if scope_kind == "repo"` 能通过这个文件里的每一条测试。现在有一条测试从各 flow 的
`scope_kind:` 派生出全部值并禁止它们出现在引擎代码里（变异验证：加一句那样的特判 → 报
`flow.py:841 "repo"`）。

**它只禁引号字面量，而这是量出来的决定不是偷懒。** 标识符形式试过，在这里不可用：有一个已装的
值只有两个字符（`cr`），于是 `_cr` 会命中 `obligation_created`——第一次跑就是四处无辜命中。
**噪音探测器换来豁免清单，豁免清单会长大，长大的豁免清单就是耦合回来了**——这个文件对工具名的
论证是同一条。而引号形式恰好就是「耦合」的那个形状：`if scope_kind == "..."`、
`HANDLERS = {"...": ...}`、`KIND = "..."` 全部落网。

**一处顺带要说清的**：`scope_kind` 是**跨 ability 共享的命名空间**。两个 ability 用同一个值就共用
同一片 scope——给两条 flow 写上同一个 `scope_kind`，在同一个 key 上开了一个，另一个就会被拒：

```
⛔ scope repo='<repo>' already has 1 open run(s)
```

这不是耦合，而是这套设计的要点：守卫回答的是「这件事能不能对**这个东西**做」，所以两条都作用于
同一个仓库的流程**必须**互相看见，而 `scope_lease` 就是给它们裁决用的。反过来说，两条流程要能并行，
它们得在谈论不同的东西——那时 `scope_kind` 本来就该不同。

**而它自己也曾静默过期。** 禁用名单里那条「已装 ability 的名字」是**手写的**，注释写着
「the two installed abilities」——而当时已经装了 **7 个**。也就是说其中五个可以被引擎代码
点名，而这道门栓不会有任何反应。修法是**从磁盘派生名单**，而不是再补五个字符串。

派生之后有一个必须做对的判断：**不能用子串匹配**。能力的名字往往就是普通英文词 ——
`delivery`、`authoring` 都是普通英文词；引擎自己的散文里就有句子在正常地使用这类词
（`predicates.py` 一句 docstring 引用真实不变量：「the revision whose checks were verified
must BE the latest revision **pushed**」）。子串规则会因为一句英文散文而失败，**而一条会被
散文打红的守卫会换来一份豁免清单**，那正是这个文件存在的理由所要阻止的东西。所以禁的是
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

### 三条从「接外部工具」里学到的，与具体工具无关

这三条各自来自一次真实的接入尝试，原始记录随那个工具走了，只有判据留在这里：

- **接一个外部工具之前，先问「这里有什么可比的东西」。** 没有可比物时，工具产出的是一个
  无法被反驳的数字。一个不能被别的来源检查的度量，进了账本也只是好看。
- **有一种工具不能 vendor：它的价值是它自己累积的状态。** 把它的代码抄过来，抄不到那份状态，
  于是得到一个形状相同、内容为空的东西 —— 而空的那个看起来是在工作的。
- **引擎对它服务的流程应当是零接触的。** 引擎不该要求那些流程改自己的文件、目录或习惯；
  它们只需要多出一份 `flow.yaml`。这条是可检验的：接入之后，那些流程的树里改了几个字节？

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

### 一条判据的数字可以在 run 里被发现，而不必写进 spec

`evidence` 的 `min_count` 是写在 spec 里的字面量，所以它只能表达「在写 spec 那一刻就定下来的数量」。
而有一类要求不是那种形状：**「每个对象一条判断」，而对象有几个是跑起来之后才知道的**。

```yaml
completion: {type: counts_at_least, kind_a: verdict, kind_b: commit}
```

两侧都来自**账本**，这才是它成为判据而不是请求的原因：对象是这个 run 收集时记下的行，判断是它工作时
记下的行。「我全部看过了」无法核对，「一边 24 条一边 20 条」可以。

```
⛔ 2 'verdict' row(s) for 5 'commit' row(s) — 3 short.
    Record one 'verdict' per 'commit'; the shortfall is what is left undone.
```

**空过会被说成空过。** 0 比 0 满足这个不等式，而且必须满足 —— 一个什么都没有的窗口是正常结论不是
失败。但「满足」在 0/0 和 24/24 上读起来一样，正是这个引擎反复被纠正的形状，所以句子会说它是空的：

```
✔ 0 'verdict' for 0 'commit' — VACUOUS: there was nothing to cover.
  Whether there SHOULD have been is not this criterion's question.
```

而「本来该有多少」由收集那一层的判据管 —— 那才是知道窗口有多宽的地方。这个谓词不猜它。

**它和 `fields_agree` 的区别正是它存在的理由。** 后者比两个**值**相等；如果让 agent 记
`subject_count=24` 和 `verdict_count=24`，它满足的是一个自己写下的等式。而这里每一行**就是**一个
对象、一条判断，数字是「做了什么」的性质，不是「怎么说」的性质。有测试专门钉住这一点：记两行各自
声称 24，判据只数到 **1**。

#### 而这条判据的位置是我第一版写错的地方

引擎有一条不显眼但要紧的性质：

```
step 的 completion   evidence_scope = 该 step 自己   → 只看本步骤记的行
phase 的 goal        evidence_scope = None          → 看整个 run
```

`commit` 记在一步、`verdict` 记在另一步。把这条判据写成后者的 **step** 判据，它看到的是 0 和 0 ——
**永远空过**，而且空过时还如实说自己是空的，于是判据看起来在、实际什么都不管。**两侧不在同一个 step
的比较，位置只能是 phase goal。**

而 phase goal 恰好是「这一层做完了没有」的正确问法，并且它在关 run 的必经路上：一个 phase 的 goal
里写 `phases_summarized`，`close-run` 就要求其余 phase 都被 summarize，而 summarize 一个 phase 要求
它的 goal 达成。少了那一条，前面几层的判据就在旁边而不在路上。

### 强度分层，以及为什么要如实报它

`validate` 打印每个 ability 的判据强度分布。下面这份输出取自**上一节那条 102 步的流程**
（同样不随发行包分发）——`role: fixture` 的能力**刻意不报强度**，所以两个夹具身上看不到这几行：

```
✅ <一条 102 步的私有流程>: 102 steps, 8 phases, 5 guard(s); order ok
   criteria: derived×21 · value-checked×47 · record-exists×34 · UNCHECKED×0
   artifacts: 151 pinned across 102 steps (37 step(s) pin ≥2)
   goals: 8/8 phases  [context:derived/config:derived/...]
```
（摘录：真实输出另有 `uses:` / `capabilities:` / `prose:` 三行。）

分层是从 `requirements()` 派生的，不是手维护的表——所以新加谓词会**按它索取什么**被自动分类，
报告不会和注册表脱节。

**为什么不只报「非 attest 的有几步」**：一条流程可以 100% 非 attest，同时几乎全是自陈。
`record-exists` 强制了一个显式记录动作、留下可审计的内容行，这比 `attest` 强，
但它**不是机器裁决**——把两者合报成「machine-checked: 102/102」是真话，
而它听起来比实际意思强得多。

### 升级 record-exists 的纪律：值必须来自散文

把 `record-exists` 的占比压下来，做法不是给每步编一个允许值集合，而是只处理**散文自己指名了结果**的步骤：
「命中则缓存」「已提议或显式跳过」「残留 = 0，或残留都已解释」「无编译错误」「历史保持单条」
「每条意见归类为『该处理』或『固有噪声』」。凭空发明枚举比留在 `record-exists` **更糟**——
它看起来更强，而它检查的是一个我编的东西。

升级形态一律是 `all_checks[evidence(内容 kind), 裁决检查]`，**两个 kind 而不是一个**。
只换成枚举会用「强度」换掉「内容」：`evidence_in(kb_entry, [written, skipped])` 能通过，
而账本上再也看不出写进去的是什么。

**升不上去的那些是真天花板**，占比最大的两类：**纯存在性产物**（散文的判据是
「这份表/块/文档产出了且清晰」，没有可枚举的值）和**全称但无裁决词汇**（「同类点已穷尽」
「下游影响已列全」——断言的是穷尽性，需要一个外部预言机而不是账本里的值）。

### 完整性是与强度**正交**的第二个维度，而强度会掩盖它

一步产出三样东西、只钉住最强的**一样**，在强度报告里得分很好，同时三分之二的产出没人查。
所以 `validate` 另报**钉住的产物数**——就是上面那份输出里的这一行：

```
artifacts: 151 pinned across 102 steps (37 step(s) pin ≥2)
```

「跨 102 步钉住 151 个产物，其中 37 步钉了两个以上」——**`pin ≥2` 的那一列才是重点**：
一步只钉一个产物时，强度报告仍然满分，而它另外两个产出没人查。
两个数字缺一不可：只看强度会漏掉「一条判据代替了好几条」，只看数量会漏掉「判据全是自陈」。
两条都有防回退基线测试。

**这个维度上的两个陷阱**（都是散文自己指出来的）：

- **完成标准与失败条款矛盾时，失败条款赢。** 一步的完成标准要求「描述已随之刷新」，
  而同一份散文的失败条款明说「没更新就发一行告警……但**不阻断**」；另一步要求「分数已落库」，
  失败时说「写分失败只记 warning、不阻断收口」。把这两项写成硬产物会让流程卡在
  散文明确允许通过的地方。正确做法是把**处置**记成枚举（`refreshed|warned`、
  `persisted|warned`）——比要求它成功更符合意图，因为散文要的是
  「分数缺失是审计信号」，而一个没被记录的 warning 不是信号。
- **不是每个「看起来像合取」的完成标准都是合取。** 有一步的完成标准只有「评分已 emit」一项，
  而散文里另一句「等待中先不 emit」是**时序前提** —— 它已由 `deps` 结构性保证，
  不需要也不应该塞进完成判据。合取里多一条已被结构保证的东西，等于把一条永真项当成守卫。

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
python3 -m pytest tests/ -q      # 全绿；条数见开头那张生成的表
```

分两类：

- `test_engine_purity.py`（71）—— **结构性不变量**。引擎源码里不许有 step id 形态的
  token、不许有能力专有名词、不许有能力词汇当标识符、不许 import 或硬编码路径进 abilities、
  **不许把任何已装 flow 的名字写成字面量或标识符**（名单从磁盘派生，不手写）。
  另有一条反向测试确保词汇**真的**在 spec 里（否则纯净测试可以被一个啥也不干的引擎满足），
  以及一条守卫的守卫（文件集合为空时不许静默通过——因为检查了 0 个文件而变绿是最糟的绿）。
- `test_behaviour.py`（317）—— 全部断言**退出码数字**而非文案。hook 判断的是数字；
  如果重构保留了措辞却改了码，强制就静默消失，只有这些断言会发现。

四条关键守卫做过变异验证（去掉守卫 → 测试必须变红）：guard 的 exit 4、witness 的同轮
重复检测、close-step 的谓词校验、同 scope 并发预防。

### 手写文档也有守卫了，而可推导的那部分改成生成

`integrations/CAPABILITIES.md` 在测试里有断言；`integrations/WIRING.md` 一条都没有——**而漂掉
四处的正是后者**，四处全是写下时正确的数字。没人看着的那一份就是变旧的那一份。

修法按声明的种类分成两半，而不是给所有声明加断言：

| 声明的种类 | 怎么处理 | 为什么 |
|---|---|---|
| **可推导**（条数、体积、默认路径） | **生成**：`integrations/render-wiring.py` 渲染进一个带标记的块，测试断言文件与重新渲染逐字节相同 | 生成的事实**过期就红**；被守的事实只能事后抓到它已经旧了 |
| **引用**（命令、flag、路径、env、常量、被引用的消息） | 断言它**真的存在** | 一份指名已改名命令的文档读起来像指令，也就按指令失败 |
| **判断**（为什么 hook 是唯一强制点、为什么隔离轴是 scope） | 留在散文里，不检查 | 不可推导，不该进代码，也不会因重构变旧 |

`WIRING.md` 开头本来就写着自己的规则——「刻意不复述任何引擎事实」——而那条规则此前**只是一句
声明**，于是被违反了四次。现在它有两样东西托着：那张生成表，以及一条把它提到的每个引用逐个核对
的测试。

**外部引用以数据形式豁免，连理由一起。** 那份文档引用了源系统的四个常量（转写的来处），它们不在
本仓库里。豁免表里每条都要写理由，且理由本身有长度断言——**一条没有理由的豁免，就是一条悄悄停止
生效的检查**。

### 可重定位性也是被测试证明的

这棵树会被复制到云桌面、会被交给别人。所以一个写死在 macOS `/Users` home 下的字面路径，
在 Linux 上直接是错的，
而一个指向某人 home 的字面路径对除他之外的所有人都是错的 —— **两者都不会响**：读者只是照着
一条对他不成立的指令做，而作者永远看不到，因为在作者机器上它们都是对的。

这和 README 曾经漂掉 8 个数字是同一个形状（写下时正确、没人看着、后来变错），所以它同样
拿到一套机制而不是一条习惯。`tests/test_portability.py`：

| 禁 | 为什么 |
|---|---|
| 根在 home 或厂商目录的路径（`/Users`、`/home`、`/opt`、`/Library` 这些） | 它们编码了一个操作系统，通常还编码了一个人 |
| **这份 checkout 自己的绝对位置**（从磁盘派生，所以规则不会过期） | 写死自己在哪的脚本只在写它的那台机器上成立一次 |
| **当前用户的登录名出现在路径段里**（从环境读，不写死） | 那正是「分享给别人」会坏的那一处 |
| 夹具能力伸到自己目录之外（`~/`、`$HOME`、`../..`） | 一个悄悄需要作者 home 里某个文件的 sample，比没有 sample 更糟 |

**`~/` 和 `$HOME/` 不禁**，它们能正确重定位 —— 这个引擎本来就依赖这一点：`harness require`
存的是字面 `~`，只在比较时才展开，这才让一份策略记录能跨机器用。

**看起来像绝对路径的占位不禁**（`/path/to/your/flows`、`<tree>/abilities/x`、
`…/delivery/providers.py`）。一条「文档里不许出现前导斜杠」的规则会把这些也判红，而它们是
写文档的正确方式 —— **一条会误伤好做法的守卫会换来一份豁免清单，而清单一长，规则就悄悄变成建议**。

**不扫非 `role: fixture` 的能力**，这是对的：那些是私有流程，它们的 provider 本来就指向真实的
本机工具，而且它们不随包走 —— 把它们泛化掉等于弄坏它们。发行面是从磁盘派生的：引擎、入口、
集成、测试、根文档，加上作为 sample 的夹具能力。

写这四条守卫时它们抓到的第一个违规者是**守卫文件自己**（docstring 里为了举例写了带尾斜杠的
`/Users` 形式）和 `pyproject.toml` 的作者署名。前者改措辞而不是加豁免；后者说明守卫写宽了 ——
一个包本来就该署名，真实危害是名字**当作目录用**，于是判据收窄成「login 出现在路径段里」。

## spec 语言的边界

这一节是**压测结果**，不是设计意图。做法是把一条成熟的真实流程程序化转写成一份 spec —— 一条
**102 步 / 8 phase / 24 stage** 的流程，从它源系统的步骤注册表导出而不是手写（手写一百多步必错）。

> **那条流程不随发行包分发**，所以下面每个数字都是**一次过去的测量**，你在这棵树里复现不了它 ——
> 和「整套测试在 3.10.16 上跑过」是同一类声称。它留在这里是因为它是关于**引擎**的证据：
> 换成两个玩具夹具，下面这些结论一条都得不出来。转写的出处细节（哪张注册表、哪些表、哪些步骤 id）
> 属于那条流程自己，不在这里。

它**通过校验并能跑**，这一点本身有意义：spec 语言在 102 步的规模上不塌。但结论是
**结构能表达，控制流不能。**

### 引擎赢的地方（两条，都是实测）

**① load-time 校验抓到了转写里的真实建模错误。** 第一版 exit 2 报的是这个形状：

```
guard 'task_close' points at step 'X', whose gate is 'none'
```

原因在源系统那边：「哪些 step 是 gate」**散在三张表里**，转写时只读了第一张，于是有一步
漏了 gate，而一个指着无 gate 步骤的 guard 是**看着有保护、实际是空**。
把 gate 归拢到「每步一处声明」的收益，就是这种漏会在**加载时炸**而不是在运行时静默。

**② 转写过程暴露了引擎自己的一个缺陷，已修。** 源流程有第三层分组，而当时的 spec 无处安放它 ——
写下 `stage:` 时它**被静默忽略**了。同一个洞让 `gaet: affirm`（拼错的 `gate`）静默产出一个无 gate
的步骤，正是上一条那类「看着有保护、实际是空」。现在未知 key 一律 exit 2。

### 引擎输的地方（四条，全部是真实构造，引擎表达不了）

| 缺口 | 真实流程里长什么样 | 现在被迫降级成 |
|---|---|---|
| **第三层结构** | layer → **stage** → step，24 个 stage | ✅ v2 已实现 `stage:` |
| **可选的整层** | revision 层 16 步，只在有 findings 时跑 | ✅ v2 已实现 `optional:` |
| **互斥分支** | 关单的「是 / 否」两条分支，二选一 | ✅ v2 已实现 `exclusive_groups:` |
| **per-step 的降级策略** | 少数几个 gate 必须 fail-closed，其余可降级 | ✅ v2 已实现 `strict_witness:` |
| **loop + 迭代预算** | 修订循环 3 次预算 + 冲突重基的**独立**上限 | ❌ 仍未做（见下文，故意的） |

另外两个较小的，**至今没做**：
- **条件步骤**：某一步「仅在复杂度为 X 时适用」——步骤是否适用取决于 run 级变量，引擎无此概念。
- **单向棘轮**：一个 run 级的等级只许升不许降。引擎没有棘轮维度。

### 这份对照给出的结论

引擎当前的形状天然适合**线性带 gate 的流程**（两个夹具都是这种），
而一条成熟流程还会有 **loop / branch / conditional / 第三层分组**。

**这次压测没有做的事**：它不是移植 —— 没有一行代码从那条流程的源系统复制过来，
它只回答「spec 语言够不够用」。答案是「结构够、控制流不够」，而下一节是把不够的那部分补上。

### 四个控制流原语（v2 已实现）

v1 的四个缺口都补上了，那条 102 步的流程现在**能真正跑到关闭**（同一次压测，同样不可在本树复现）：

```
驱动结果：closed=82  skipped=29  gated=6  人类发言=4
close-run → exit 0（未用任何 --force-* flag）
audit → unwitnessed: 0，无 forced_close
```

这几个数字里**最要紧的是 `skipped=29` 和 `人类发言=4`**：前者说明「可选」真的可以合法地永不运行，
后者说明 gate 没有被一次性批量糊过去 —— 一个人在四个不同的回合里说了话。

| 原语 | 语法 | 语义 |
|---|---|---|
| **`stage:`** | 步骤上 `stage: pre-flight`，phase 上 `stages: [...]` | 第三层分组。stage 必须被它的 phase 声明，拼错 → exit 2 |
| **`optional: true`** | 步骤上 | 可以合法地永不运行。`close-run` 只要求非 optional 的步骤；`skip` 命令**只对 optional 生效**（必需步骤能被运行时跳过，流程就变成谁想跳就跳） |
| **`exclusive_groups:`** | 顶层 `- [X1, X2]` | 互斥分支。关掉一个 → 兄弟自动记 `skipped`，该组只算一次。**依赖单个分支是 fatal**（另一分支被选时依赖方永远不可达） |
| **`strict_witness: true`** | 有 gate 的步骤上 | 这个 gate 不接受降级：无 witness 直接 exit 3，不记「带标记的通过」 |

`status` 按 stage 渲染，`●` 已了结 / `◌` 可选未动 / `○` 仍欠：

```
  <phase> (<phase>（8 步）)
    ●●             <stage-a>
    ●              <stage-b>
    ●●●            <stage-c>
  owed    nothing — closeable
```

### 实现过程中又抓到一个真 bug

手写 Y/N 分支时用了 `- id: YES` / `- id: NO`，结果 **YAML 1.1 把它们解析成布尔值**——
step id 变成了 `True` / `False`（错误信息里 `known: A, True, False, Z`）。
`str()` 会把 `True` 转回字符串 `"True"`，于是它「能跑」但 id 不是作者写的那个，
而文件里其它地方对 `NO` 的引用全部解析失败。

`ON`/`OFF`/`Y`/`N` 同理，`07` 会变成 int 7。这正是这个加载器存在的理由——静默改变语义。
现在非字符串 id 一律 exit 2，并提示加引号。

**这个 bug 是「真的去用它」抓出来的，不是想出来的。** 光写两个玩具夹具永远碰不到它 ——
这也是这一节即使主体不随包分发也要留下的原因：**夹具证明机制存在，规模证明机制够用**，
而后者只有把真东西压上去才拿得到。

### loop + 迭代预算（已实现）

`repeatable` + `budget: N` + `on_exhausted: refuse | escalate`。

**「预算用尽怎么办」由 flow 声明，不由引擎决定。** 计数器一引进来引擎就必须回答这个问题，
而在引擎里回答等于把一个能力的答案烙给所有人。校验会拒绝「声明 escalate 但那一步没有 gate」
的 spec——那样人的决定没地方记录。

压测里的用法说明了为什么「独立计数」要能表达：修订循环有 3 次预算，而**冲突重基单独计 3 次** ——
理由是它解决的不是「意见」，因此不该占那条预算。两者耗尽都是 `refuse`：拒绝继续、换方法或叫人，
而不是偷偷把上限调大再来一轮。升级路径由**一个独立的步骤**表达，不是引擎的 escalate 模式。

### violation 的两种性质必须可区分

同一个账本上混着两件事，而它们方向相反：

| severity | 含义 |
|---|---|
| `blocked` | 一次尝试被**拒绝**。保障生效了，这行是审计痕迹 |
| `breach` | 有东西**带标记地通过了**。保障没生效 |

不区分的后果是具体的：终点判据 `no_open_violations` 会因为**引擎工作了**而让 run 关不掉，
于是「不去尝试」比「守规矩」更划算。更具体的是，`unwitnessed_gate` 这一个 code 曾经
既用在「拒绝伪造」也用在「接受降级」上——语义相反，而下游分不出来。
现在前者叫 `gate_refused_unwitnessed` 且是 `blocked`。

默认值是 `breach`：忘传的调用方拿到**严重**的那个，安全的默认是多报 breach，
绝不是把 breach 静默降级成审计注记。

## 运行时条件与义务

引擎的条件层**不绑定任何具体事实**。四个注册表现在形态一致：

| 注册表 | 扩展什么 | 内置 |
|---|---|---|
| `predicates` | 「这步做完了吗」 | 15 个 |
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

## 作用域与强制的边界

一个动作**算不算落在这条流程管的范围里** —— 以及当两条流程共用同一个 scope 时，谁拥有这个动作。这一节里每一条都是某个绕过路径被关掉的记录。

### 一个位置 scope 可以按「调用自己指名的目标」判定

`path_prefix` 只问**调用方在哪**，而一次工具调用可以作用于它所在之外的目录。实测：

```
scope = /s，run 已开
  cwd=/s/pkg   git commit -m y                  →  拦住（原有行为）
  cwd=/other   git -C /s/pkg commit -m y        →  【修之前放行】
  cwd=/other   git -C /elsewhere commit -m y    →  放行（目标不是这个 scope 的）
```

第四种 `scope_match` 模式 `path_prefix_or_payload` 把这个洞关掉：cwd 在 scope 内**或**
调用的 payload 里指名了 scope 内的路径，都算这个 run 拥有这个动作。

**加宽是逐 ability 的声明，绝不做默认** —— 这条规则是 `scope_covers` 自己的文档定的：

> A false positive here is worse than a miss, and not symmetrically: being told to satisfy a
> gate that belongs to somebody else's work leaves **forging that gate** as the only way forward.

代价是诚实的、可测的：一个只是**被提到**的 scope 也会命中（commit message 里引了别的 repo 的
路径）。两件事把它收窄：

- **动作模式匹配是第一道过滤** —— `echo /s/pkg` 不是受守动作，即使提到也放行（有测试钉住，因为
  这正是误拦面的边界）
- **不声明的 flow 逐字节不变**，默认仍是最窄的 `exact`

**怎么选**取决于这条流程的 scope 是不是一个**位置**：scope 是一个目录（仓库、工作区）时，
一个动作可以「站在别处、指名这里」，所以要 `path_prefix_or_payload`；scope 是一个 CR、一次迭代、
一个文档 slug 这类**不是位置**的东西时，`in_payload` 就够了，路径前缀对它没有意义。
不声明的 flow 不受任何影响。

**两种 payload 模式共用一份搜索实现。** 两份实现会漂，而漂移是隐形的：两个模式由不同 ability
使用，分歧表现为其中一个悄悄匹配得更少。有测试同时对两个模式断言同一批边界。

payload 那一侧**刻意不解析符号链接**（而两个目录会）：payload 是散文，猜哪些子串是路径本身就是
一次猜测。所以符号链接写法在那一侧**漏**而不是**拦** —— 按上面那条不对称，漏是安全方向。

### 一个要求可以由目录声明，而那份声明里没有任何路径

机器本地那份记录（`<state_dir>/required-flows`）的条目带**绝对路径**。对「一次评审」「一个迭代」
这种不是位置的 scope 它是唯一可行的形状；对其余全部则是错的形状 —— **它不可移植**：

```
换台机器          同一个 checkout 拼法不同   → 条目一条都不匹配，静默无保护
别人 clone        他没有这个文件             → 什么都不发生
重命名父目录       条目永远匹配不到任何东西     → 无声失效
```

所以**目录也能声明**：在它下面放一个 `.harness-required`，列出在它之下必须走的 ability。
那份文件里**一个路径都没有** —— scope 就是持有它的那个目录：

```
<repo>/.harness-required
  # 这个仓库的交付必须走 delivery
  delivery
```

于是同一份字节在任何机器上意思相同，签进仓库就让要求**跟着代码走**，并且改动它会出现在 code
review 里。有测试直接钉住这条性质：同一份 marker 放在两个不同位置，发现出的 scope 分别等于各自
所在目录，而文件内容里连 `/` 都没有。

**一处我要更正自己上一轮的判断。** 当初否掉「repo 内声明」的理由是「仓库可写，agent 能改」。这条
理由站不住：机器本地那个文件**同样**是同一个 agent 可写的，而这个机制从不声称防颠覆。可写从来不
区分这两个位置。真正的区别恰好是反过来的 —— **committed 文件里少一行会出现在 diff 里，本机文件里
少一行没人会看见。**

**最近者胜，而空 marker 因此是一次刻意的本地豁免。** 父目录的要求是机器所有者的，仓库自己的
marker 是它作者的；两者都说话时，更具体的那个更知道自己在说什么。而「不在这里」必须说得出来，
否则退出的唯一办法是去改别人 committed 的文件。

#### 两条被明确划下的边界

**① 发现从调用方所在处开始，不从 payload 里的路径开始。** 从 payload 定位 marker 需要一条「哪些
子串是路径」的规则，而这个引擎在别处正因同一个理由拒绝这种猜测。后果说清楚而不是留给人撞见：
**一次从树外发出、指名树内目标的调用，找不到 marker。** 机器记录覆盖这一格 —— 它的条目直接指名
目录，配合上面那个加宽模式就能认领。这个缺口有答案，不是一个洞。

**② 补救随来源而变，给错比不给更糟。** `require --remove` 改的是机器记录。对一个由目录声明的要求
提这条命令，会把读者送去改一个并不包含它的文件 —— 命令报成功、要求依然生效，读起来像机制坏了。

```
来源 = 机器记录  →  and comes out with: harness require --remove <ability> --scope-key <key>
来源 = 目录 marker →  and comes out by editing that file — it is not in the machine's record,
                      so `harness require --remove` would not touch it.
```

`harness require`（读视图）**同时报两个来源并标出每条来自哪里**，否则「这里要求什么」有两个答案
而只有一个可见。驱动契约里那条判断规则也点明了两处声明位置 —— 那一节的清单只是机器记录，
**清单短不等于你工作的地方没有要求。**

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
run1  flow-a  repo=<repo>  step=C00  actor=alpha  → delegated to run2
run2  flow-b  repo=<repo>  step=C01  actor=beta   ← leased from run1  → delegated to run3
run3  flow-b  repo=<repo>  step=C01  actor=gamma  ← leased from run2

repo=<repo>: 3 runs, sc1 → dl1 → dl2  — guards adjudicate
```

再塞进一条无关的 run，判定就翻过去（而这正是强制此刻在这个 scope 上是关着的）：

```
repo=<repo>: 4 runs, no usable delegation (partial)  — ⚠️  guards will NOT adjudicate here
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

## 能力声明与并发

一条 flow 声明它依赖引擎的哪些机制（于是缺失在载入期就说得出口），以及多个 session 同时跑时**隔离轴是什么**。

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

## 演进与夹具

账本怎么带着数据往前走，以及为什么两个 demo 能力最终被留下来当**夹具**而不是被删掉。

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
（共 24 个测试函数）都跑在它们上面，替代方案是跑 102 步的真实流程。一个夹具需要的是小、稳、
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

## provider：把外部世界接进来

运行时事实来自外部世界，而外部世界会不在、会要凭证、会撒谎。这一节是**接一个外部工具之前要先回答的那些问题**。

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

### 通用性是被测试证明的，不是声称的

两个 ability 用**完全不相交的事实**：

| ability | provider | 事实 |
|---|---|---|
| `delivery` | `git_tree` | `changed_files` / `vcs_branch` / `dirty` / `change_count` |
| `authoring` | `run_progress` | `closed_steps` / `evidence_kinds` / `gate_count` / `violation_count` |

`authoring` 完全不碰文件系统。**如果引擎有任何一处暗设「事实是文件形状的」，它就跑不起来。**
配套三条测试：两个 ability 不许共用任何一个事实名；求值层（`conditions.py` / `operators.py`）
不许出现任何事实名；内置算子不许含领域词干。

