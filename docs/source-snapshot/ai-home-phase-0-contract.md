# AI Home Phase 0 Contract

> 状态：Verified
> 冻结日期：2026-09-17
> 依据：[EdgeOne Makers Agents 快速开始](https://cloud.tencent.com/document/product/1552/132786)、[AI Home Agent PoC 详细设计](./ai-home-poc-design.md)

## 1. 平台约束

- Agent 源码使用文件即路由约定，目标入口为 `agents/ai-home/index.ts`，导出 `onRequest(context)`。
- `edgeone.json` 在实现 Agent 时增加 `agents.framework`、`agents.dir` 和 `agents.timeout`；Phase 0 不提前修改部署配置。
- 所有 Agent 请求必须携带 `Makers-Conversation-Id`。平台要求长度为 6–36 个字符，只允许字母、数字、`-`、`_` 和 `.`。
- `AI_GATEWAY_API_KEY`、`AI_GATEWAY_BASE_URL` 和 `AI_GATEWAY_MODEL` 只通过 EdgeOne 项目环境变量或本地关联项目注入，由 Agent 服务端从 `context.env` 读取。
- 仓库不提供生产默认模型。`AI_GATEWAY_MODEL` 必须填写当前项目中人工验证可用的模型 ID；官方文档中的示例模型不等于本项目批准模型。
- 目标 EdgeOne 项目已完成人工关联并确认 Agents 可用；首期批准模型为 `@makers/deepseek-v4-flash`。模型 ID 只记录为验证结果，不成为源码默认值。

## 2. Web Chat Contract

### `POST /api/ai/chat`

鉴权仅接受有效的 `xiaomi_session` Cookie。客户端不得提交 `principalId`、Gateway 凭据、米家凭据或 Agent 内部会话 ID。

请求：

```json
{
  "conversationId": "conv_client_opaque",
  "homeId": "123456",
  "message": "我回家了",
  "idempotencyKey": "0199b0ce-8b8d-7000-a000-000000000001"
}
```

- `message`：必填，去除首尾空白后长度为 1–500。
- `homeId`：必填，必须属于当前登录用户。
- `conversationId`：可选，只能使用服务端签发给当前 principal 与家庭的句柄。
- `idempotencyKey`：只在可能执行副作用工具时必填，长度为 16–128。
- 首期使用非流式 JSON 响应；SSE 不属于本阶段 contract。

成功响应：

```json
{
  "requestId": "req_opaque",
  "conversationId": "conv_client_opaque",
  "message": "欢迎回家，已经开启回家模式。",
  "intent": "activate_scene",
  "tool": {
    "name": "activate_scene",
    "status": "success",
    "sceneName": "回家模式"
  },
  "quota": {
    "mode": "default",
    "remainingRequestsToday": 49,
    "remainingTokensThisMonth": 99872,
    "resetAt": "2026-09-18T00:00:00+08:00",
    "softLimit": true
  }
}
```

所有响应必须设置 `Cache-Control: no-store`，且不得返回真实 Scene ID、DID、原始 Xiaomi userId、Gateway Key、米家 Token 或内部配额覆盖名单。

### Conversation API

- `POST /api/ai/conversations`：为当前登录用户和指定家庭创建不透明客户端会话句柄。
- `DELETE /api/ai/conversations/:conversationId`：只删除当前用户对应 Agent 会话记忆，不影响米家设备、场景和额度账本。
- Web API 生成平台会话 ID，并将其限制为 `Makers-Conversation-Id` 允许的格式；浏览器不能直接指定平台会话 ID。

### Quota API

- `GET /api/ai/quota`：只返回当前 Cookie 对应 principal 的配额摘要。
- 配额为 EdgeOne KV 最终一致性约束下的软限额，响应和 UI 不承诺并发场景下精确不超额。

## 3. Agent Internal Contract

Web API 到 `agents/ai-home/index.ts` 的调用必须由服务端构造，浏览器不得直接组装可信上下文。

请求头：

```text
Content-Type: application/json
Makers-Conversation-Id: conv_<server-derived-id>
Authorization: Bearer <AI_AGENT_INTERNAL_SECRET>
```

内部请求：

```json
{
  "requestId": "req_opaque",
  "principalId": "usr_opaque",
  "homeId": "123456",
  "message": "我回家了",
  "idempotencyKey": "0199b0ce-8b8d-7000-a000-000000000001",
  "scopes": ["ai:chat", "scene:activate"],
  "sessionBinding": "sealed-server-binding",
  "locale": "zh-CN",
  "timezone": "Asia/Shanghai"
}
```

- `AI_AGENT_INTERNAL_SECRET` 只存在于 Web API 与 Agent 的服务端环境，不进入客户端 contract。
- Agent 必须验证内部鉴权和密封 binding，再把 principal、home 与 scopes 作为可信上下文交给工具；模型输出不能覆盖这些字段。
- 模型只看到脱敏场景名称和内部别名，不看到真实 Scene ID、DID、米家会话或任何服务端 Secret。
- `list_scenes` 只能列出当前家庭允许展示的场景；`activate_scene` 必须重新校验 principal、home、scope、审核状态、风险等级和幂等键。
- Agent stop 入口使用 `agents/ai-home/stop.ts`，与主入口共享同一服务端会话映射。

## 4. 错误码

| HTTP | Code | 含义 |
|---:|---|---|
| 400 | `AI_INVALID_REQUEST` | 请求字段、大小或格式无效 |
| 401 | `AI_UNAUTHENTICATED` | Xiaomi Cookie 或内部 Agent 鉴权无效 |
| 403 | `AI_HOME_FORBIDDEN` | 家庭不属于当前用户 |
| 403 | `AI_SCOPE_FORBIDDEN` | 当前入口无工具权限 |
| 403 | `AI_PREVIEW_READ_ONLY` | Preview 环境禁止真实设备动作 |
| 409 | `AI_IDEMPOTENCY_CONFLICT` | 相同幂等键对应不同请求 |
| 409 | `AI_REQUEST_IN_PROGRESS` | 相同幂等请求仍在处理中 |
| 429 | `AI_QUOTA_EXCEEDED` | 当前用户应用配额不足 |
| 429 | `AI_GATEWAY_RATE_LIMITED` | Gateway 限流，遵循 `Retry-After` |
| 502 | `AI_AGENT_UNAVAILABLE` | Agent Runtime 或内部调用不可用 |
| 502 | `AI_GATEWAY_UNAVAILABLE` | Gateway 鉴权失败、5xx 或响应无效 |
| 502 | `AI_SCENE_FAILED` | 米家场景执行失败 |
| 504 | `AI_GATEWAY_TIMEOUT` | Gateway 调用超时 |
| 504 | `AI_SCENE_TIMEOUT` | 米家场景执行超时 |

错误响应只包含稳定 code、用户可读 message、`requestId` 和必要的 quota/retry 摘要，不透传上游响应体、Header 或异常堆栈。

## 5. 迁移与环境决策

- 旧“用户提交模型 Key，并密封进 Automation Token”的方案状态为 **Superseded**。
- 当前 `/api/ai/command` 保留为旧 Siri PoC 兼容入口，但迁移完成前不接入 Web UI；新部署应设置 `AI_COMMAND_ENABLED=false`。
- `/api/ai/command` 只有在后续阶段改为复用同一 `AiAgentService`、Quota Service 和工具校验后才能重新开放。
- Vercel Preview 采用只读预览：允许展示助手入口和 mock 文本，不调用 Makers Agent、不消耗 Gateway 配额、不执行真实米家场景。
- Production、Preview、Development 使用不同的 `AI_AGENT_INTERNAL_SECRET`、`AI_PRINCIPAL_SECRET` 和会话加密 Secret。

## 6. 人工验证记录

| 项目 | 结果 |
|---|---|
| 验证日期 | 2026-09-17 |
| EdgeOne CLI | `1.6.40` |
| `edgeone makers link` | 通过 |
| 目标项目 Agents 能力 | 通过 |
| Gateway 环境变量注入 | 通过，未记录或提交 Secret 值 |
| 首期批准模型 | `@makers/deepseek-v4-flash` |

`edgeone makers dev`、Agent 实际路由、`Makers-Conversation-Id` 和 `/agent-metrics` 在 Phase 4 创建 `agents/ai-home/index.ts` 后验证，不阻塞 Phase 1 Gateway Provider。
