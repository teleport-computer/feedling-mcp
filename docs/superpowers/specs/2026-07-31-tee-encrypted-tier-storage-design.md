# 加密档用户在 TEE 主库的存储形态（Task 2.4 设计）

> 母计划：`docs/superpowers/plans/2026-07-23-tee-promotion-decrypt-removal.md`
> 状态：设计已定，**实现未开始**。这是 Phase 4 cutover 的硬前置。

## 1. 要解决什么

v6 之后内容加密是每用户偏好。cutover 会把 TEE 库从「影子」扶正为唯一主库，
于是必须先回答：**opt-in 加密的用户，他们的数据在新主库里长什么样？**

## 2. 先说一个必须先处理的既有事实

TEE 影子库是当年「加密恒定开启」时代的产物，它的定位是**明文影子**：

- `backend/tee_replicator/transforms.py` 对每一行**无条件调 enclave 解密**，
  并显式丢弃全部加密学字段（`body_ct / nonce / K_user / K_enclave /
  enclave_pk_fpr / content_pk_fpr`），注释写得很清楚：「TEE 明文库读路径不再过
  enclave，留着既无用又危险」。
- `_decryptable()` 只在 `local_only` 或缺 `K_enclave` 时才跳过——**跟用户档位无关**。

两条推论，都需要在 cutover 前处理：

**(a) 若今天直接 cutover，加密档用户的数据会静默变明文。** 他们在设置页开着加密，
新主库里却是明文行。这是本计划反复强调的「最严重失败模式」在存储层的版本，而且
不会有任何报错。

**(b) 现存影子库里已经有全体用户的明文副本。** 这不是 cutover 才产生的——它现在
就在那里。任何用户一旦选择加密档，他此前内容的明文副本仍留在影子库中，并会随
cutover 变成主库的一部分。**这是一笔必须显式处理的存量，不能靠「以后不再解密」
自然消化。**

> 判据可自查：`ftee prod "select doc ? 'body_ct' as sealed, count(*) from
> chat_messages group by 1;"` —— 现状应当是 `sealed=false` 占 100%。

## 3. 决策

**按行原样搬运（carry verbatim）：复制过程不再解密。**

> **2026-07-31 实现时修正了判据**：初稿写的是「按**行形状**搬运」，动手才发现有洞
> ——现在 `PLAINTEXT_WRITES_ACCEPTED` 是 False、effective 恒 `"on"`，**所有行都是
> 信封**。按形状搬运会让影子库立刻整体变密文、明文排查通道当场失效。那是过渡期
> 回归，不是终态。
>
> 正确判据是**用户意图**（`content_encryption` 偏好）：显式选加密的用户才原样搬运，
> 其余维持解密。平台放开明文后意图与形状自然一致，本分流退化成「按行形状搬运」，
> 即本节描述的终态。已实现于 `worker._carries_verbatim()`。

| 行形状 | TEE 主库里的形态 | 读取方式 |
|---|---|---|
| 信封行（加密档） | **原样双收件人信封** | 经 enclave 解密 |
| 明文行（默认档） | 明文 | 直读 |
| R2 指针行 | 指针原样，正文在对象存储 | 按原有 R2 路径 |

即：**一行进来是什么形状，出去就是什么形状。** 复制层不再对内容做任何加解密。

### 为什么现在才能这么定

Task 2.3 之前，TEE 侧读路径假定「行一定是明文」，所以复制时必须解密。Task 2.3
把读侧改成**按行形状路由**（`core.envelope.read_envelope_body`：有 `body_ct`
走 enclave，有 `body` 直读）之后，主库里两种形状共存才成为可读的状态。

换句话说：Task 2.3 是本决策的前置，而不是并列项。

### 为什么不选另外两个方案

- **全量解密成明文**（现状延续）：直接违背加密档用户的承诺，不可接受。
- **全量加密**：明文档用户平白多一次 enclave 往返，且与「明文档读写不经 enclave、
  故 enclave 故障不连坐」这条已兑现的性质冲突。

## 4. 这个决策带来什么变化

### 4.1 schema：无需改动

TEE 内容表的 `doc` 是裸 `JSONB`，**没有任何 CHECK 约束**（已核对
`alembic_tee/versions/0001_tee_baseline.py` 及后续全部迁移）。信封行结构上直接
存得进去。这是个好消息：本决策不需要新的 `alembic_tee` revision。

> 对比：RDS 侧的 V2 轨迹表**有**表级 CHECK 强制信封形状，那是 `0072` 要放宽的
> 对象。TEE 侧没有对应约束，不要混淆这两件事。

### 4.2 `transforms.py`：从「解密器」退化成「透传器」

改动集中在一处，且是**减法**：

- 删掉解密调用与 `_strip_envelope()` 的加密学字段丢弃。
- `_decryptable()` 连同 `PendingDeviceMigration` / `PermanentDecryptFailure`
  两个异常一起退场——不解密就没有解不开的问题。

**副作用是好的**：`tee_replicate_poison_row_headofline_quarantine` 那整套毒行
隔离机制（790 条毒行、watermark 冻结、quarantine-and-advance）**在 carry-verbatim
路径下不再需要**。毒行之所以是毒行，正是因为复制时非解密不可。

⚠️ 但**不要顺手删掉隔离机制**：cutover 前的存量清理（§4.4）还要用它，且 pending
表里的隔离行是需要交代的历史。退役放到 Phase 5。

### 4.3 `verify.py`：对账口径要跟着改

现在 verify 是「RDS 密文解开 == TEE 明文」。carry-verbatim 之后，加密档的行应当
是**密文对密文逐字相等**，不需要解密即可对账——这反而更快更可靠。明文行维持现状。

对账要按行形状分流，不能一刀切；`decrypt_failures` 指标对加密档行将恒为 0。

### 4.4 存量明文副本：cutover 前必须处理

这是本设计里**唯一需要跑数据的部分**，也是最容易被忽略的一步。

对每个 opt-in 加密档用户，其影子库里的明文行必须在 cutover 前被替换成信封行。
两条路，建议前者：

1. **重放**：清空该用户在影子库的内容行 → 重置其复制水位线 → 让 replicator 用
   新的 carry-verbatim 逻辑从 RDS 重新拉一遍。简单、幂等、不需要写一次性加密器。
2. 就地重封：需要在复制层引入加密能力，与「复制层不做任何加解密」的决策冲突。**不采纳。**

顺序上必须是：**先改 `transforms.py` → 再重放 → 最后才 cutover**。反过来会重新
生成明文副本。

### 4.5 R2 与 frames

frames 在 TEE 侧是「R2 指针 + 无 inline 密文」的新形状（baseline §4 决策），
正文加解密由 R2 storage key 那条独立路径负责，**不受本决策影响**。Phase 1.2
（`frames-tee/` 密文重写）与本任务正交，两者都要做，互不阻塞。

## 5. 与 iOS 的接口

不变。iOS 已经是按行自识别（`ContentWire.readBody`：`body_ct` → `unseal`，
`body` → 直读），主库里两种形状共存对它是透明的。

## 6. 执行清单（供后续拆成 plan 任务）

- [ ] `transforms.py` 改 carry-verbatim；相应删/停用解密回调注入
- [ ] `verify.py` 按行形状分流对账（密文逐字比对 / 明文比对）
- [ ] 回归：四象限（加密档信封行、明文档明文行、R2 指针行、混格式用户）
- [ ] 盘点当前 opt-in 加密档用户（cutover 前再盘一次，名单会变）
- [ ] 对这些用户执行「清空 + 重置水位线 + 重放」
- [ ] 重放后核对：`doc ? 'body_ct'` 为真的行数 == RDS 侧该用户的信封行数
- [ ] Phase 4 的 verify 步骤加入「加密档行确实是密文」这一条

## 7. 未决

- **加密档用户的 TEE 侧全文检索会失效**（明文影子库当前支持 `doc::text ilike`
  排查）。这是加密档的固有代价，但排查手册需要写明：加密档用户查不到内容，只能
  按 id/时间定位。这一条要写进 `feedling-ops-recon` 技能与影子库排查配方。
- **重放期间该用户的读取**：清空到重放完成之间有窗口。cutover 本身在维护窗口内，
  若重放也放在窗口内则无影响；若提前重放，需要确认那段时间的读走 RDS（cutover 前
  RDS 仍是主库，所以实际无影响——但要显式确认，别想当然）。
