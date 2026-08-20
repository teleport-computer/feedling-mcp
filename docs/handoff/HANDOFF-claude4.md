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
| **#301** T152 base_url 按 host 匹配 | 已 rebase 解冲突,重跑 311 passed,**只差 `test_api.py (multi-tenant)` 这一项 CI**;绿即可合(Seven 已授权"该合的就合了") |

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

### 3.4 手工伪造异常的测试

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

8. **不要碰 `/Users/xiaotingtan/Desktop/feedling-mcp-main`**;
   共享工作树 `Desktop/feedling-mcp-test` 只读,**绝不 reset --hard**
   (里面可能有 codex 未提交的活)。功能改动在 `~/fleet/p4/feedling-mcp-test`。
