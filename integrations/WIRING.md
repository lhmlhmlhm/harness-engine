# 把 harness-engine 接到一个 agent 上

三件事：一份**生成的**驱动契约（上下文）、一个 hook（唯一的强制点）、一条两侧必须一致的环境变量。

这份文档只讲**接线**，而且刻意不复述任何引擎事实——凡是引擎自己知道的，都由命令产出。
它此前复述过，然后如实漂了：写着一个已经搬走的状态库位置、一张少一档的退出码表、一条只在
一台机器上成立的路径。**所以下面每处「查一下」都是一条命令，不是一段抄下来的话。**

而这条规则此前只是**一句声明**，于是又被违反了四次——一个契约行数、一个规则条数、一个 spec
体积、两个用例条数，写下时全都是对的。现在它有两样东西托着：下面这张表是**生成的**（改了不
重生成就会有测试变红），而这份文档提到的每个命令、flag、路径、环境变量与常量都有一条测试去
核对它真的存在。

<!-- BEGIN GENERATED — python3 integrations/render-wiring.py --write -->

| 本安装实测 | | 谁产出它 |
|---|---|---|
| 驱动契约（`--portable`） | 216 行 | `harness brief --portable` |
| 其中不可派生的判断规则 | 7 条 | `brief.JUDGMENT` |
| 只在用到时才渲染的小节 | 10 个 | `brief._CONDITIONAL` |
| 最大的一条 flow | `sample-change`，13 步 | `harness abilities` |
| 它的 spec 与散文 | 14 KB + 153 行 | 磁盘 |
| 一步的 directive（中位数） | 5 行 | 同上 |
| 适配器用例 translation | 5 条 | `harness adapter-contract` |
| 适配器用例 resilience | 5 条 | 同上 |
| 适配器用例 end_to_end | 7 条 | 同上 |
| 状态库默认位置 | `$XDG_STATE_HOME/harness-engine`（未设时 `~/.local/state/harness-engine`） | `harness init` 会打印实际路径 |

<!-- END GENERATED -->

判断留在散文里，因为它不可派生：为什么 hook 是唯一的强制点、为什么隔离轴是 scope 而不是 agent、
为什么 fail-open 是铁律。那些东西不该进代码，也不会因为一次重构而变旧。

## 1. 驱动契约 —— 生成，不要手写

```sh
harness init                      # 顺带把本机契约写到 store 旁边，并打印路径
harness brief --write             # 或者单独刷新它
#   ✅ wrote ~/.local/state/harness-engine/brief.md
```

它按**这套安装**产出：真实存在的子命令、真实的退出码、以及**已装 flow 实际用到的机制**——
没有 variants 的安装不会被讲 variants。它的规模与其中「不可派生」的比例见上面那张生成表。

**有两份副本，指错会给 agent 一条跑不通的路径：**

| | 谁读 | 路径 |
|---|---|---|
| `$XDG_STATE_HOME/harness-engine/brief.md` | **agent**（配到 `resources`） | 本机真实路径，可直接跑 |
| `integrations/DRIVING.md` | 人（在仓库里看） | `<path-to-engine>` **占位符** |

签入那份用 `--portable` 生成，因为一台机器的绝对路径不该进版本控制；它开头会**自述自己是参考
副本**并给出 `harness brief --write`。`--write` 与 `--portable` 同时给会被拒——两者面对的读者
不同，静默让一个胜出恰好产出那份不可用的文件。

**这条曾经真的错过**：一个 agent 配置指着签入的那份，于是 agent 被告知 alias 一个占位符。

接进 agent 配置时给**文件**，因为 `resources` 要的是文件而不是命令：

```jsonc
"resources": ["file://$HOME/.local/state/harness-engine/brief.md"]
```

**新鲜度靠 `init`**，不靠记性：`init` 每次都重写这份契约，而它是每条接线路径本来就会跑、且可
反复跑的命令。一份没人重新生成的生成物，就是一份多几个步骤的手写文件——而一份过期的契约会
**静默地给 agent 错的指令**，那正是「改成生成」要消掉的失效，不是把它搬个地方。

⚠️ **不要**把 `flow.yaml` 或 `prose/` 放进 `resources`。一条成熟流程的 spec 与散文合起来是
几十 KB 量级（上表有本安装的实测值）；预载它们会把渐进披露整套设计废掉——那套设计的意义就是
agent **不**预载，而是每步问 `harness next`，拿到 directive（上表的中位数行数）+ 指针，需要时
才 `harness show`。

## 2. hook —— 唯一的强制点

```jsonc
// ~/.kiro/agents/<your-agent>.json
{
  "hooks": {
    "preToolUse": [
      { "matcher": "shell",
        "command": "python3 \"$HOME/…/harness-engine/integrations/kiro-pretooluse.py\"",
        "timeout_ms": 8000 }
    ]
  }
}
```

**`matcher` 该覆盖哪些工具，不要猜也不要手算** —— `harness brief` 最后一节直接列出来（并集自
已装 flow 声明的 `guards.*.matches[].tool`）：

```sh
harness brief | sed -n '/Tools the runtime hook must cover/,$p'
```

**没被 `matcher` 覆盖的工具，它的规则永远不会被问到。** 这是个安静的失效：ability 里声明得
好好的，而 hook 从没被调用。

### 换一个 agent：不要重新发明契约

### 一个配置里不该出现 checkout 的路径

上面那个 `command` 里的路径能用，而它**每多一个 matcher、每多一个 agent 就多一份**。实测过一次：
两个 agent 配置各写了 4 个 matcher 加 1 个 `resources` 条目，再加 shim 一处 —— **同一个目录名
出现了 11 次**。每一处都是对的，而每一处只在那一台机器的那一个目录下是对的。

代价不是「搬家麻烦」。**漏改一处的失效是静默的**：一个 `command` 解析不到的 hook 会什么都不跑、
什么都不拦，而从外面看，那和「本来就没什么要拦」一模一样。

所以让配置**一个路径都不写**：

```jsonc
{ "matcher": "shell", "command": "harness-hook", "timeout_ms": 8000 }
```

`harness-hook` 是 PATH 上的一个几行的包装，它问另一个包装「引擎在哪」，后者读**唯一一份记录**
（可被 `HARNESS_ENGINE_HOME` 覆盖，于是云桌面可以在 plist 或 profile 里声明而不改任何文件）。

`resources` 是个例外 —— `file://` URI 没法向谁提问，所以它指向状态目录里一个**稳定路径**，
而那个路径是一个指进 checkout 的链接：

```jsonc
"resources": ["file://$HOME/.local/state/harness-engine/CAPABILITIES.md"]
```

用链接而不是拷贝：**拷贝会静默过期**，而那正是 agent 读到上个月的能力地图的方式。

于是搬家变成一条命令重指全部，而它**验证而不是假设** —— 特别是验证那个包装真的在 PATH 上，
因为「hook 够不到」是这里唯一一种从外面看不见的失效。

### 第三样东西：一条必须是字面路径的策略行

`required-flows` 里的 scope key 是被**前缀匹配**的，所以它只能是字面路径 —— 没法像 hook 那样
在调用时问出来。而工作树的根就住在某个 ability 里，于是它是这台机器上第三个必须知道 checkout
在哪的地方。

它同样不该手写，理由和上面一样：**一个没被命名的根就是没人守的根，而没人守和「没什么要守」
长得一模一样。** 所以那条命令也维护它，并且：

- **通过 `harness require --add` 写**，不自己编辑文件格式 —— 格式归引擎，值归接线。
- **谁需要这条线是派生的**：`requires:` 里列了 `worktree` 的每一条 flow 都得到一条，所以第二个
  借用方靠「存在」加入，而不是靠有人记得改接线。
- **只回收自己写过的那一条**：删除严格按 (ability, scope key) 成对进行，且只针对**上一次记下的
  那个值**（写在状态目录的 `worktree-root` 里）—— 归属是**记录**的，不是从路径长相猜的。猜法认不出
  一个用 `HARNESS_WORKTREE_ROOT` 指到任意位置的根，于是覆盖值搬走后会留下一条**假要求**，而假要求
  比缺一条更糟：它会拒绝真实的工作。
- **你自己的条目一行不动。** 不想要这条线，`HARNESS_WIRE_NO_POLICY=1` 让它整段跳过。

**你在 `required-flows` 里手写的注释会存活。** 曾经不会：写入是从「条目 + 固定表头」重新渲染
整个文件的，于是下一次 `--add` 就把注释删掉了 —— 而 `read()` 从不看注释，所以没有任何东西会报告
这次丢失。现在写入是**就地编辑**：注释、空行、以及你自己排版的那一行都原样留下，被删掉的条目只
带走它自己那一行（它上面那句解释归你，不归引擎猜）。表头也只在文件还不存在时写 —— 否则那次写入
就在推翻它自己要保护的那个编辑。接线命令仍然会在注释行发生变化时明说，作为回归探针。


**这个间接层是可选的。** 只有一个 agent、且 checkout 不会动，就直接写路径 —— 上面那种写法更少
文件。它值得做的时候是：配置多于一个，或者这棵树会去别的机器。

**包装里的退出码不是自由选择。** 解析失败时它必须**放行**（exit 0）并大声说，因为
`kiro-pretooluse.py` 的铁律就是 fail-open：只有引擎明确的 exit 4 才变成拦截。一个在解析失败时
`exit 1` 的包装，是在用一个**未定义**的码拼写同一个「放行」，然后把「1 是什么意思」留给运行时决定。

`kiro-pretooluse.py` 是 kiro-cli 方言的翻译层（stdin JSON + exit 2）。别的运行时方言不同，
适配器**天生一个方言一份**、语言各异，生成不了。但**它必须满足的用例可以发布**：

```sh
harness adapter-contract          # JSON：I/O 契约 + translation + resilience + end_to_end
```

- `translation` —— 引擎退出码 → 你的 allow/block。**只有 BLOCKED 变成拦，其余全放行。**
- `resilience` —— 输入不是 JSON、引擎不存在、调用超时、适配器自己崩：全部放行，其中
  「引擎不存在」还必须**在 stderr 说出来**（「查不到该查什么」和「没什么要查」不能长得一样）。
- `end_to_end` —— 派生自已装 flow 的真实 action / gate 步骤 / 工具 / 正则。**载荷要你自己
  构造**：一个匹配任意正则的字符串没法从正则反推出来，而一个「碰巧不匹配」的假载荷会通过
  却什么都没证明——所以那一条如实标了「这归你」。

这些用例不是摆设：**本引擎自己的适配器测试就在迭代它们**。

## 3. `HARNESS_STATE_DIR` —— 两侧必须一致

默认位置是 `$XDG_STATE_HOME/harness-engine/harness.db`（未设 XDG 时
`~/.local/state/harness-engine/harness.db`）——**不在引擎目录里**，因为装进 site-packages 后
那个目录常常不可写，且下次升级会被替换。

若 agent 侧设了这个变量，**hook 侧必须设同一个值**，否则 hook 查的是另一个库、永远找不到
open run、静默全放行。适配器会显式检查并报警而不是静默放行：

```
⚠️  HARNESS_STATE_DIR=<dir> has no harness.db — guard NOT enforced.
```

最省事的做法是**两边都不设**，让它用默认值。

三个环境变量的完整清单由 `harness brief` 的 Environment 一节给出。

## 证人：四个变量，其中两个描述你的转录长什么样

gate 的「人确认过」由 `HARNESS_WITNESS=transcript` 强制，而它要数出人类回合，就得知道你的转录
把「这一条是谁说的」写在哪：

```sh
HARNESS_WITNESS=transcript
HARNESS_TRANSCRIPT=<这一次会话的转录文件>
HARNESS_TRANSCRIPT_ROLE_PATH=<点号路径，如 kind 或 data.role>
HARNESS_TRANSCRIPT_HUMAN=<逗号分隔，如 Prompt>
```

**默认值（字段名 `role`/`author`/`from`，人类值 `user`/`human`）对一个真实 runtime 是错的**，
实测过：它每行都是 `{kind, data, version}`，角色在 `kind`，人类回合拼作 `Prompt`。用默认去读会数到
0 个人类回合，于是每道门都被拒——一边拒一边说「这份转录里根本没有人类回合」，那句话对解析为真、
对会话为假。所以两种失败被分开报，因为修法不同：**找不到那个字段**会说出用了哪条路径，
**找到了但没有值意味着人**会报出实际出现的值（有上限：一条写错的路径可能指向内容）。

引擎不学任何 runtime 的拼法——同 `guards.<action>.matches`：知识在外面声明，引擎只施加。

**这段声明不能放在引擎里**（转录路径是你的磁盘布局），**也不能只放在 agent 手上**（一个被约束者
可以拒绝的证人不算证人）。`HARNESS_TRANSCRIPT` 通常还需要一个只在会话开始后才存在的 id，所以静态
配置也表达不了。可行的位置是**一个在 PATH 上的包装脚本**：它在调用时展开 id、声明这四个变量、再
`exec` 引擎。只在转录文件真的存在时才声明——否则引擎回落到自己的默认，而**那个降级会被记成一条
violation**，于是缺失出现在 `harness audit` 里而不是无声通过。

## 两个面，而不是一个

| 面 | 谁发起 | 形态 | 性质 |
|---|---|---|---|
| **驱动面** | agent 主动问 | `integrations/mcp-server.py`（tool calls），或直接调 CLI | 依赖 agent 配合 |
| **拦截面** | runtime 代问 | `integrations/kiro-pretooluse.py` → `harness guard-tool` | **agent 不配合也照样发生** |

把两者合成一个「全是 tool」的设计会把拦截面弄丢：模型不会去调一个目的是阻止自己的工具。所以
`guard-tool` 在 `adapter-contract` 的 `surface.not_a_tool` 里，连理由一起。

## 强制到什么程度：一个必须知道的边界

hook 强制的是「**已开 run 内**的门」。它**不能**强制「run 被开过」——`guard-tool` 在找不到
open run 时放行（fail-open 是铁律，它跑在每个会话的每次工具调用之前）。

所以「不开 run」是一条敞开的绕过路径。目前只能靠 prompt 里的指令去堵，而这个项目的论点恰恰
是**散文绑不住 agent**——所以 `harness brief` 把它作为一条判断规则明写出来，并说明它为什么
不是工具能替你兜住的事。结构性的答案是在声明过的位置内把 fail-open 翻成 fail-closed，但那有
真实代价（一个 bug 会拦住那些位置里的所有动作），所以**没有默认打开**。

诚实的现状（在没有声明的 scope 里）：**这套接线让「开了 run 之后绕过门」变得不可能，而没有让
「开 run」变成不可能不做。** 想在某个 scope 里把后半句也补上，用 `harness require`——它给守卫补上
一个可比对的 scope，于是没有 run 时那里会被拒绝而不是放行。空清单时行为与从前逐字节相同，而它
**防遗漏不防颠覆**：能删掉清单的人能关掉它，区别在于删除是一次动作、省略不是。

声明有**两个来源，能力相同，区别只在谁承担配置**：

| 来源 | 形状 | 代价 |
|---|---|---|
| 机器记录 | `harness require --add <ability> --scope-key <key>`，条目里可以写 `~`（比较时展开） | 不碰任何仓库；换机器要么重跑命令、要么把记录文件拷过去 |
| 目录声明 | 目录里放一个 `.harness-required`，列出 ability 名——**文件里没有路径**，scope 就是持有它的那个目录 | 跟着仓库走（可签进 git，改动进 code review）；代价是往仓库里加一个文件 |

**最近者胜**，所以一个空的 `.harness-required` 是一次刻意的本地豁免。两者都只对**有受守动作**的
flow 有意义：一条只产出文档的 flow 没有受守动作，声明它不会有任何效果。

一条被明确划下的边界：目录发现**从调用方所在处向上走**，不从 payload 里的路径开始——从 payload
定位一份声明需要一条「哪些子串是路径」的规则，而引擎在别处正因同一个理由拒绝这种猜测。后果是
一次从树外发出、指名树内目标的调用**找不到 marker**；机器记录覆盖这一格，因为它的条目直接指名
目录。

### 「没什么要守」与「我看不见要守什么」不许长得一样

`guard-tool` 会遍历**所有** open run 去判断这次调用是否被守。一个 flow **读不出来**的 run 曾经被
静默跳过——理由是对的（一个坏 spec 不该把无关工具链砸死），后果是错的：它让「这个 run 没有守你」
和「这个 run 的守卫查不到」输出完全相同，也就是零输出。

三种成因，此前**全部无声**：

| 成因 | 修法 |
|---|---|
| ability 不在这个进程的搜索路径上（`HARNESS_ABILITIES_PATH` 与 run 不一致） | 让 hook 的路径覆盖它要守的 run |
| spec 无效 | `harness validate <ability>` |
| **扩展代码未在本机批准**，于是装载拒绝 | `harness trust <ability>` |

第三种是随内容钉定一起到来的：**编辑一个已批准的 `providers.py` 会撤销批准，装载随即拒绝，那个
run 的 guard 就此无声关闭**——一个安全特性顺手发出了一张绕过券。

现在它出声，而且**仍然放行**：

```
⚠️  guard-tool: 1 open run(s) whose flow it CANNOT READ. Their
    guards are not enforced for this call — that is "the guard could not be
    looked up", not "there is no guard".
      r1 (priv): <为什么读不出来>
    Allowing anyway: this hook runs before every matching tool call, so it
    must never brick normal work. Fix whichever applies: …
```

**不按 scope 过滤**：判断这个 run 的 scope 是否覆盖本次调用，需要那份刚刚装载失败的 flow。报一个
后来发现无关的 run 是噪音；对一个其实相关的 run 保持沉默，正是这条要消除的失效。

## 多个 agent 同时用同一个引擎

隔离轴是 `(scope_kind, scope_key)`，**不是 agent**——守卫回答的是「这件事能不能对**这个东西**
做」，而那是被触碰对象的属性。所以各 agent 在各自的 worktree / scope 里并发是正常路径。

```sh
harness status            # 谁在跑，以及每个共享 scope 能不能被裁决
harness history --actors  # 跨 agent 的一个地方：各自多少 run、多少 violation
harness history --actor <name>
```

`HARNESS_ACTOR=<name>` 是**给人看的**，绝不参与隔离——按 actor 隔离会让同一个 repo 上的两个
worker 各自只强制自己的 gate 而谁都不知道对方存在，那是守卫唯一要回答的问题就此无解。

**共用一份状态库是刻意的**：各 agent 一份库会让它们的守卫互相看不见，跨 agent 的歧义也就
无从发现。

## 验证接线是否真的生效

```sh
cd <一个 ability 的 scope 内>
harness open <ability> --scope "$(pwd)" --run smoke

echo '{"tool_name":"shell","tool_input":{"command":"git commit -m x"}}' \
  | python3 <engine>/integrations/kiro-pretooluse.py ; echo "exit=$?"     # 期望 2

echo '{"tool_name":"shell","tool_input":{"command":"git status"}}' \
  | python3 <engine>/integrations/kiro-pretooluse.py ; echo "exit=$?"     # 期望 0
```

拿到 `exit=0` 而期望 2 时，按这个顺序查：

| 现象 | 原因 |
|---|---|
| 有 `⚠️ no harness.db` | 两侧 `HARNESS_STATE_DIR` 不一致 |
| 有 `⚠️ NOT enforced: N open runs` | 同一 scope 多个 open run 且**没有声明委派**，引擎拒绝猜（`harness status` 会直接说这个 scope 能不能被裁决；`harness leases` 看委派） |
| 无任何输出 | scope 不覆盖 cwd（检查 ability 的 `scope_match`——若动作作用于 cwd 之外的目录，需要 `path_prefix_or_payload`），或该工具无匹配规则 |
| hook 根本没被调用 | agent 的 `matcher` 没覆盖这个工具名（用上面那条 `harness brief` 取清单） |

## 为什么适配器只有一份，而不是每个 ability 一个

「哪个工具调用等于哪个动作」是 ability 的知识，所以它以**数据**形式住在
`guards.<action>.matches` 里。引擎编译并施加 pattern，**不知道那些命令是什么意思**（纯净性
测试会抓到它学会）。于是适配器只做两件事：翻译退出码，以及在任何不确定时放行。

替代做法是每个 ability 一个适配器、各自硬编码正则——那会让同一份映射存在 N 份、各自漂移，
而这正是本项目一直在反对的形态。
