# Siri 接入快速指南

本指南通过 iOS「快捷指令」直接调用 `mijia-agent`。Siri 与 Web 使用同一套助手和权限策略；当前 Phase 2 只支持通用问答和已授权的家庭只读查询，不支持执行场景或控制设备。

## 准备 Automation Token

1. 在米家 Web Console 登录并连接米家账号。
2. 打开「设置 → AI 自动化」，在「AI 助手访问权限」中，只开放 Siri 需要读取的房间指标和设备状态。默认是全部关闭。
3. 在同一页面选择家庭并生成 Automation Token。Token 默认有效 30 天，可设置为 1–90 天；选择的家庭会绑定到 Token。
4. 复制 Token，马上粘贴到快捷指令的认证字段中。它自包含加密的米家会话，请按密码保管。

不要把 Token 放在 URL、快捷指令输入正文、截图或日志里。过期后回到 Web Console 重新签发并更新快捷指令。丢失或泄露时，联系部署管理员处理 Token 密钥轮换。

## 创建快捷指令

在 iPhone 或 iPad 的「快捷指令」中新建快捷指令，并按顺序添加以下动作：

1. **听写文本**，或用「要求输入」收集问题。
2. **获取 URL 内容**：
   - URL：`https://<你的-Makers-域名>/api/ai/assistant`
   - 方法：`POST`
   - 请求头：`Authorization` = `Bearer <Automation Token>`
   - 请求头：`Content-Type` = `application/json`
   - 请求体类型：JSON
   - 请求体：

     ```json
     {
       "text": "客厅温度是多少？",
       "channel": "siri",
       "locale": "zh-CN",
       "timezone": "Asia/Shanghai"
     }
     ```

   将 `text` 的固定示例替换为第 1 步的听写或输入结果。通常省略 `home` 即可，Agent 会使用 Token 绑定的家庭。

3. **从输入中获取词典**，然后获取键 `answer` 对应的词典。
4. 从 `answer` 词典中获取 `speechText`。
5. 添加 **朗读文本**，输入上一步得到的 `speechText`。

生产 EdgeOne 部署的外部路径是 `/api/ai/assistant`；本地 ASGI 服务路径是 `/ai/assistant`。不要使用旧的 `/ai/command` 路径。

## 响应与错误

成功响应包含 `answer.speechText`（最多 280 个字符），以及 `outcome`、可选的结构化 `data` 和 `toolEvents`。家庭读数只有在控制台中明确授权后才会返回；模型收到的是受限的工具结果，不会接收原始设备标识。

常见错误以 JSON `code` 返回：

- `UNAUTHORIZED`、`AUTOMATION_TOKEN_INVALID` 或 `AUTOMATION_TOKEN_EXPIRED`：检查 Bearer 格式，或重新签发 Token。
- `AI_CAPABILITY_UNAVAILABLE`：回到 AI 助手访问权限设置，确认已授权所需房间和数据类型。
- `AI_AGENT_UNAVAILABLE`：Agent、模型网关或家庭读取服务暂不可用。

请求超时意味着结果未知。快捷指令不要自动重试同一次请求；先告知用户请求没有得到确认，再由用户发起新请求。

## 本地验证

配置本地生产测试所需的环境后，可以先用一次性 Siri 渠道只读请求检查短语音输出：

```bash
mijia-agent-local-prod run --profile live-read --channel siri \
  --message '客厅温度是多少？' --expect-tool get_home_environment
```

这会访问生产模型和已授权的真实家庭数据，并产生模型费用。更多安全准备和日志说明见 [local prod test 指南](./local-prod-test.md)。
