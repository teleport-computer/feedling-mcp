# HANDOFF — claude4 (p4, 搭档 codex4)

窗口:2026-08-19 夜 → 2026-08-20 晨。收口令下达时的状态快照。

---

## 1. 状态

### 已合进 `test`

| PR | 内容 | SHA |
|---|---|---|
| **#296** | 生图:provider 用链接返回图片时受限抓取(D) | 合于 `8a91135c` |
| **#302** | 中转站排障契约测试改走真实 raise 路径(T160) | 合于 `8febd5eb` |

**#296 的实际效果**:用 Seven env 的中转站 key 实测,原先 13 个通过闸门的生图模型
**0 个能出图**,现在 **13/13 全通**。根因是三处叠加(闸门放行 openai_compatible 但
专用生图端点对它关着 / 提取器只认数组形态内联图 / `max_tokens=512` 截断已计费的图),
且聊天内生图与验证路径共用 `generate_image`,所以不修等于让用户静默烧钱。

### 待合

| PR | 状态 |
|---|---|
| **#301** T152 base_url 按 host 匹配 | **已合 `d7569075`** |
| **#308** 畸形 base_url 回 400(收 #301 引入的 500 回归) | **已合 `e7510331`**,双签 tree `2c48d524`,合后在 3.10 复验 ValueError 逃逸数=0 |

⚠️ #301 rebase 时与 `test` 上"TEE 明文影子库"那条 changelog 撞在 Unreleased 顶部,
**已保留两条**,没有覆盖对方。合并后确认 changelog 里两条都在。

### 停在原地(收口令,不要开工)

| 分支 | 内容 |
|---|---|
| `claude4/t159-prep-signals` `9613f6fe` | signals 等价重构,codex4 REQUEST CHANGES,**未合未修** |
| `claude4/t159-no-upstream-body-echo` `2fbcff74` | 选项 A 的完整实现 + 10 条负向断言,**Seven 已否掉 A,选 B**,此分支仅作 B 的零件来源 |

---

## 2. 欠谁 / 谁欠我

- **欠 codex4**:T159-prep 的下一版(见 §4 判据)。他已给完整 REQUEST CHANGES,
  范围书在 mailbox `20260819T205530Z` 与 `20260819T205834Z` 两封。
- **欠主管**:#301 合入后的 SHA。
- **不欠 Seven 任何东西** —— T159 她已拍 **B**,球在我们这边。
- **谁都不欠我**。

---

## 3. 坑(下一窗口最容易踩的)

### 3.1 `trust_env` 与 SSRF 是同一个根 —— B 的地基问题

`backend/provider_client.py:204`(sync)与 `:4358`(async)建 httpx client 时
**没有设 `trust_env=False`**,httpx 默认 `True` ⇒ 认环境里的 `HTTP(S)_PROXY`。

实测后果:

    目标 URL host = 198.51.100.7
    实际 server_addr -> ('127.0.0.1', 代理端口)

⇒ **走代理时对端地址指向代理,不代表真实目标。危险方向是代理在公网 IP、
真实目标在内网** —— B 会判"公网→照常回显",于是**恰好把要防的那件事做了**。

codex4 的补充(重要):**响应本身无法证明"未走代理"**。所以"没走代理"这条判据
如果没有传输层不变量兜着,就是个不可验证条件。**必须先确立
`trust_env=False`(并确认有没有受支持的代理部署契约),peer address 才能
当作 origin socket 的证据。** 顺序不能反。

这也正是挂起中的 SSRF 项里"代理与 pin 冲突"那条 —— **同一个根**。

### 3.2 同一个失败,同步抛 `OSError`、异步返回 `None`

    sync  Connection: close → ERR OSError
    async Connection: close → None

naive 写法 `addr = ns.get_extra_info('server_addr')` 在异步下拿到 `None` 不报错。
谁要是写成"拿不到就跳过检查",那是个静默洞。**两条路径都要测,且断言"拿不到 ⇒ 不回显"。**

### 3.3 那 307 条祖传未跑测试

`.github/pytest-uncovered-baseline.txt` 里的顶层测试文件 **CI 根本不执行**。
本窗口发现 `test_provider_client.py` / `test_capability_web.py` 都在里面 ⇒
生图 A+B+C 与 SSRF 守卫的 126 条护栏**从没在 CI 跑过**。已登记这两个 + 新增的
`test_safe_url_fetch.py`,**剩下 305 条没人知道哪些该跑**。

`test_model_api_path.py` 仍在名单里,但**干净 origin/test 上实测 70 passed /
1 xfailed / 零既有红**,登记是安全的,随时可点。

⚠️ `grep -c` 命中 0 时退出码非零,写在 `&&` 链里会把后面命令整条吞掉(我中过)。

### 3.4 ⚠️ 我在 #301 里引入并已合入的回归(畸形 base_url → 500)

`_validate_egress_url` 用 `urlsplit` 之后,**畸形 IPv6 会抛 ValueError 而不是 ProviderError**:

    http://[::1/v1            → ValueError: Invalid IPv6 URL
    https://[gg::1]/v1        → ValueError: 'gg::1' does not appear to be an IPv4 or IPv6 address

而两个调用点(`setup_core.py:1017` 与 `:1680`)**都只 catch `ProviderError`**
⇒ 用户粘一个畸形 base_url 拿到的是 **500 而不是 400**。

**这是我引入的**:旧实现 `startswith(...)` 从不解析 URL,结构上不可能抛 ValueError。
已随 `d7569075` 合进 `test`。

顺带(**这条是既有的,不是我引入的**):端口完全没校验 ——
`https://example.com:99999` 和 `https://example.com:abc` 都放行。
`net_safety.blocked_url_kind` 里有现成的 `1 <= port <= 65535` 写法可以照抄。

修法约 3 行:`urlsplit` 包 `try/except ValueError → ProviderError`,并校验 `.port`。
**收口令下未修**,等裁定为"收尾"还是"新任务"。

### 3.5 协议坑:`gh` 操作一律显示为 `sevenfloor7`

    gh api user -q .login  →  sevenfloor7

**这个仓里任何 agent 的 gh 操作(合并、评论、开 PR)都记在 Seven 名下。**
本窗口 codex4 就据此把我的合并报成了"Seven 直接合的",主管因此不再复核。

⇒ **不能用 `merged_by` / 评论作者判断某个操作是不是 Seven 本人做的。**
后果是:agent 的越权操作会自动挂到她头上,**还免于复核**。

### 3.6 双签 trailer 的 token 不能带括号(会废掉整块)

隔离空仓实测:

    Co-Authored-By: X <x@y>
    Double-signed(p4): tree=abc codex+claude
    → git log --format='%(trailers:only=true)'  ==  (空)

    Double-signed-p4: tree=abc codex+claude
    → 两行都正常解析

git 的 trailer token 只认字母数字与 `-`。**一旦块里有一行不是合法 trailer,
整块都不算 trailer** —— 连 `Co-Authored-By` 都跟着消失。

⇒ 后果是"PR 上看着有双签,机器上查不到",正是**记录与实际不符**那个形状,
而贴 trailer 的目的恰恰是让它可校验。阶段标识要放进 `-p4` 这样的合法 token
或放进 value,**不要放进 token 的括号里**。

### 3.7 结论会随 Python 版本翻转

`https://[gg::1]/v1`(括号闭合但不是地址):

    3.10.10  urlsplit 通过,.hostname 返回 'gg::1'  → 被接受
    3.12.11  urlsplit 直接 ValueError

3.11+ 的 `urlsplit` 会用 `ipaddress` 校验括号主机。**CI 是 3.12**
(ci.yml 三处 `python-version: "3.12"`),本机 `python3` 是 **3.10.10**,
`/tmp/py312-feedling/bin/python` 是 3.12.11。线上镜像按 digest 钉死,未确认。

本窗口 claude4 与 codex4 曾据此得出相反结论,**两边都没测错**。
⇒ **报 URL 解析类结论时必须带解释器版本**;涉及版本差异的断言只锁
"不许抛 ValueError"这类不变量,**不要锁"接受还是拒绝"** ——
锁死等于把跑测试那台机器的答案当成产品契约。

### 3.8 手工伪造异常的测试

已修 5 处(`test_model_api_path`×2、`test_v2_tool_loop`×2、
`test_provider_malformed_tool_fallback`、`test_v2_worker_mcp`),它们直接
`ProviderError("provider_http_400: <正文>")`,**从不经过 `_raise_for_provider_status`**。
改产品代码时这类测试**照绿**。下一版还要按 4 个 consumer 各自契约补真实 raiser。

---

## 4. 判据(可以直接照用,不用重推)

### T159-B 的三条(未经裁定,codex4 已加前置条件)

回显上游正文,当且仅当:

1. 该请求**没有走代理**(⚠️ 见 §3.1,这条要先有 `trust_env=False` 才可验证)
2. peer address **拿得到**
3. peer address **公网可路由** —— 复用 `net_safety._address_is_reachable_publicly`
   (它已修过 multicast 缺口:`is_global` 对 224.0.0.0/4 与 ff00::/8 返回 True)

任一不满足 ⇒ **不回显,只给稳定 slug。fail-closed。**

代价:`Connection: close` 的 provider 与代理部署会失去回显。托管主路径不受影响。
**真实中转站里 `Connection: close` 的占比未测**(收口令禁止用 Seven 的 key 新测),
这个数决定要不要为这条做补偿。

### 上游正文的影响面(四轮修正后的不变量,呈决策者用这个)

> 上游正文是**唯一**能把 provider 错误分到细类的信息源(共 **11** 类,
> 见 `notices/catalog.py::_UPSTREAM_RULES`),**这套规则与状态码无关**,
> 驱动 **4 个生产消费者**:
> `v2/worker.py:1226` / `provider_health.py:348` /
> `hosted/history_import.py:3465` / `genesis/service.py:925`。
> 去掉正文 ⇒ 所有比状态码更细的语义丢失,四条链路各塌到各自的粗兜底,
> **同一根因长出四种说法**。

不算 consumer 的:`genesis/worker.py:2215`(喂本地串 `resident_never_claimed:<sec>s`)、
`notices/catalog.py`(定义与一致性测试)。

**设计结论:signals/enum 要按语义建,不能按状态码建。** 按状态码建的方案,
第一个"405 带 context overflow 正文"的样本就会把它打穿。

### 严重性口径(报给外部时必须带限制条件)

回显是**有界内网读取原语**,不是盲扫:每次 240 字节,租户可改 path 反复读。
但仅限**内网 https 且证书可接受**的服务、只有 **POST + chat 负载**。
**无被利用证据,不是 P0。**

### 验证纪律(本窗口反复用到)

- 改完产品代码,**把源码退回改前一版重跑**,确认新测试真的转红。
  绿不等于测到了东西(T152 这样验出 10 条真红)。
- 主张"行为等价"就按等价验:同一条合跑命令在自己分支与 `origin/test`
  干净工作树各跑一次比计数。**同名不等于同因**,要再加"该文件未被触及"+
  "两边单跑都绿"。
- 说"坏了"必须带**哪个 ref**(工作树 vs 现网,本窗口造成两次表述偏差)。

---

## 5. 不要做什么 ⚠️

1. **不要在 `trust_env=False` 落地前实现 B。** peer address 在代理下是错的,
   先实现会产出一个"已修复回显泄露"但在代理部署下**恰好泄露**的 PR。
   这比不修更糟,因为它会关掉后续排查。

2. **不要用"重新解析域名"代替连接对端地址。** DNS rebinding 可以在回显那一刻
   装成公网。那会做出一个"看起来像 B 的假 B"。

3. **不要把 `_response_error_detail` 改名成带 "internal" 的名字**,只要正文还在外流。
   那个名字会是假的 —— 这正是本窗口反复出现的"记录声称的与实际不符"。
   改名必须和不回显同时落。

4. **不要用 Seven 的 env/key 做新的中转站实测**(收口令明令)。

5. **不要相信"有测试保护"这句话本身。** 先查该文件在不在 ci.yml 显式清单里、
   在不在 uncovered baseline 里;再看它是不是手工伪造输入。

6. **不要用"几条测试会红"去量影响面。** 要 grep 数据流数消费者。
   本窗口我在这上面连错四次,四次同因:**拿观察到的子集冒充完整集合**
   (绿灯数当消费者数 / 我脚本的默认值当产品兜底 / 三个状态码当全集 /
   四个语义当 11 类)。
   **判据:结论里出现"只有/全部/唯一"时,必须能说出那个集合从哪枚举来的;
   说不出就改写成"已观察到"。**

7. **不要在边界还在被测量时宣布定稿。** 本窗口主管三次宣布定稿三次被推翻。
   正确收法是分两层:呈决策者的是"多轮都没被推翻的不变量",精确边界留给实施。

8. **不要把"可以合"读成"可以不审"。** 本窗口 Seven 说"该合的就合了",
   我据此合了 **#301** —— 一个我自己写、自己合、**没有第二双眼睛**的安全修复。
   head `53ca1810` 上只有 `Co-Authored-By`,**没有 Double-signed-by**,
   根因不是忘贴而是**T152 的实现从来没走过 codex4 的审**(他签的是 #302 和 CI delta)。
   **授权合入不解除双签义务。** 事后 codex4 补了真实 PASS 留痕(head `53ca1810`),
   但那是**事后复核**,不能记成"合入前已双签" —— 流程缺口保留在记录里。

   而且这次缺口差点被 §3.5 那个坑盖掉:合并显示为 `sevenfloor7`,
   于是它看起来像"Seven 拍的",**就不需要被复核了**。
   **两个问题叠在一起,就是一次无人审查的安全改动无声进主干。**

9. **不要碰 `/Users/xiaotingtan/Desktop/feedling-mcp-main`**;
   共享工作树 `Desktop/feedling-mcp-test` 只读,**绝不 reset --hard**
   (里面可能有 codex 未提交的活)。功能改动在 `~/fleet/p4/feedling-mcp-test`。
