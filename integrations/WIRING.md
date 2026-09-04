# 把 harness-engine 接到一个 agent 上

三件事：一份**生成的**驱动契约（上下文）、一个 hook（唯一的强制点）、一条两侧必须一致的环境变量。

这份文档只讲**接线**，而且刻意不复述任何引擎事实——凡是引擎自己知道的，都由命令产出。
它此前复述过，然后如实漂了：写着一个已经搬走的状态库位置、一张少一档的退出码表、一条只在
一台机器上成立的路径。**所以下面每处「查一下」都是一条命令，不是一段抄下来的话。**

## 1. 驱动契约 —— 生成，不要手写

```sh
harness init                      # 顺带把本机契约写到 store 旁边，并打印路径
harness brief --write             # 或者单独刷新它
#   ✅ wrote /Users/you/.local/state/harness-engine/brief.md
```

它按**这套安装**产出：真实存在的子命令、真实的退出码、以及**已装 flow 实际用到的机制**——
没有 variants 的安装不会被讲 variants。约 200 行，其中不可派生的只有 6 条判断规则。

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

⚠️ **不要**把 `flow.yaml` 或 `prose/` 放进 `resources`。转写后的交付流程是 35 KB spec + 1,338 行
散文；预载它们会把渐进披露整套设计废掉——那套设计的意义就是 agent **不**预载，而是每步问
`harness next`，拿到 directive（约 2 行）+ 指针，需要时才 `harness show`。

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

`kiro-pretooluse.py` 是 kiro-cli 方言的翻译层（stdin JSON + exit 2）。别的运行时方言不同，
适配器**天生一个方言一份**、语言各异，生成不了。但**它必须满足的用例可以发布**：

```sh
harness adapter-contract          # JSON：I/O 契约 + translation + resilience + end_to_end
```

- `translation` —— 引擎退出码 → 你的 allow/block，5 条。**只有 BLOCKED 变成拦，其余全放行。**
- `resilience` —— 输入不是 JSON、引擎不存在、调用超时、适配器自己崩：5 条，全部放行，其中
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
⚠️  HARNESS_STATE_DIR=/tmp/x has no harness.db — guard NOT enforced.
```

最省事的做法是**两边都不设**，让它用默认值。

三个环境变量的完整清单由 `harness brief` 的 Environment 一节给出。

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

诚实的现状：**这套接线让「开了 run 之后绕过门」变得不可能，而没有让「开 run」变成不可能不做。**

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
| 无任何输出 | scope 不覆盖 cwd（检查 ability 的 `scope_match`），或该工具无匹配规则 |
| hook 根本没被调用 | agent 的 `matcher` 没覆盖这个工具名（用上面那条 `harness brief` 取清单） |

## 为什么适配器只有一份，而不是每个 ability 一个

「哪个工具调用等于哪个动作」是 ability 的知识，所以它以**数据**形式住在
`guards.<action>.matches` 里。引擎编译并施加 pattern，**不知道那些命令是什么意思**（纯净性
测试会抓到它学会）。于是适配器只做两件事：翻译退出码，以及在任何不确定时放行。

替代做法是每个 ability 一个适配器、各自硬编码正则——那会让同一份映射存在 N 份、各自漂移，
而这正是本项目一直在反对的形态。
