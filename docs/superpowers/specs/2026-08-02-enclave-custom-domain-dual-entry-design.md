# Enclave 自定义域名双入口设计

日期：2026-08-02

## 背景

生产 API 已通过 `api.feedling.app` 对外服务。Cloudflare 只负责该域名的 DNS
管理和 ACME DNS-01 授权；公网 TLS 由主 CVM 内的 `dstack-ingress` 终止，流量
不经过 Cloudflare 橙云代理。

Enclave 客户端目前直接访问 Phala dstack gateway 的 `-5003s` 域名。该入口在
enclave 进程内终止 TLS，并把自签证书指纹写入 TDX attestation 的
`REPORT_DATA`，供旧版 iOS 做证书 pinning。部分地区无法稳定访问
`*.phala.network`，因此需要增加 Feedling 自有域名，同时保留现有验证和兼容
入口。

## 目标

- 为 test、pre、prod 分别增加 `test-enclave.feedling.app`、
  `pre-enclave.feedling.app`、`enclave.feedling.app`。
- 新域名沿用主 API 的外层方案：Cloudflare DNS + CVM 内 `dstack-ingress` +
  Let's Encrypt。
- 旧版客户端继续使用 Phala `-5003s`，无需升级且验证行为不变。
- 新版客户端首选自定义域名，并验证 ingress 的 TDX certificate evidence 和
  enclave 自身 attestation。
- 不让 Cloudflare 或 CVM 外部的反向代理看到 enclave 明文流量。

## 非目标

- 不关闭或替换现有 `-5003s` 入口。
- 不开启 Cloudflare 橙云代理。
- 不通过普通 WebPKI 取代 TDX/compose attestation。
- 不要求已安装的旧版客户端自动切换域名。
- 首期不切换现有 backend、runner 或 CI canary 使用的
  `*_MAIN_ENCLAVE_URL`；它们可继续使用直通入口。

## 架构

每个环境的同一主 CVM 保留现有 `enclave` TLS 服务，并增加一个仅供 Docker
内部网络访问的 `enclave-domain` 服务。两个服务使用相同应用镜像、相同安全
配置和 dstack KMS 路径，因此派生相同的 enclave 内容密钥；两者都属于同一份
受 compose hash 度量的部署。

```text
旧入口（保持不变）

client
  -> https://<app-id>-5003s.dstack-pha-prod9.phala.network
  -> enclave:5003（enclave 自签 TLS）

新入口

client
  -> https://enclave.feedling.app
  -> dstack gateway :443
  -> dstack-ingress（Let's Encrypt TLS 在主 CVM 内终止）
  -> enclave-domain:5004（仅 Docker 内网 HTTP）
```

`enclave-domain` 不声明 host `ports`，只在 compose 网络中向 ingress 暴露
`5004`。其环境变量、runtime token secret、backend URL、数据卷和 dstack socket
与现有 enclave 保持一致；唯一有意差异是监听端口为 `5004` 且
`FEEDLING_ENCLAVE_TLS=false`。现有 `enclave:5003`、自签证书、attestation
指纹和公网 `5003s` 映射不变。

各环境 ingress 的路由变为：

| 环境 | API 路由 | Enclave 域名路由 |
| --- | --- | --- |
| test | `test-api.feedling.app=backend:5001` | `test-enclave.feedling.app=enclave-domain:5004` |
| pre | `pre-api.feedling.app=backend:5001` | `pre-enclave.feedling.app=enclave-domain:5004` |
| prod | `api.feedling.app=backend:5001` | `enclave.feedling.app=enclave-domain:5004` |

`DOMAINS` 同时包含 API 和 enclave 域名。`dstack-ingress` 通过现有 Cloudflare
DNS token 管理 CNAME、`_dstack-app-address` TXT、CAA 和 ACME challenge；DNS
记录必须保持 DNS-only。

## 验证模型

### 旧入口

验证模型完全不变：客户端获取 enclave attestation，验证 TDX quote、已授权
compose hash 和内容公钥，并确认 TLS peer 证书 DER 指纹等于 attestation
`REPORT_DATA` 中的 enclave TLS 指纹。

### 新入口

新版客户端按以下顺序验证，任一步失败都不得继续敏感 enclave 操作：

1. 按系统 WebPKI 验证 `enclave.feedling.app` 的 Let's Encrypt 证书。
2. 从同一 origin 的 `/evidences/` 读取 ingress 的 `cert-<domain>.pem`、
   `sha256sum.txt` 和 `quote.json`。
3. 确认 evidence 中的域名证书与本次 TLS peer 证书一致；确认
   `sha256sum.txt` 覆盖该证书和 ACME account；确认 quote 的
   `report_data` 等于 `sha256(sha256sum.txt)`。
4. 验证 ingress TDX quote 和该环境允许的 reference measurements，证明当前
   域名证书在预期的受度量 TDX 部署中签发和使用。
5. 从同一 origin 获取 `/attestation`，验证 enclave quote、环境对应的
   AppAuth 合约授权、compose hash、内容公钥和 reference measurements。
6. 确认 ingress evidence 与 enclave attestation 均匹配同一环境允许的部署
   measurements。允许同一 app 的不同健康实例响应不同连接，但不允许跨环境或
   未授权 compose 混配。

新入口不再把 Let's Encrypt peer 证书与旧的
`enclave_tls_cert_fingerprint_hex` 比较；该字段只描述 `5003s` 入口。客户端 UI
应分别显示“自定义域名 ingress evidence 已验证”和“enclave attestation 已验证”，
避免把两种证书链混为一谈。

## 客户端选择与失败处理

- 新版客户端默认使用自定义域名；旧版客户端保持原 URL。
- 自定义域名 DNS、TLS、evidence 或 attestation 验证失败时，新版客户端显示明确
  错误，不静默退化为未经 evidence 验证的普通 HTTPS。
- 可提供显式的“兼容/审计直通入口”重试；只有直通域名可达且原 pinning 全部通过
  时才切换。
- 服务端不从旧 Phala URL 重定向到新域名，因为无法在域名被阻断时依赖该重定向，
  且重定向会改变 pinning 语义。
- 任一新域名故障不得影响 API 域名或旧 `5003s` 入口；域名路由必须独立健康检查。

## 发布顺序

1. 在 test compose 增加 `enclave-domain` 和 `test-enclave.feedling.app`，发布新的
   compose hash 并完成环境验证。
2. 更新新版客户端，先仅连接 test 自定义域名并实现双 evidence 验证。
3. 在 pre 重复部署和真实客户端回归。
4. 在 prod 先部署 `enclave.feedling.app`，保持客户端仍走旧入口；验证稳定后发布
   使用新域名的客户端版本。
5. 长期保留 `5003s`，除非未来另有经过迁移设计和客户端覆盖率证明的退役决策。

每次环境推进都必须遵循仓库的 test/pre/main 分支与部署证据要求，不直接从普通
开发分支向 `main` 发布。

## 验收与测试

### 配置和单元测试

- 校验三份 Phala compose 的 `DOMAINS`、`ROUTING_MAP` 和
  `enclave-domain` 环境变量一致且无重复 YAML key。
- 校验 `enclave-domain` 没有 host port，TLS `5003` 服务和 gateway 映射未改变。
- 测试 `/attestation` 在 domain 服务上明确表达“当前 listener 未使用 enclave
  自签 TLS”，客户端不会错误应用旧 fingerprint pinning。
- 为客户端 evidence verifier 覆盖证书不匹配、hash 不匹配、quote 无效、环境
  measurements 不匹配、未授权 compose 和跨环境混配。

### 环境验证

每个环境至少完成：

- 公共 DNS CNAME/TXT/CAA 和 DNS-only 状态核对。
- 系统 CA 验证的新域名 TLS 握手。
- ingress evidence 全链验证，并与实际 TLS peer 证书比对。
- 新域名 `/healthz`、`/attestation` 和 `/v1/decrypt/selfcheck`。
- register -> whoami -> seal -> enclave decrypt 的完整 canary。
- 原 `-5003s` attestation、证书 pinning 和 canary 回归。
- API 域名、WebSocket、runner decrypt health 无回归。
- 至少从一个曾无法访问 `*.phala.network` 的目标地区或网络验证新域名可达。

证书续期路径需在 test 验证 evidence 文件和 HAProxy reload 后仍能通过 peer
证书比对；不能只验证首次签发。

## 文档与可观测性

这项变更修改公开架构和信任边界，实施时必须同步：

- `docs-site/content/docs/architecture.mdx`
- 相关 workflow/self-hosting 信任模型页面
- `docs-site/content/docs/changelog.mdx` 的 `Unreleased`
- `deploy/DEPLOYMENTS.md`
- compose 文件顶部关于 TLS termination、域名和 pinning 的说明

健康监控应分别探测自定义域名和直通域名，并区分 DNS/TLS、ingress evidence、
enclave attestation 与 decrypt self-check 故障，避免一个 200 `/healthz` 掩盖验证
链失效。

## 安全边界

- Cloudflare 只持有最小范围的 `Zone.DNS:Edit` token，不终止或代理业务 TLS。
- 自定义域名的明文只存在于同一受度量 TDX CVM 内的 ingress 到
  `enclave-domain` Docker 网络链路。
- `enclave-domain:5004` 不可通过 dstack gateway、host port 或其他公网路径直连。
- 两个 enclave 服务及其配置都进入 compose hash；镜像或拓扑变更必须重新发布并
  通过 AppAuth 授权。
- 未完成 ingress evidence 验证的客户端不得把新域名标记为与旧 pinning 等价。
