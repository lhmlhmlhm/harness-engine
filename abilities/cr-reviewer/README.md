# cr-reviewer

评审**别人的**一个 CR：取到它、读懂它、成文、发布意见。9 步、1 个受守动作。

> 引擎侧的机制（谓词、gate、guard、scope、`requires`、退出码）见仓库根 `README.md`。
> 这份文档只讲**这条流程**。

## 这个 ability 解决什么

`when:` 的原话：「用户给出一个评审链接/编号，或说『review 一下这个』『有哪些评审等我看』
『帮我看看这个 CR』。只用于**评审别人的**代码；自己的改动要交付走 `shipcheck-asis`。」

这条分界是这条流程唯一的前提，也是它 scope 形状的来由：它谈论的对象是**一个评审**，
不是一个仓库。

## scope

```
scope_kind:  cr
scope_match: in_payload
```

**一个 CR 不是一个位置**，所以路径前缀对它没有意义 —— 用 `in_payload`：判断一个受守动作
是否落在这条 run 管的范围内，看的是**调用载荷里提到的 CR 编号**，而不是当前目录。

对比：`shipcheck-asis` 的 scope 是一个仓库目录，那是位置型的，所以它才需要
`path_prefix_or_payload`（既看 cwd 前缀、也看载荷指名的目标）。同一个引擎机制，
两种 scope 形状选不同的模式 —— 选择依据是「这条流程谈论的东西有没有路径」。

## 流程结构

| phase | 步骤 | 说明 |
|---|---|---|
| `intake` | `R01` `R02` | 取到 CR 并读懂它 |
| `analyze` | `A01` `A02` `A03` | 分析与成文 |
| `deliver` | `V01` `V02` `V03` `V04` | 发布意见（**不可逆的一步**在这里） |

`uses`（10 项）：`exclusive_groups`, `facts`, `gates`, `guards`, `hooks`, `obligations`,
`optional_steps`, `phase_goals`, `prose`, `results`。

## 判据与 gate

`harness validate cr-reviewer` 的实测输出：

```
✅ cr-reviewer  steps=9  gated=1  guards=1
```

（完整输出还有 `criteria:` / `goals:` / `prose:` 几行；跑一次就能看到当前值 ——
这里不抄，抄下来的数字就是下一个漂移点。）

**唯一的 gate 在 `V01`，类型 `affirm`。** 它在那里的理由很直接：`V01` 是**把意见发出去**
的那一步，发出去之后收不回来。前面 8 步都是读和写草稿，都可以重来。

`V01` 没有打 `strict_witness`，所以没有 witness 时它会记一条 `breach` 并放行 ——
与 `shipcheck-asis` 那两个关任务/拆工作树的 gate 是不同的分级。

## providers 与 facts

一个 provider：

| provider | requires |
|---|---|
| `cr_review_state` | `cmd: curl`, `net: code.amazon.com:443` |

这是这棵树上**唯一一个声明 `net:` 的 provider**，也是唯一一个让端到端测试依赖网络的地方。
代价在下面〈设计记录〉里如实记着。

**它刻意不做的事：从不碰凭证库。** 读一个评审需要已鉴权的会话，而一个伸手去读用户凭证文件的
provider 就不再是「取一个事实」而是「获得一种能力」。所以主机要凭证这件事被**当作事实上报**
（「这台主机要求我没有在用的凭证」），而不是被获取。

由此产生一个必须诚实写下的限制：**已存在的评审和不存在的评审，在未鉴权探测下返回同一个重定向。**
所以「这个评审是否存在」在这里**不可派生**，没有任何事实声称它 —— `providers.py` 里
`cr_exists` 出现 0 次，那不是漏了，是它被删掉之后没有再回来。`cr_readable` 因此恒为 false。

## guards

| 动作 | 保护的步骤 | 匹配什么 |
|---|---|---|
| `publish_comment` | `V01` | `CRAddComment` 载荷里 `"publish": true` |

**只拦 `publish: true`。** 写草稿评论不受影响 —— 受守的是不可逆的那一次，不是整个工具。
这条 guard 由 `CRAddComment` 这个 preToolUse matcher 触发，所以它**不是** ask-only：
运行时钩子看得见它，能真正拦下来。

## 环境与前置

- **`providers.py` 必须被批准**（`harness trust cr-reviewer`）—— 只在 `abilities/` 位于
  引擎树之外时；理由见根 README 的〈上手〉。
- `curl` 必须在 PATH 上。
- `code.amazon.com:443` 可达。**不可达时不是崩溃**：`requires` 里的 `net:` 缺失会让
  `cr_review_state` 求值成 `Unavailable`，依赖它的条件变成「不可判定」而不是 false。
- 这条流程的端到端测试因此**依赖网络**。这是一个真实代价，见下。

## 设计记录

下面这一节原本写在仓库根 README 里。它讲的是**这条流程**接一个需要网络的外部系统时
划下的两条硬边界 —— 不是引擎机制，所以随这条 ability 走。

#### 依赖网络的 provider：两条硬边界

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

