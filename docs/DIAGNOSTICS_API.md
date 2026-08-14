# 诊断日志收集 API

让 App 一键把本地 `diagnostics.log` 上报到后端,开发者按 `user_id` 取回排查问题。
实现见 `backend/diagnostics/`。

- **App 只需对接「上报」一个端点**(用户鉴权)。
- 「取回」端点是开发者专用(admin 鉴权),App 不调用。

## 基础信息

| 项 | 值 |
|---|---|
| Prod Base URL | `https://api.feedling.app` |
| Test Base URL | `https://test-api.feedling.app` |
| 上报请求体 | `multipart/form-data`（见 §1） |
| 响应体 | `application/json` |
| 字符编码 | UTF-8(中文/emoji 均支持) |

存储:日志正文存到 Cloudflare R2 桶 `io-user-logs`,key 为 `<user_id>/<上传时间ISO>.log`;
Postgres 仅留一条轻量索引行(`r2_key` + `meta` + `ts`)。R2 未配置时自动回退为把正文
存进 Postgres(本地/测试)。两条路径对 App 都透明,响应一致。

---

## 1. 上报日志（App 调用）

```
POST /v1/diagnostics/logs
Content-Type: multipart/form-data
```

**直接上传日志文件**(multipart/form-data),不是上传文本字段。

### 鉴权
请求头带用户 API key(与其他 `/v1/*` 端点一致):

```
X-API-Key: <用户的 api_key>
```
（也接受 `Authorization: Bearer <api_key>` / runtime token。）

### 表单字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | file part | 是 | `diagnostics.log` 文件本体。非空。**服务端按字节截断到 512 KB**(超出丢弃)。文件名随意(如 `diagnostics.log`)。 |
| `meta` | text part | 否 | 一个 **JSON 对象字符串**,任意键值的设备/环境元数据,**原样存档,服务端不校验字段**。建议至少带 `app_version` / `ios_version` / `device` / `env`。 |

> 体积限制:整个请求体(含 multipart 封装)超过 **2 MB** 会在读取前直接被拒(见 413)。
> 512 KB 的日志 + 元数据远在 2 MB 内,正常不会触发。

### 响应

| 状态码 | body | 含义 |
|---|---|---|
| `201` | `{"status":"ok"}` | 上报成功 |
| `400` | `{"error":"missing_file"}` | 没有 `file` 部分 |
| `400` | `{"error":"empty_file"}` | `file` 为空 |
| `401` | `{"error":"unauthorized"}` | API key 缺失/无效 |
| `413` | `{"error":"payload_too_large"}` | 请求体超过 2 MB |

### 示例

```bash
curl -X POST "https://test-api.feedling.app/v1/diagnostics/logs" \
  -H "X-API-Key: $API_KEY" \
  -F "file=@diagnostics.log;type=text/plain" \
  -F 'meta={"app_version":"1.2.3","device":"iPhone15,2","env":"test"}'
# → 201 {"status":"ok"}
```

### Swift 接入参考

`DiagnosticLog`(`App/Shared/Log.swift`)持有 `diagnostics.log` 的文件 URL。
建议在 `DiagnosticLog` 暴露文件 URL,再在 `FeedlingAPI` 加一个 multipart 上报方法
(复用 `authorizedRequest` 拿到带 `X-API-Key` 的请求,再覆盖 body 与 Content-Type):

```swift
// DiagnosticLog 内：暴露当前日志文件 URL（无内容时返回 nil）
func currentFileURL() -> URL? {
    queue.sync { try? handle?.synchronize() }
    guard let fileURL,
          let size = (try? FileManager.default.attributesOfItem(atPath: fileURL.path)[.size]) as? Int,
          size > 0 else { return nil }
    return fileURL
}

// FeedlingAPI 内：一键上传日志文件（multipart）
func uploadDiagnostics() async throws {
    guard let fileURL = DiagnosticLog.shared.currentFileURL() else {
        throw NSError(domain: "diagnostics", code: 1)  // 无日志可上传 → UI 提示
    }
    let fileData = try Data(contentsOf: fileURL)
    let meta: [String: String] = [
        "app_version": appVersion, "ios_version": iosVersion,
        "device": deviceModel, "env": environmentLabel, "storage_mode": storageMode,
    ]
    let metaJSON = String(data: try JSONSerialization.data(withJSONObject: meta), encoding: .utf8)!

    let boundary = "Boundary-\(UUID().uuidString)"
    var body = Data()
    func append(_ s: String) { body.append(s.data(using: .utf8)!) }
    // file part
    append("--\(boundary)\r\n")
    append("Content-Disposition: form-data; name=\"file\"; filename=\"diagnostics.log\"\r\n")
    append("Content-Type: text/plain\r\n\r\n")
    body.append(fileData)
    append("\r\n")
    // meta part
    append("--\(boundary)\r\n")
    append("Content-Disposition: form-data; name=\"meta\"\r\n\r\n")
    append(metaJSON)
    append("\r\n--\(boundary)--\r\n")

    guard var req = authorizedRequest(path: "/v1/diagnostics/logs", method: "POST", body: body) else {
        throw URLError(.badURL)
    }
    req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
    let (_, resp) = try await URLSession.shared.data(for: req)
    guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
        throw URLError(.badServerResponse)
    }
}
```

> 注:`authorizedRequest` 默认把 Content-Type 设成 `application/json`,所以上面**显式覆盖**
> 为 `multipart/form-data; boundary=...`。

UI 入口建议放在「设置 → 隐私 → Advanced」现有「导出诊断日志」旁边,新增一行
「上传诊断日志」,成功/失败用现有 toast 反馈。

---

## 2. 取回日志（开发者专用，App 不调用）

```
GET /v1/admin/diagnostics/logs/<user_id>
```

### 鉴权
```
X-Admin-Token: <FEEDLING_ADMIN_TOKEN>
```
（也接受 `Authorization: Bearer <token>` 或 `?admin_key=<token>`。）

记忆诊断可用 `GET /v1/admin/users/{user_id}/memory-card-metadata` 分页读取逐卡时间与替代关系元数据，并用 `GET /v1/admin/memory-dream-jobs` 分页读取 dream job 的耗时、失败码和 provider/model 标签；两者均不返回 prompt、回复或记忆正文。

### 响应 `200`

返回该用户**最近最多 10 条**上报(按时间正序):

```json
{
  "user_id": "usr_xxxxxxxxxxxxxxxx",
  "logs": [
    {
      "ts": 1782534475.99,
      "meta": { "app_version": "1.2.3", "device": "iPhone15,2", "env": "test" },
      "r2_key": "usr_xxxx/2026-06-27T04-27-55-991151Z.log",
      "download_url": "https://<acct>.r2.cloudflarestorage.com/io-user-logs/...&X-Amz-...="
    }
  ]
}
```

每条目二选一:
- **R2 路径**:含 `r2_key` + `download_url`(presigned GET,有效期 1 小时,直接浏览器下载 `.log`)。
- **回退路径**(R2 未配置时):含 `content`(日志正文直接内联),无 `download_url`。

| 状态码 | 含义 |
|---|---|
| `200` | 成功 |
| `401` | admin token 缺失/错误 |
| `503` | 服务端未配置 `FEEDLING_ADMIN_TOKEN` |

### 示例
```bash
curl "https://test-api.feedling.app/v1/admin/diagnostics/logs/usr_xxxx" \
  -H "X-Admin-Token: $ADMIN_TOKEN" | python3 -m json.tool
```

---

## 排查闭环建议

`user_id` 是不透明 ID(`usr_` + 16 位 hex),系统不存姓名/邮箱。为了把「某测试者反馈」
对上日志,最顺的做法:**App 上报成功后回显 `user_id`**(也可在隐藏调试面板的 Info 里已有),
测试者反馈问题时报这个 ID,开发者直接 `GET /v1/admin/diagnostics/logs/<user_id>` 取回。

## 留存

- R2 桶 `io-user-logs` 建议配 7 天 lifecycle 过期。
- Postgres 索引流每用户只保留最近 10 条(`log_trim`)。
