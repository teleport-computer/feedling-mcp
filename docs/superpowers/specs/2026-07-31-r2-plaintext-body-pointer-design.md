# R2 明文正文 pointer 协议（Task 1.3 设计）

> 母计划：`docs/superpowers/plans/2026-07-23-tee-promotion-decrypt-removal.md`
>
> 状态：**2026-07-31 四项推荐方案已拍板**；未实现，禁止生产 apply。
>
> 范围：`chat_messages` / `chat_message_archive` 的 file/image R2 重体。
> `frames-tee/` 使用 enclave storage key，是 Task 1.2，不在本设计内。

## 1. 结论先行

首版 Task 1.3 不能靠「原地把 R2 密文覆盖成明文」修补。正确方案是：

1. 新增显式的 **plaintext R2 pointer** 形状；
2. 明文字节经 API 线形传输时用 `body_b64`，不冒充 `body_ct`；
3. 迁移复用现有 `upload_guard + per-key advisory lock + CAS + durable cleanup`；
4. 每次迁移写一个全新的私有 key，数据库切换成功后才异步删旧密文 key；
5. live 表用 JSONB CAS UPDATE，archive 因不可 UPDATE，事务内 delete+insert；
6. 只处理显式 `content_encryption="off"`；`"on"` 或查不到用户一律跳过；
7. 真跑后移到支持 `body_b64` 的 iOS 强制升级之后。

推荐允许明文档的 file/image 原始字节以明文存在 R2。否则图片/二进制文件仍依赖
enclave，产品承诺的「明文档读路径不经 enclave、enclave 故障不连坐」对重体不成立。
这是信任边界变化，实施前需要产品/安全拍板并同步公开文档。

## 2. 为什么现有 pointer 不够

现有持久化行：

```json
{
  "body_key": "chatfiles/usr_x/g3/msg_x/<upload-version>",
  "body_ct_len": 123456,
  "nonce": "...",
  "K_user": "...",
  "K_enclave": "..."
}
```

R2 对象存 raw ciphertext。`object_storage.get_chat_body()` 把 raw bytes 重新
base64 成 `body_ct`；`db.hydrate_chat_file_body()` 再把它装进 doc，所有消费者据此
走解密。

若只覆盖对象：

- 行里没有字段说明对象已变明文；
- hydrate 仍产出 `body_ct`，客户端/enclave 必然误解密；
- `body_ct_len` 也不再代表实际对象；
- 原地覆盖在 R2 成功、PG 未切换时不可逆地丢失原密文；
- 普通 `put_chat_body()` 生成新 key，根本不会覆盖 versioned `body_key`。

所以 marker、读取协议和原子 pointer 切换缺一不可。

## 3. 三种正文线形状

### 3.1 既有信封正文

```json
{"body_ct": "<base64 ciphertext>", "nonce": "...", "K_user": "..."}
```

读取优先级最高，经 enclave/设备解密。

### 3.2 既有 inline UTF-8 明文

```json
{"body": "plain text"}
```

本地 UTF-8 直读。适用于普通文本行。

### 3.3 新增 plaintext R2 pointer

持久化：

```json
{
  "body_key": "chatfiles/usr_x/g3/msg_x/<new-upload-version>",
  "body_object_format": "plaintext_v1",
  "body_size_bytes": 12345,
  "body_sha256": "<64 lowercase hex>"
}
```

R2 对象存**原始明文字节**。读取出口水合为：

```json
{
  "body_b64": "<base64 plaintext bytes>",
  "body_size_bytes": 12345
}
```

`body_b64` 是明文字节的线编码，不是加密。消费者 base64 decode 后直接得到原文件/
图片 bytes。file/image 一律走这个形状，不靠「能否 UTF-8 decode」猜类型；猜测会让
同一 MIME 因内容不同产生两种协议。

`body_key/body_object_format/body_sha256` 是服务端内部存储字段，跟现有 `body_key`
一样必须在 public response 前剥掉。SHA-256 用于服务端完整性核验，不需要暴露给
客户端，也不要扩大成新的公共内容指纹面。

读取优先级：

```text
body_ct  → 解密
body_b64 → base64 decode（不进 enclave）
body     → UTF-8 encode（不进 enclave）
其他     → envelope_shape_unrecognized
```

`body_ct` 仍优先，保留迁移中间态的 fail-safe 语义。一个规范行不得同时持久化上述
任意两个正文键。

### 3.4 legacy 兼容

- pointer 缺 `body_object_format`：按既有 `sealed_v1` 处理；
- 新写 encrypted pointer 可暂不补 marker，避免无收益地改全量旧行；
- 任何未知 `body_object_format`：硬失败，不猜、不回退成 sealed/plaintext；
- `body_ct_len` 只属于 legacy sealed pointer；
- `body_size_bytes` 只表示 plaintext raw bytes，omit 阈值以后统一按它判断。

## 4. 持久化行转换

迁移前：

```json
{
  "id": "msg_x",
  "content_type": "file",
  "body_key": "<old sealed key>",
  "body_ct_len": 16400,
  "nonce": "...",
  "K_user": "...",
  "K_enclave": "...",
  "enclave_pk_fpr": "..."
}
```

迁移后：

```json
{
  "id": "msg_x",
  "content_type": "file",
  "body_key": "<new plaintext key>",
  "body_object_format": "plaintext_v1",
  "body_size_bytes": 12288,
  "body_sha256": "..."
}
```

主信封的加密学字段必须一并删除：

```text
body_ct / body_ct_len / nonce / K_user / K_enclave /
enclave_pk_fpr / content_pk_fpr
```

消息自身的 `id/role/source/visibility/owner_user_id/content_type/file_name/file_mime`
及并发产生的 operational metadata 必须保留。

`thinking_*` / `caption_*` 是独立子正文，不能被主正文迁移顺手删除。它们按自身形状
继续路由；若仍是信封，明文档复制进 TEE 时单独解密。后续若要把它们在 RDS 也迁明文，
应走普通 inline 内容迁移，不与 R2 object CAS 混在一起。

## 5. 对象存储 helper

不要让迁移工具直接调用 boto client，也不要开放「任意 key 写入」。新增两个窄 helper：

```python
get_chat_body_bytes(key: str, user_id: str) -> bytes | None

put_chat_body_bytes(
    user_id: str,
    msg_id: str,
    raw: bytes,
    content_type: str,
    *,
    upload_version: str,
    storage_generation: int,
) -> str
```

约束：

- key 仍由 `chat_body_key()` 生成；
- owner prefix 校验仍集中在 object_storage；
- 现有 `get_chat_body()` 包装 raw getter 后 base64，行为不变；
- 现有 `put_chat_body()` 严格 base64 decode 后包装 raw putter，行为不变；
- 新 helper 只提供「raw bytes + 私有 versioned key」，不允许覆盖传入 key。

## 6. 崩溃一致性状态机

复用 `_offload_chat_body_after_commit()` 已在生产事故中验证过的原语。

```text
S0  旧 sealed key 被 PG pointer 引用；新 key 不存在
 │
 │ PG：为新 key 提交 upload_guard cleanup row
 ▼
S1  旧 key 仍权威；新 key 即使不存在也有 durable tombstone
 │
 │ 持有 new-key advisory lock，PUT 新 plaintext object
 ▼
S2  旧 key 仍权威；新对象存在；upload_guard 仍在
 │
 │ PG 单事务：CAS 切 pointer + 删除 new-key guard
 │            旧 key cleanup 由 trigger 同事务入队
 ▼
S3  新 plaintext key 权威；旧 sealed key 等待 cleanup
 │
 │ cleanup worker 在引用检查后幂等 DELETE
 ▼
S4  仅新 key 存在
```

### 每个崩溃点

| 崩溃位置 | 恢复结果 |
|---|---|
| guard 提交前 | 无新对象、旧 pointer 完整 |
| guard 后、PUT 前 | cleanup 幂等删一个可能不存在的 key |
| PUT 后、CAS 前 | cleanup 删除未引用的新孤儿；旧 pointer 完整 |
| CAS 事务中 | 回滚后 guard 仍在，新孤儿被清；旧 pointer 完整 |
| CAS commit 后 | pointer 与 guard 删除同事务完成；旧 key cleanup 已持久化 |
| 旧 key DELETE 后、cleanup ack 前 | DELETE 幂等，下一轮收口 |

整个 PUT/CAS 区间持有新 key 的 session advisory lock；cleanup worker 删除新 key 时
获取同一把锁。绝不在 R2 I/O 期间持有 per-user lifecycle row lock。

## 7. CAS：live 与 archive

### 7.1 `chat_messages`

定位键：`(user_id, msg_id, storage_generation, old_body_key)`。

更新必须基于**数据库当前 doc 做 JSONB 减法/合并**，不能把盘点时读出的整份 doc
写回，否则会覆盖上传期间落下的 reply/push/claim metadata：

```sql
UPDATE chat_messages
SET doc = (
  doc
  - 'body_ct_len' - 'nonce' - 'K_user' - 'K_enclave'
  - 'enclave_pk_fpr' - 'content_pk_fpr'
) || :new_pointer
WHERE user_id = :uid
  AND msg_id = :msg_id
  AND storage_generation = :generation
  AND doc->>'body_key' = :old_key
  AND COALESCE(doc->>'body_object_format', 'sealed_v1') = 'sealed_v1'
RETURNING 1;
```

trigger 看到 key 被替换，会把 old key 以 `pointer_replaced` 放进 cleanup。

### 7.2 `chat_message_archive`

archive 的 `BEFORE UPDATE` trigger 是 load-bearing 的不可变性保证，不能为了 backfill
永久放宽。迁移事务采用：

1. `SELECT ... FOR UPDATE` 并核 `source_seq/generation/old_key/sealed shape`；
2. `DELETE ... RETURNING`；
3. 用返回的同一组列 INSERT，只有 `doc` 换成新 pointer；
4. 删除 new-key upload guard；
5. commit。

DELETE trigger 会在同事务给 old key 入 cleanup。若 INSERT/FK/commit 失败，DELETE 与
cleanup 一起回滚，新 key guard 保留，孤儿对象随后被删。

必须逐字保留：

```text
user_id / source_seq / msg_id / ts / storage_generation /
clear_generation / cleared_at
```

account delete 与本事务依靠 row lock/FK 串行化；若 delete 赢，迁移 CAS 失败并保留
new-key guard，不得重新创造 archive 行。

## 8. 读取、复制与 verify

### 8.1 app/backend 读取

`db.hydrate_chat_file_body()` 按 marker 分流：

- legacy/sealed pointer：raw bytes → base64 → `body_ct`（现状）；
- `plaintext_v1`：raw bytes → 校验 size/hash → base64 → `body_b64`；
- 未知 marker、hash/size 不符、R2 缺对象：本条硬失败/缺正文，不降级猜格式。

成功水合后剥掉内部的 `body_key/body_object_format/body_sha256`；公共响应只留下
`body_b64/body_size_bytes` 与既有消息元数据。

history omit 逻辑：

- sealed pointer 用 `body_ct_len`；
- plaintext pointer 用 `body_size_bytes`；
- omit 输出不得携带 `body_key`；
- plaintext 可增加 `body_omitted_reason=image_body|large_body`，旧的
  `large_body_ct` 仅保留给 sealed pointer。

### 8.2 TEE replicator

- 加密档：沿用 carry-verbatim，sealed pointer 原样进入 TEE；
- 明文档 + sealed pointer（迁移未跑完）：水合 → enclave 解密 → 旧兼容路径；
- 明文档 + plaintext pointer：**pointer 原样保留**，不把大对象重新 inline 进 PG；
- 该行的 encrypted thinking/caption 仍按子信封单独解密；
- R2 marker/hash 等字段不得被 `_strip_envelope()` 丢弃。

### 8.3 verify

- 加密档 sealed pointer：沿用密文/行形状对账；
- 明文档 legacy sealed pointer：沿用解密后正文对账，直到迁完；
- plaintext pointer：两库 pointer 字段逐字比对；
- 另抽样 GET R2，核 `sha256/size`，但不把正文写进 report/log；
- 不能因为 RDS/TEE 指向同一 key 就把对象完整性自动判绿。

## 9. 新写路径

只做存量迁移不够：明文档以后发送的新 image/file 也必须走同一协议。

建议 public request 的 plaintext envelope 支持：

```json
{
  "body_b64": "<base64 plaintext bytes>",
  "owner_user_id": "usr_x",
  "visibility": "shared",
  "id": "msg_x"
}
```

规则：

- 只允许 `content_type in ("file", "image")`；
- 只有服务端 effective tier 为 `off` 时接受；
- base64 必须 strict decode，解码后执行现有 byte-size 上限；
- inline PG 首写存 `body_b64`，读得出来后再走同一 upload-guard/CAS offload；
- CAS identity 字段加入 `body_b64`；
- offload 成功后移除 `body_b64`，换 plaintext pointer；
- encrypted tier 收到 `body_b64` 必须 400，不得静默重新加密或明文降级；
- plaintext tier 仍接受旧 App 的 sealed envelope，保证强更窗口内兼容。

这会修改公共 API 契约。实现时必须同步：

- `tools/public_openapi_contracts.py` 与生成的 `docs-site/openapi/public.json`；
- API/架构/信任边界/自托管文档；
- `docs-site/content/docs/changelog.mdx` 的 Unreleased；
- iOS `ContentWire.readBody` 与 image/file 写路径。

## 10. 工具形态

默认永远 inventory-only；真跑需要两个显式开关，例如：

```text
--apply --allow-plaintext-r2-rewrite
```

能力：

- `--table live|archive|all`；
- `--user`；
- keyset pagination（live 按 `seq`，archive 按 `source_seq`），不一次拉全表；
- `--limit` 限的是**成功 CAS 数**，不是 tier 过滤前的 SQL 行数；
- 默认低 QPS，GET/decrypt/PUT 各自计数；
- 单行失败继续，输出仅含 user/msg/source_seq/status/error class，不含正文/密钥；
- 行状态本身就是 checkpoint：sealed → 待迁；`plaintext_v1` → 幂等跳过；
- 运行前断言客户端 rollout gate/服务端 feature gate 已开启；
- archive 与 live 都扫，不能只扫 `chat_messages`。

候选资格：

```text
explicit content_encryption == "off"
AND body_key owned by user
AND legacy/sealed pointer
AND K_enclave present
```

查不到用户、未知 marker、local_only/无 K_enclave、foreign key 一律不写。

## 11. rollout 顺序

原母计划把 Task 1.3 放在 Phase 1，但新 wire shape 证明这个顺序不成立。调整为：

1. 合并当前 fail-closed inventory guard；
2. 实现 object raw helpers、pointer shape、hydrate/read/replicator/verify；
3. 更新 OpenAPI、公开信任边界与 changelog；
4. iOS 支持 `body_b64` 读写，四象限回归；
5. backend 接受新明文 image/file 写入，但 effective gate 暂不放；
6. test 部署，跑 sealed/plaintext × live/archive × new/old App；
7. iOS 发版并完成强制升级窗口；
8. 打开服务端 effective gate，新写先稳定观察；
9. prod inventory；
10. 单用户/小批 apply，核 app/agent/TEE/verify；
11. 全量 apply；
12. 验收后才允许 Phase 4 cutover。

**禁止**在第 7 步之前执行存量 apply。旧 App 不认识 `body_b64`，先改数据会让历史
图片/文件立即不可读。

## 12. 测试矩阵

### 格式与消费者

- sealed inline / sealed pointer / plaintext inline / plaintext pointer；
- text / image / file；
- live / archive；
- 主正文 + plaintext/encrypted/mixed caption/thinking；
- app history、single-message、resident poll、hosted/V2 file/image、TEE replicator、
  verify、clear、account delete、export。

### 崩溃与竞态

- guard 前后、PUT 前后、CAS 前后逐点注入异常；
- PUT 成功但响应丢失；
- CAS loser；
- 同时 clear chat；
- 同时 account delete/generation advance；
- 同时 reply/push metadata 更新；
- cleanup worker 与 migration 竞争同一 new key；
- archive delete 在 migration 前/中获胜；
- 重跑同一行、同一用户、全表。

### 安全守卫

- encrypted tier 永不产生 plaintext pointer；
- unknown user fail-safe；
- foreign key 不 GET/PUT/DELETE；
- unknown marker 硬失败；
- hash/size mismatch 不交付；
- plaintext bytes/keys/密钥不进日志；
- fake-envelope/surface-freeze 守卫登记 `body_b64` 与 pointer marker，避免把协议新增
  误判成偷偷扩大加密面。

## 13. 验收 SQL / 指标

按 tier 与表分别核：

```sql
-- 明文档：最终不应再有 legacy sealed pointer
COUNT(*) FILTER (
  WHERE doc ? 'body_key'
    AND COALESCE(doc->>'body_object_format', 'sealed_v1') = 'sealed_v1'
)

-- 加密档：不得出现 plaintext pointer
COUNT(*) FILTER (
  WHERE doc->>'body_object_format' = 'plaintext_v1'
)

-- 形状互斥
COUNT(*) FILTER (
  WHERE (doc ? 'body_ct')::int
      + (doc ? 'body_b64')::int
      + (doc ? 'body')::int > 1
)
```

工具/metrics 至少报告：

```text
candidates / skipped_tier / migrated_live / migrated_archive /
cas_lost / decrypt_failed / r2_get_failed / r2_put_failed /
hash_mismatch / unknown_format / foreign_key
```

最终 gate：

- 明文档 live/archive legacy sealed pointer = 0；
- 加密档 plaintext pointer = 0；
- 新 key hash/size 抽样 100%；
- `chat_r2_cleanup` 无持续增长/毒行；
- TEE verify 两档都绿；
- old App 已出强更窗口；
- prod 真实 image/file 往返通过。

## 14. 已拍板（2026-07-31）

1. **明文档的 image/file raw bytes 允许明文存在 R2。**
   这是兑现 enclave-independent 的必要条件，也是公开信任边界变化。
2. `body_b64` 作为第三种 public wire body shape，iOS 强更后才迁存量。
3. archive 迁移采用事务内 delete+insert，不放宽 immutable UPDATE trigger。
4. plaintext pointer 固定带 SHA-256 + raw byte length，读取时校验。

以上四项按推荐方案整体批准。实现不得退回原地覆盖方案；生产 apply 仍受 §11 的
iOS 强更与 rollout gate 约束，设计批准不等于批准生产数据操作。
