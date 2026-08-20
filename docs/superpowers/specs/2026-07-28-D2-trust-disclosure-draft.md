# D2：信任承诺与告知口径（初稿 v1）

> 对应计划 `docs/superpowers/plans/2026-07-23-tee-promotion-decrypt-removal.md`
> Phase 0 Task 0.1 的 D2。**本文是待拍板初稿**，最终口径由产品决策。
> 起草日期 2026-07-28。

## 一、立论基线：这不是信任承诺的回退

D2 最大的风险是被读成「原来承诺 E2E，现在偷偷改成服务端可读」。**核对现有公开
文档后，这个风险基本不成立**——现网口径从来没做过「服务端永不见明文」的承诺：

| 现有原文（`docs-site/content/docs/architecture.mdx`） | 位置 |
|---|---|
| "This is dual-recipient envelope encryption, **not a claim that no server-side component ever sees plaintext**." | L153–154 |
| "Hosted Chat, enclave-backed read projections, normalized Perception processing, user-approved diagnostics, and external inference each have an **explicit plaintext boundary**." | L154–156 |
| "'encrypted at rest' **does not mean** that every identifier, status field, compatibility payload, normalized signal, or diagnostic upload is encrypted." | L45–47 |
| Attested decrypt service 的 "Plaintext visibility" 一栏直接写着 "Plaintext while servicing an authorized operation" | L38 |
| Perception 已存在 encrypted / plaintext / legacy 三分支，文档明说客户端「新的敏感集成应选加密分支」 | L131–133 |

**结论**：现有叙事 = 「双收件人信封 + 受测量飞地可解 + 若干显式明文边界」。
v6 的改动是**把明文边界从「若干显式例外」扩成「默认档」，并把加密变成用户可选**。
信任模型的类型没变，变的是默认值。文案应当据此定调，不必写成道歉或降级公告。

## 二、两档的对外表述

### 默认档：明文（Standard）

- **怎么说**：你的内容以明文存储在我们运营的机密虚拟机（confidential VM）内的
  数据库中。传输有 TLS，静态数据受部署边界与访问控制保护，服务端可读——这是
  agent 能全功能工作、读写更快的前提。
- **不要说**：「不加密」「无保护」。存储仍在 TDX 机密 VM 边界内，不是裸公网库。
- **明确边界**：运维人员在授权流程下可访问；模型 provider 本来就是外部明文
  收件人（现有文档已声明，不是新增暴露面）。

### 可选档：加密（Protected，opt-in）

- **怎么说**：内容以双收件人信封加密写入数据库与对象存储；只有你的设备和经过
  远程证明的解密飞地能打开。**功能不减**——agent、记忆、主动消息照常，靠飞地
  内解密投影工作。代价是读路径多一跳，**稍慢**。
- **信任对象**：TEE 硬件 + attestation（不是「服务端没有钥匙」）。这与今天
  `shared` 可见性的模型**完全一致**，是现状的延续而非新承诺。
- **一句话定位**：**更强隐私、功能不减、稍慢**。

### 一定要避免的三种写法

1. ❌「加密档功能受限 / 降级」——v6 明确加密档功能不降级，写错会劝退。
2. ❌「明文档不安全」——会让默认档（绝大多数用户）恐慌，且不实。
3. ❌「我们看不到你的数据」——加密档也不成立（飞地能解），现有文档已回避这种
   表述，新文案不能倒退回去。

## 三、公开文档改动清单（Phase 5 同 commit 执行）

| 文件 | 改什么 |
|---|---|
| `docs-site/content/docs/architecture.mdx` | 「Envelope and decrypt boundary」节新增两档说明；L144 `K_enclave` 的 "required for `shared` visibility" 改为按用户偏好；删 `local_only` 相关表述 |
| `docs-site/content/docs/index.mdx` | L3/L6 的 "encrypted, user-scoped API" 措辞校准（避免暗示强制加密） |
| `docs-site/content/docs/workflows/{chat,memory,perception}.mdx` | 各写侧新增「按用户 `content_encryption` 偏好产生明文或信封」的分支说明 |
| `docs-site/content/docs/api-keys.mdx` | BYOK 凭证明文化（Phase 1 Task 1.1）后的存储表述 |
| `docs-site/content/docs/changelog.mdx` | `Unreleased` 记一条用户可见契约变更 |
| io-onboarding `skill.md` / `quickstart.md` / `troubleshooting.md` | 若三件套提到加密/可见性需同步（**待核**：本次未逐字检查，落地前必须 grep 一遍） |

## 四、现有用户的告知方式与时点（待拍板）

**背景事实**：现有全体用户是 `shared`（带 `K_enclave`），Phase 1 由飞地解密迁为
明文。对用户而言是**默认档的静默变更**——存储形态变了，可读方从「飞地」扩到
「服务端」。这是本次唯一需要主动告知的实质变化。

起草的告知方案（**三选一，需产品拍**）：

| 方案 | 内容 | 时点 | 代价 |
|---|---|---|---|
| **A. 事前应用内告知 + 可先行 opt-in**（推荐） | Phase 3 的 iOS 版本上线时弹一次说明：默认将迁为明文、可在设置里打开加密档保持现状 | Phase 3 发版 → 至少留一个版本周期 → Phase 4 cutover | 需要 iOS 文案 + 一次弹窗；但把选择权真正交给了用户 |
| B. 仅更新公开文档 + changelog | 不打扰用户，文档写清楚 | Phase 5 | 对「原本以为飞地才能解」的用户等于未告知，口碑风险 |
| C. 事后邮件/推送通知 | cutover 完成后统一通知 | Phase 4 之后 | 既定事实后告知，最差 |

**推荐 A 的理由**：Phase 3 本来就要发带开关 UI 的 iOS 版本，边际成本只是一段
文案；而 Phase 1 的迁明文一旦执行，用户再想回加密档需要重新加密存量（成本更高）。
先告知能让在意的用户在迁移前就 opt-in。

⚠️ **顺序含义**：若选 A，则 **Phase 1（存量迁明文）不能早于 Phase 3（iOS 开关
发版）**——这与当前计划的 Phase 顺序（1 → 2 → 3）相反。这是 D2 拍板会**反向
约束 Phase 顺序**的地方，必须一并决定：
- A-1：调整顺序，先发 iOS 开关再迁存量（更尊重用户，工期更长）
- A-2：保持顺序，迁明文照做，Phase 3 发版时告知「已迁移，可打开加密档重新加密」
  （工期不变，但用户是事后知情）

## 五、需要产品拍板的开放项

1. 告知方案 A / B / C；若选 A，再定 A-1 还是 A-2（**会影响 Phase 顺序**）。
2. 「默认明文」这一档对外叫什么（Standard / 标准档 / 快速档？）；加密档叫什么
   （Protected / 加密档 / 隐私增强？）。
3. 是否给加密档标注「稍慢」的量化口径（例如「读路径多一次飞地往返，约 +XX ms」）
   ——若标数字，需 Phase 2 实测后填。
4. quarantine 的 797 条 / 13 用户（D1 已决定丢弃）**是否单独告知受影响用户**。
   计划 D1 只定了「丢弃」，没定「是否告知」。建议至少给这 13 人单独说明。
