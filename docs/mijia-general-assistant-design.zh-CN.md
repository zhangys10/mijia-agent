# 米家通用家庭助手——项目设计

**状态：** 提议采用的全新架构<br>
**日期：** 2026-09-22<br>
**审阅的仓库：** [zhangys10/mijia-agent](https://github.com/zhangys10/mijia-agent) 与 [zhangys10/mijia-web-console](https://github.com/zhangys10/mijia-web-console)<br>
**决策：** 用通用助手与受限能力循环取代场景路由器思路。只选择性保留有价值的安全和部署组件；不要求兼容旧版迁移路径。

**语言版本：** [English](./mijia-general-assistant-design.md) | 简体中文<br>
**双语维护：** 两个版本应在同一变更中同步更新，并保持章节、决策、接口、示例和状态一致。英文版是规范技术字段、标识符和术语的基准；若发现文本不一致，应在同一变更中修正两版。

## 1. 执行摘要

当前项目在结构上是一个场景意图路由器：

- 每个非预览轮次都会在询问模型之前加载场景；
- 系统提示词将助手定义为家庭控制意图路由器；
- 模型只能做出一个决策，且最多调用一次工具；
- 对外命令流程只识别场景激活；
- “今天天气怎么样”这类有效问题会被判定为 not_understood。

这是一种错误的抽象。产品应成为一个**通用家庭助手**，能够：

1. 直接回答一般问题；
2. 查询天气等当前外部信息；
3. 查询当前家庭及设备状态；
4. 通过受控方式请求家庭操作，例如激活场景；
5. 后续支持提醒及经用户同意的记忆。

家庭操作是助手的能力，而不是助手的身份。面向小米的控制台仍是确定性执行器和凭据边界。新 Agent 负责对话、模型编排、能力选择、工具结果迭代、回答组织和跨渠道行为。

建议采用由精简 Web、Siri 和 EdgeOne 适配器承载的干净核心。它使用逐请求能力注册表、最多四轮迭代、显式风险类别、可选择加入的家庭数据暴露机制，并为任何物理写操作设置独立的持久化操作账本。

## 2. Home Assistant 展示的设计模式

本设计借鉴的是架构模式，不要求使用 Home Assistant 代码或部署 Home Assistant。

### 2.1 对话 Agent 与家庭 API 分离

Home Assistant 将 AI 集成表示为一个对话实体。家庭访问则由独立的 LLM API 提供。其内置 Assist API 向内置对话 Agent 提供相同的受限能力和已暴露实体，并明确排除管理操作。

**采用：** 将对话/模型编排与家庭领域执行和授权分开。

来源：

- [Home Assistant LLM API](https://developers.home-assistant.io/docs/core/llm/)
- [Conversation entity](https://developers.home-assistant.io/docs/core/entity/conversation/)

### 2.2 能力是按请求组装的类型化工具

Home Assistant 集成可以通过延迟调用的 async_get_tools 钩子提供工具。系统会针对每个包含 LLMContext 的请求评估该钩子，因此可用工具会因助手、发起设备、所选 API 和请求上下文而异。

**采用：** 构建 CapabilityRegistry，仅为已认证轮次组装获准工具。工具可用性必须由可信服务端上下文决定，不能由模型参数决定。

### 2.3 真正的 Agent 会把工具结果交回模型

Home Assistant 文档中的对话流程通过聊天记录提供工具，并反复调用模型，直到所有工具结果都得到响应。其示例循环最多允许十次迭代。

**采用更严格的上限：** 每个用户轮次最多进行四次模型迭代、八次读取调用和一次物理写入。工具结果作为下一步模型调用的输入，使助手能在取得数据后自然地组织回答。

### 2.4 实体暴露应由用户选择且尽量精简

Home Assistant 允许用户控制 AI 可以访问哪些设备和实体。其指南建议只暴露最低限度的数据，因为过大的目录会降低准确性、增加延迟和 Token 成本。

**采用：** 在控制台中建立助手可见的数据投影。只有明确暴露的房间、设备、测量值和场景才会成为助手可见的别名。绝不暴露原始 DID、MIoT 地址、凭据或不受限制的服务 API。

来源：

- [OpenAI conversation integration](https://www.home-assistant.io/integrations/openai_conversation/)
- [Assist best practices](https://www.home-assistant.io/voice_control/best_practices)

### 2.5 确定性命令可以在 LLM 之前处理

Home Assistant 的“优先在本地处理命令”路径会先尝试内置的确定性对话 Agent。只有本地 Agent 无法理解请求时才使用 LLM。这能降低常见命令的成本和延迟，同时保留回答一般问题的能力。

**选择性采用：** 精确、无歧义的命令可以走确定性快速路径，但必须使用与模型选择操作相同的策略和操作账本。LLM 仍负责一般问题和含糊表达。

来源：[Home Assistant Voice Chapter 9](https://www.home-assistant.io/blog/2025/02/13/voice-chapter-9-speech-to-phrase/)

### 2.6 将脚本转化为专用工具

Home Assistant 会把已暴露的脚本转换成可调用工具，而不是将其全部塞进静态实体列表。脚本说明会告诉模型该脚本的用途及适用场景。

**采用：** 将经过审核的米家场景视为操作工具或可发现的操作候选项，并提供清晰说明和风险元数据。不要暴露通用小米 API。

来源：[Exposing scripts to LLM conversation agents](https://www.home-assistant.io/voice_control/exposing_scripts_to_llms/)

### 2.7 外部能力可以通过插件接入

Home Assistant 可以作为 MCP 客户端，为对话 Agent 提供外部工具。因此无需将网络搜索或记忆服务合并进家庭控制实现中。

**后续采用：** 现在先定义与提供方无关的能力接口。原生天气和家庭提供方建立安全模型后，再加入受限 MCP 桥接。绝不能自动信任或启用 MCP 发现的工具。

来源：[Home Assistant MCP integration](https://www.home-assistant.io/integrations/mcp)

## 3. 产品定义

### 3.1 产品描述

米家助手是一款支持多语言、与渠道无关的个人家庭助手。它能回答一般问题、查询获准的当前信息、查看用户家庭，并通过确定性执行器执行经过明确授权的家庭操作。

### 3.2 初始使用场景

| 类别 | 示例 | 预期行为 |
|---|---|---|
| 一般知识 | “湿度多少比较舒服？” | 直接回答，不调用家庭接口 |
| 最新信息 | “今天新加坡天气怎么样？” | 调用天气提供方并总结当前数据 |
| 缺少上下文 | “今天天气怎么样？” | 使用明确配置的默认位置，或询问城市 |
| 家庭观察 | “客厅温度是多少？” | 查询已暴露的家庭测量值，并据此回答 |
| 设备观察 | “还有哪些灯开着？” | 查询已暴露的设备状态 |
| 家庭操作 | “执行离家模式” | 解析场景、应用策略、持久化认领、只执行一次 |
| 含糊操作 | “弄暗一点” | 请求澄清，不执行操作 |
| 连续对话 | “那卧室呢？” | 根据范围受限的先前上下文解析 |

### 3.3 首个版本的非目标

- 不提供通用 call_api、原始 REST、原始 MIoT、set_property 或任意服务工具。
- 不支持门锁、摄像头、燃气、门禁、购买或其他安全关键操作。
- 不根据学到的习惯自主执行物理操作。
- 首个版本不支持后台提醒或主动通知。
- 不使用向量数据库。
- 不提供不受限制的网页浏览器或任意 MCP 服务器。
- 不依赖 Home Assistant 安装。
- 不要求保留现有 /ai/command 行为或封闭的意图枚举。

## 4. 架构

~~~mermaid
flowchart TD
    Channel["Web / Siri / 未来语音"] --> Ingress["已认证入口"]
    Ingress --> Engine["对话引擎"]
    Engine --> Local["确定性快速路径"]
    Engine --> Model["模型提供方"]
    Engine --> Registry["逐请求能力注册表"]
    Registry --> General["通用工具：时间、天气"]
    Registry --> HomeRead["家庭读取提供方"]
    Registry --> HomeWrite["家庭操作提供方"]
    HomeRead --> Console["米家控制台 API"]
    HomeWrite --> Policy["策略 + 持久化操作账本"]
    Policy --> Console
    Console --> Xiaomi["小米云"]
~~~

### 4.1 信任边界

| 边界 | 负责内容 | 绝不能接收 |
|---|---|---|
| 渠道/UI | 用户交互和展示 | 服务端密钥、小米凭据 |
| 入口适配器 | 身份验证、配额预留、渠道元数据 | 解密后的小米会话 |
| 助手核心 | 对话和工具编排 | 小米凭据、真实场景 ID、DID |
| 能力提供方 | 类型化外部/家庭操作 | 不受限制的模型权限 |
| 米家控制台 | 小米凭据、设备图谱、别名、执行 | 模型凭据和不受限制的提示词 |
| 操作账本 | 原子化写操作认领和结果 | 小米 Token 或对话内容 |

### 4.2 核心规则

模型可以**请求**使用某项能力，但无权授权或执行。授权、暴露范围、参数校验、风险策略、幂等性和最终结果均由服务端负责。

### 4.3 将平台独立性作为设计约束

EdgeOne 是首个部署平台。当前实现可以直接使用其运行时和服务来交付产品；实现第二个平台或通用插件框架并非前置要求。但 EdgeOne 是可替换的实现选择，不属于助手的业务契约。该要求适用于所有外部依赖，而不仅是存储。

对话规则、能力模式、授权策略、暴露语义、操作结果和公开渠道契约应独立于平台 SDK 类型、请求头、存储键和部署布局。平台集成应位于所属仓库的明确边界中：本仓库的 adapters/edgeone，以及 mijia-web-console 中对应的服务端适配器。运行时入口选择实现并注入规范化配置和依赖。核心不得自行探测运行环境或选择供应商。

增量交付期间可以保留现有直接集成，但必须记录这些耦合，并在相关边界重构或替换时消除。新改动不得把这类耦合扩散到更多业务模块。只需围绕实际操作定义小型接口；不要构建通用 SDK 抽象，也无需提前要求多个生产后端。第 16.3 节记录了所需边界、当前 EdgeOne 选型和替换标准。这些是设计要求，并不表示每个适配器都已实现。

## 5. 请求生命周期

### 5.1 轮次处理

1. 入口验证调用方，并创建可信的 AssistantContext。
2. 对话存储加载范围受限的模型历史投影。
3. 确定性识别器可以处理已知的安全命令。
4. 否则，注册表为当前请求组装工具。
5. 模型收到通用助手提示词、范围受限的历史、渠道提示及工具模式。
6. 若模型返回答案，引擎完成此轮。
7. 若模型请求工具，引擎校验每次调用、执行获准工具，并将经过净化的工具结果追加到临时模型对话记录。
8. 模型给出有依据的最终回答，或再次请求允许的工具。
9. 达到四次模型迭代、八次读取、一次写入、截止时间、取消或写结果不确定时，引擎停止。
10. 引擎保存展示用对话记录，以及单独脱敏后的模型历史投影。

### 5.2 工具循环

~~~mermaid
stateDiagram-v2
    [*] --> Prepare
    Prepare --> Model
    Model --> Complete: 文本回答
    Model --> Validate: 工具请求
    Validate --> Tool: 获准
    Validate --> Fail: 拒绝
    Tool --> Model: 净化后的结果
    Tool --> Complete: 物理写操作已结束
    Complete --> [*]
    Fail --> [*]
~~~

### 5.3 循环上限

- 模型迭代最多 4 次。
- 读取工具调用最多 8 次，每个工具最多 4 次。
- 物理写入最多 1 次。
- 写入不得与其他工具调用并行。
- 写入结果为终态；之后模型不得再请求其他操作。
- 写入分发后发生超时应记为 outcome_unknown；绝不自动重试。
- 整体交互时限按渠道设置，初始为 Web 20 秒、Siri 12 秒；提供方超时预算应更短。

## 6. 助手上下文

可信上下文由入口构造，绝不接受模型工具参数提供这些信息。

~~~python
class AssistantContext:
    request_id: str
    conversation_id: str
    principal_ref: str        # opaque, server-only
    home_ref: str | None      # opaque, server-only
    channel: Literal["web", "siri", "voice", "automation"]
    locale: str
    timezone: str
    initiating_device_ref: str | None
    scopes: frozenset[str]
    consent: ConsentSnapshot
    deadline: datetime
~~~

模型可以看到语言区域、时区、渠道回复风格和选定的非敏感偏好。模型不得看到 principal 引用、home 引用、会话绑定、自动化 Token、DID、真实场景 ID、内部 URL 或服务端密钥。

## 7. 能力框架

### 7.1 能力契约

~~~python
class Capability(Protocol):
    name: str
    description: str
    risk: RiskClass
    input_schema: dict

    async def is_available(self, ctx: AssistantContext) -> bool: ...
    async def invoke(self, ctx: AssistantContext, args: dict) -> CapabilityResult: ...
~~~

~~~python
class CapabilityResult:
    status: Literal["success", "partial", "error", "outcome_unknown"]
    model_content: dict | str | None
    client_data: dict | None
    display_text: str | None
    persistence: Literal["full", "redacted", "none"]
    is_terminal: bool
~~~

model_content 是仅供当前轮次使用的、经过净化和长度限制的视图。client_data 是权威结构化载荷。display_text 供无法展示结构化卡片的客户端使用。持久化策略决定哪些内容可以进入后续模型上下文。

### 7.2 风险类别

| 类别 | 示例 | 默认策略 |
|---|---|---|
| general_read | 时间、天气 | 模式校验通过后自动允许 |
| home_read | 温度、已开启的设备 | 要求家庭成员身份已认证且数据已暴露 |
| home_write_scene | 获得家庭授权的场景激活 | 要求操作范围权限、明确的当前意图和持久化认领 |
| home_write_high | 门锁、安全、燃气 | 不注册 |
| external_write | 消息、购买 | 首期项目不注册 |

### 7.3 初始工具目录

| 工具 | 风险 | 说明 |
|---|---|---|
| get_current_datetime | 通用读取 | 使用请求中指定或获准的时区读取服务器时间 |
| get_weather | 通用读取 | 从配置的提供方获取当前天气和预报 |
| list_home_capabilities | 家庭读取 | 返回经过净化的房间、测量类型、设备类型和场景 |
| get_home_environment | 家庭读取 | 仅返回已暴露来源的温度、湿度和空气质量 |
| get_device_status | 家庭读取 | 返回经过净化的状态投影，不包含原始 ID |
| find_scenes | 家庭读取 | 返回匹配的、不透明的别名及说明 |
| activate_scene | 家庭场景写入 | 只接受本轮可信发现返回的别名 |

当操作范围、持久化账本、控制台执行门控、当前场景版本或家庭授权不可用时，不暴露 activate_scene。

## 8. 一般回答与当前信息

### 8.1 直接由模型回答

系统提示词应定义一个有帮助的通用助手，而非意图路由器。模型可以直接回答稳定的一般知识问题。没有成功的工具调用时，不得声称掌握当前信息、私人信息或家庭状态。

### 8.2 天气能力

**决策：** Phase 1 原生天气提供方使用 [Caiyun Weather v2.6](https://docs.caiyunapp.com/weather-api/v2/v2.6/6-weather.html)。MVP 仅面向中国大陆。

选择理由：

| 考量 | 评估 |
|---|---|
| 中国覆盖 | 本 MVP 选择 Caiyun v2.6 天气接口，可接收经纬度目标 |
| 数据 | 单次组合请求可返回实时及逐日天气；实时数据按分钟更新，逐日预报批量发布 |
| 身份验证 | 使用 Caiyun 推荐的签名 App Key/App Secret 模式：每次请求使用 HMAC 签名、随机数和时间戳。App Secret 仅保留在服务端，两个凭据均不得进入模型上下文、客户端响应或日志 |
| 延迟 | 只有在 EdgeOne 部署探测证明预定中国执行路径的 p95 ≤ 1.5 秒且 p99 ≤ 3 秒后才接受 |
| 预警 | API 仅在明确请求且 Token 有相应权限时返回预警。Phase 1 设置 alert=false，在预警策略设计完成前返回 alertsSupported: false |

参考：[组合天气接口](https://docs.caiyunapp.com/weather-api/v2/v2.6/6-weather.html)、[实时天气字段](https://docs.caiyunapp.com/weather-api/v2/v2.6/1-realtime.html)及[逐日天气字段](https://docs.caiyunapp.com/weather-api/v2/v2.6/4-daily.html)。

适配器按解析后的位置、语言和预报窗口缓存规范化结果五分钟。提供方时限为三秒，不自动重试。服务端使用 AMap 将用户提供的中国城市或区域名称解析为坐标，再仅将坐标发送给 Caiyun；不得向用户索取或展示坐标。查询失败时返回通用的稍后重试提示。即使无法提供预警，天气预报仍有价值，但结果必须明确包含 alertsSupported: false。

**MCP 决策：** MVP 不使用 Caiyun MCP 服务器。它会暴露多个远程工具，包括历史数据和预警；而本 Agent 只需要一个本地版本化模式、固定请求预算、确定性结果投影，以及不向模型暴露凭据的路径。只有在多个独立管理的提供方确实需要受限 MCP 适配器时，才重新评估 MCP。

位置解析顺序：

1. 当前请求中明确说出的城市或区域名称；
2. 用户明确配置的默认地点；
3. 经同意、由控制台提供的粗略家庭位置投影；
4. 否则询问用户澄清。

绝不根据 homeId、家庭名称、IP 地址或时区推断位置。工具输出包括提供方、观测/预报时间戳、时区、单位和数据新鲜度。助手必须指出数据过期或不可用，不能猜测。

初始实现应通过小型接口使用一个原生天气提供方：

~~~python
class WeatherProvider(Protocol):
    async def forecast(self, location: WeatherLocation, days: int) -> WeatherSnapshot: ...
~~~

不要仅为获得天气而引入 MCP。只有在需要多个分别管理的外部能力提供方时，MCP 才有价值。


## 9. 家庭语义层与数据暴露

### 9.1 助手可见的数据投影

控制台应暴露一个版本化、经过净化的数据投影，包括：

- 房间/楼层的显示名称和别名；
- 已暴露设备的显示名称、类别、所属房间及支持的读取能力；
- 已暴露的测量类型和来源标签；
- 获得家庭授权的场景别名、名称、说明、版本哈希和操作摘要；
- 数据新鲜度和完整性元数据。

不得包含原始设备标识符、MIoT SIID/PIID 值、账户标识符、凭据、未经批准暴露的拓扑证据或不受限制的操作名称。

### 9.2 选择性加入的数据暴露

每个家庭只配置一份暴露设置，并由该家庭当前所有已授权成员共享。每次请求仍须逐个主体检查授权，但成员不能各自维护互不一致的暴露列表。默认策略应保守：

- 可以建议暴露只读环境测量值；
- 设备须由用户选择加入；场景须逐项批准，或明确确认开启家庭级跳过逐项审批；
- 敏感设备类别不符合设备状态读取条件；场景授权按上述场景设置处理；
- 用户可以查看和撤销暴露；
- 修改设置无需重新部署 Agent 即可生效。

在仅支持米家的首版中，完整实体清单和暴露 UI 由 mijia-web-console 负责。在“家庭 → AI 助手访问”页面中提供：

- 家庭级助手启用开关；
- 房间和环境测量值读取开关；
- 设备状态读取开关；
- 场景操作总开关、逐项场景批准，以及须确认的跳过逐项审批开关和当前版本标记；
- 来源、上次同步、上次修改以及“批准后已更改”指示；
- 一键撤销所有助手访问权限。

设置页的可选项必须来自控制台当前的读取结果：只有环境采集器返回有效读数的房间与测量类型组合才可供选择；设备只有出现在共享的设备状态投影中才可供选择。不得根据静态测量类型构造房间与指标的笛卡尔积，也不得将所有已发现设备都作为状态来源。暂时缺失的读数不能获得新授权；既有授权记录保留至用户保存修改后的配置。工具边界仍在每次调用时重新检查暴露范围。

将暴露记录关联到 provider + providerEntityRef 这类与提供方无关的键。初始 provider=mijia；未来可将 Home Assistant 作为另一来源加入，而无需改变 Agent 契约或每户共享规则。

### 9.3 延迟发现

一般问题不得调用控制台或加载场景目录。只有在模型或确定性识别器选择了家庭相关操作后，才获取家庭能力。缓存的只能是经过净化的数据投影，并应按家庭、主体授权、版本和较短 TTL 进行约束。

## 10. 物理操作安全

### 10.1 授权条件

以下条件必须全部满足：

- 当前家庭成员身份已验证；
- 具备所需操作范围权限；
- 场景已启用、版本匹配，且有逐项批准或已确认的家庭级跳过逐项审批授权；
- 请求表达了明确的当前意图，或使用了有效的确认票据；
- 别名来自可信上下文中的发现结果；
- 已原子获取持久化幂等认领；
- 执行功能门控已启用。

### 10.2 持久化操作账本

现有幂等键仍作为请求身份，但单有键值并不能保证安全执行。控制台必须在不同工作进程、部署、重试和对话之间持久化并原子认领该键。

**初始实现决策：** 权威账本放在 mijia-web-console 的 EdgeOne Blob 上，不使用 EdgeOne KV。Blob 通过 setJSON(key, value, {onlyIfNew: true}) 提供强一致性读取和条件创建。KV 可用于配额或缓存，但其他边缘节点可能读取到最多 60 秒前的陈旧值。可移植契约应包含原子持久化认领、不可变结果记录和权威重放读取；替代实现须保留这些语义，而非复刻 Blob API。迁移和验证要求见 §16.3。

认领键是 environment + principal + home + idempotency_key 的摘要。不可变认领记录包含：

- 规范请求哈希；
- 能力和不透明目标别名；
- 已批准的目标版本；
- principal/home 范围摘要和创建时间戳。

单独的不可变生命周期记录表示 dispatched、succeeded、partial、failed 或 unknown，并包含长度受限的净化结果和协调尝试记录。

认领使用 onlyIfNew 创建。发生重复时，控制台执行强一致性读取：

- 规范请求哈希相同：返回已记录结果或 pending/unknown，不再分发；
- 请求哈希不同：以 IDEMPOTENCY_KEY_REUSED 拒绝；
- 已 dispatched 但没有最终结果：返回 outcome_unknown 并执行协调，不得再次执行。

由于有文档记录的 Blob API 支持“不存在时创建”但不提供通用 compare-and-swap 更新，应使用不可变对象，避免相互覆盖：

~~~text
actions/claims/{claimHash}.json
actions/dispatched/{claimHash}.json
actions/outcomes/{claimHash}.json
actions/reconciliation/{claimHash}/{attemptId}.json
~~~

每个权威对象都使用 onlyIfNew 写入；读取和协调列表使用强一致性。可以为方便而维护派生的可变状态对象，但它不具权威性。启用物理执行前，Phase 0 必须在多个已部署工作进程上对 onlyIfNew 进行并发测试，并记录确切的冲突响应。

删除对话记录不得删除操作回执。EdgeOne KV 和进程内锁不能充当权威账本。

参考：[EdgeOne Blob](https://pages.edgeone.ai/zh/document/blob-storage)及[EdgeOne KV](https://pages.edgeone.ai/document/kv-storage)。

### 10.3 响应结果的权威来源

执行器结果具有权威性。模型生成的文本不能改变 success、partial、failed 或 unknown 状态。对于操作，应优先根据执行器结果生成确定性的服务端确认文本。

## 11. 对话、记录和记忆

### 11.1 分离展示记录与模型历史

用户可能需要查看或听到精确温度，但后续模型轮次不应自动收到所有历史测量值。应存储两个投影：

- **展示记录：** 用户可见的回答和结构化卡片；
- **模型历史：** 范围受限、经过脱敏的对话内容。

例如，展示记录可以包含“客厅目前 25.5°C”，而模型历史只保存“已回答用户关于客厅当前温度的问题”。默认情况下，工具结果仅在当前轮次有效。

### 11.2 EdgeOne Makers 存储适配器

EdgeOne Makers 的 context.store 适合作为持久化后端。其通用消息 API 与框架对话/会话存储明确分离，通过平台 Blob 存储跨实例持久化，并可用于 Agent 和 Cloud Function 运行时。

将持久化放在仓库自有的 ConversationRepository 后面；不要让对话引擎直接依赖 context.store：

- 通用 appendMessage / getMessages 存储完整展示记录和结构化卡片元数据；
- context.store.state 中的 model_history_v1 键存储范围受限、经过脱敏的模型历史投影；
- 原生框架会话适配器为可选项，并且不得在仓库脱敏策略之外持久化原始私人工具结果；
- stop/delete/history 接口通过同一个仓库调用，从而保留更换平台的可能。

这种方式使用 Makers 存储，不必引入外部数据库，同时保留产品的双投影隐私契约。跨实例持久化要求 EdgeOne CLI 1.6.26 或更高版本。

context.store 与直接调用 Blob SDK 是两种不同的平台集成。前者提供平台托管的对话持久化；后者供控制台负责的暴露和操作存储使用。两者都应隐藏在各自领域操作之后，不要把 API 暴露给引擎。对话契约应定义排序、保留期限、范围身份、删除和故障行为，使替代方案无需依赖 Makers 对话 ID 或存储布局也能保留这些语义（§16.3）。

参考：[EdgeOne Makers conversation management](https://cloud.tencent.com/document/product/1552/132787)。

### 11.3 初始记忆

- 保留最近 12 条经过脱敏的用户/助手消息。
- 不自动提取偏好。
- 不持久化工具载荷。
- 不得从历史记录推导操作授权。
- 用户明确删除对话时，应删除展示记录和模型历史，但不删除审计/操作回执。

### 11.4 后续记忆

后续阶段可以增加：

1. 有长度上限的对话摘要；
2. 用户可查看/编辑/删除并明确同意的偏好；
3. 来自控制台的版本化家庭语义。

学到的习惯只能生成建议，绝不能静默触发物理操作。

## 12. API 设计

### 12.1 标准内部轮次 API

~~~json
POST /internal/v1/assistant/turn
{
  "requestId": "req_...",
  "conversationId": "conv_...",
  "message": "今天新加坡天气怎么样？",
  "locale": "zh-CN",
  "timezone": "Asia/Shanghai",
  "channel": "web",
  "idempotencyKey": "...",
  "trustedBinding": "opaque"
}
~~~

principal、home、scopes、consent 和 exposure 均从已认证服务端上下文解析，不是调用方或模型可选择的字段。

### 12.2 标准响应

~~~json
{
  "requestId": "req_...",
  "conversationId": "conv_...",
  "status": "completed",
  "outcome": "tool_answer",
  "answer": {
    "text": "新加坡今天炎热并有阵雨可能。",
    "speak": true,
    "continueConversation": false
  },
  "data": {
    "type": "weather",
    "capturedAt": "2026-09-22T...Z",
    "freshness": "fresh"
  },
  "toolEvents": [
    {"name": "get_weather", "status": "success"}
  ],
  "usage": {
    "promptTokens": 0,
    "completionTokens": 0,
    "totalTokens": 0,
    "estimated": false
  }
}
~~~

outcome 的取值：

- direct_answer
- tool_answer
- clarification
- action_result
- refused
- failed
- outcome_unknown

不得仅仅因为一个有效的非家庭问题无法映射到场景，就将其标记为 not_understood。

### 12.3 渠道适配器

- Web 可以渲染结构化数据和较长文本。
- Siri 通过自动化 Token 直接调用标准 Agent 接口，并接收不含 Markdown、适合朗读的简短文本。
- 语音以后可以支持流式响应，但工具调用仍遵循同一核心流程。
- 自动化请求可以禁止澄清，并改为返回结构化错误。

所有渠道调用同一对话引擎并使用同一能力策略。

Siri Token 只放在授权请求头，不得出现在请求正文、模型输入、对话记录或日志中。控制台签发的 Token 可撤销且绑定特定受众。Agent 在入口验证外层授权信封，但无法解密小米凭据；只有在调用家庭能力时，才转发不透明的控制台授权。一般 Siri 问题通过入口认证后无需调用家庭提供方。

## 13. 建议的源代码布局

~~~text
src/mijia_assistant/
  api/
    app.py
    models.py
    errors.py
  conversation/
    engine.py
    prompt.py
    history.py
    response.py
  capabilities/
    base.py
    registry.py
    policy.py
    datetime_tool.py
    weather/
      models.py
      provider.py
      tool.py
    home/
      provider.py
      models.py
      environment.py
      devices.py
      scenes.py
  providers/
    base.py
    openai_compatible.py
  security/
    context.py
    action_ledger.py
    sanitization.py
  adapters/
    mijia_console.py
    edgeone.py
  observability/
    events.py
    metrics.py
tests/
  unit/
  contract/
  integration/
  safety/
~~~

包可以保留仓库名称 mijia-agent；代码应使用 mijia_assistant 这类与具体能力无关的包名。

## 14. 现有项目复用评估

迁移兼容性不是目标。“复用”是保留良好的概念或小型实现，而不是保留现有模块边界。

| 现有部分 | 决策 | 理由/目标位置 |
|---|---|---|
| config.py 的 URL 校验、密钥分离和模型白名单 | 适配后复用 | 默认值合理；扩展提供方、循环、天气、时限和功能门控 |
| gateway.py 的传输加固、用量解析和重定向策略 | 复用概念，重写接口 | 必须支持规范化的流式/非流式事件和重复工具调用 |
| llm_log.py 的非致命日志 | 只复用概念 | 用结构化脱敏事件和可配置采样取代原始提示词日志 |
| app.py 的内部认证、请求体限制、no-store 响应和脱敏错误 | 大量复用 | 围绕一个标准引擎整合重复的入口行为 |
| console.py 的严格响应解析和禁止写入重试 | 大量复用 | 改名/整合为 MijiaHomeProvider，并添加暴露投影和发现 |
| command_console.py 的自动化 Token 转发 | 在渠道入口适配 | 不要保留独立的命令决策核心 |
| service.py | 替换 | 单步封闭枚举和提前加载场景体现了错误架构 |
| command_service.py | 替换 | 重复的纯场景编排 |
| command_rules.py | 拆分并大部分替换 | 在场景策略中保留净化器和保守操作短语；替换路由器提示词 |
| models.py 的家庭状态校验 | 选择性复用领域模型 | 围绕与能力无关的结果/决策重新构建类型 |
| command_models.py | 替换 | intent=activate_scene|none 和 not_understood 是应移除的产品级限制 |
| command_idempotency.py | 不用于写操作 | 可作为读取操作的进程内优化，但绝不能作为权威记录 |
| EdgeOne shared.ts 的认证、范围 ID、停止/删除流程 | 审核后复用 | 保持适配器精简；更新标准 API 和历史投影 |
| EdgeOne 回执状态 | 写操作场景下替换 | 对话重放缓存不是原子物理操作账本 |
| EdgeOne 范围受限历史 | 复用概念 | 分别存储展示/模型投影并明确保留期限 |
| local_prod.py / mijia-agent-local-prod | 复用加固后的测试外壳，替换协议 | 保留配置校验、密钥隔离、生产环境确认、Token 流程、回环服务器生命周期和保守传输行为。替换旧路由导入、/ai/command、载荷/响应模型、历史行为和场景意图展示。绝不自动回退到旧接口。 |
| 现有安全/凭据测试 | 移植 | 不变量仍有价值；行为测试数据需重写 |
| 部署文件和 CI | 复用 | 更新包路径并增加提供方契约测试 |
| 现有架构/对齐文档 | 作为历史资料归档 | 它们描述的是场景路由器迁移，而非新产品 |

### 14.1 旧路由器的弃用与移除

现有场景路由器实现立即弃用，包括：

- service.py 的单步场景编排；
- command_service.py 及其重复命令流程；
- command_rules.py 中特定于路由器的部分；
- command_models.py 中封闭的意图/结果类型；
- 仅供这些路径使用的旧接口、配置、测试数据和测试。

实现期间：

- 不要为旧路由器增加能力或产品行为；
- 仅允许关键安全或生产稳定性修复；
- 将每个新调用方和测试迁移到标准对话引擎；
- 旧接口只能通过显式部署开关访问，并发出结构化弃用/流量指标；
- 绝不从标准引擎自动回退到旧操作路径，否则可能绕过新策略或重复执行物理操作。

旧代码只用于临时回滚，不是兼容性要求。以下门槛全部满足后，在核心项目完成时移除：

1. Web 和 Siri 仅使用标准助手接口。
2. 直接回答、天气、家庭读取和获准场景操作均通过验收及安全测试。
3. Blob 操作账本和场景版本检查已在生产环境启用。
4. 旧路由器流量在一个生产观察窗口内为零，且至少持续 14 天。
5. 运营人员批准移除，且标准部署有独立回滚制品。

移除时删除上述模块，以及对应路由、封闭意图模式、功能开关、环境变量、路由器提示词、过时测试和部署配置。可复用的净化器或策略短语必须先迁入与能力无关的模块；删除后不能有任何代码反向依赖已弃用路由器。

### 14.2 Phase 0 本地测试 CLI 评估

现有 mijia-agent-local-prod CLI 可以保留为 Phase 0 运维冒烟测试客户端，但不能原样验证新助手。目前它会启动真实的本地 ASGI 应用，并安全连接生产 Gateway 和控制台服务；随后调用旧的 POST /ai/command 场景路由契约、校验 CommandRequest/CommandResponse、维护旧版客户端历史，并呈现封闭的场景意图字段。因此它是一个**使用生产依赖的本地进程**，而不是离线本地助手测试。

保留已测试的部分：

- 严格且不会执行代码的环境文件解析，以及明确的子进程环境白名单；
- 已脱敏的 check 输出、HTTPS/非回环生产目标校验，以及精确的生产使用确认；
- 控制台生成的自动化 Token、受保护的 Cookie/Token/日志文件，以及避免在参数或子进程环境中泄露密钥；
- 回环 Uvicorn 启动、就绪轮询、范围受限的关闭、代理绕过、拒绝重定向、不自动重试写操作以及显示未知结果；
- 单次和交互工作流，并提示真实模型成本和生产数据访问。

在 CLI 可计入 Phase 0 验收之前，替换以下依赖：

- 从 command_models 和 command_rules 导入；
- /ai/command 接口及场景路由器请求/响应契约；
- intent、sceneName 和 decisionSource 的呈现；
- 将客户端管理的旧历史作为隐式事实来源；
- 自动兼容回退到弃用路由器的行为。

将 CLI 改为调用标准助手接口和规范化轮次/结果事件。它应呈现回答文本、结果、能力/工具事件、操作结果和用量，而不解析供应商专有字段。对话状态须通过与其他调用方相同的 ConversationRepository 行为；只有明确命名时才能提供无状态本地模式。

Phase 0 继续使用现有可执行程序名称，以免再造一个测试外壳。定义三种显式工作流：

| 命令 | 网络和凭据 | 用途 |
|---|---|---|
| mijia-agent-local-prod check | 不联网 | 校验配置、模型白名单、URL 策略、文件权限和所选测试配置，不打印密钥 |
| mijia-agent-local-prod smoke --profile fake | 仅本地；不需要小米 Token 或生产模型密钥 | 运行确定性直接回答、澄清、模拟天气工具循环、格式错误工具、截止时间和策略拒绝案例 |
| mijia-agent-local-prod run --profile live-read | 真实 Makers AI Gateway 和生产控制台只读 API | 验证所选 AI_GATEWAY_MODEL、工具调用续接、用量上报、授权及已暴露的家庭读取；提示会产生费用并访问真实家庭数据 |

两个配置中都必须禁用物理写入。一般问题和模拟天气案例不得需要小米 Cookie、自动化 Token、控制台检出或家庭提供方调用。live-read 可以复用现有控制台 Token 生成器，但只能用于授权和读取能力。每个请求均应显示请求/幂等标识；结果含糊时绝不自动重试。

CLI 不能证明 EdgeOne 路由、Makers context.store、配额结算、跨工作进程 Blob 认领、对话记录持久化或真实物理操作有效。这些都需要部署路径契约和运维手册测试。若临时保留旧模式，必须显式选择并发出弃用遥测；标准模式不得把它作为回退路径。

Phase 0 CLI 验收必须全部满足：

1. check 不联网并且所有密钥均已脱敏。
2. 模拟冒烟测试在不加载控制台提供方的情况下，证明直接回答和两步天气工具循环。
3. live-read 证明配置模型的工具续接与用量规范化，并能读取已暴露的环境或设备状态。
4. 请求物理写入时，在分发前拒绝。
5. 格式错误的工具参数会安全拒绝；超时和含糊结果会显示且不重试。
6. Token、Cookie、提示词、工具结果和日志的处理维持仓库现有凭据隔离不变量。

## 15. 作为家庭工具 API 的 mijia-web-console

控制台已经是访问小米服务的正确信任边界。它负责小米会话、主体推导、当前家庭成员校验、不透明场景别名映射和家庭状态净化。Agent 不应重复这些职责。

当前实现是良好的起点，但不是完整的助手契约。正在退役的 POST /api/ai/tools 分发逻辑有两种已认证请求信封：

- 携带已验证会话绑定、主体、家庭和 scopes 的服务请求；或
- 服务请求加上 X-Ai-User-Token，由控制台从中推导主体并解析获准家庭。

这两条路径都能正确保证小米凭据留在控制台内。它们也会实施 32 KiB 请求体限制、严格参数校验、Cache-Control: no-store、错误脱敏和新的家庭成员资格检查。这些属性应保留。Phase 1 的标准 Web、Siri 和本地生产测试统一使用自动化 Token 信封；会话绑定路径只保留为旧路径，不再增加新的助手行为。

### 15.0 Phase 1 标准上下文及后续门槛

Phase 1 的标准流程刻意保持只读：

~~~text
authenticated Web/Siri ingress
  -> short-lived opaque automation token
  -> Makers adapter authorize(token)
  -> console re-derives principal and home
  -> adapter compares trusted context before loading history
  -> Python capability loop
  -> console is called with the same token only for a selected home read
~~~

Token、小米会话、主体/家庭标识符和工具响应都不会进入模型输入或普通日志。Token 载荷中的身份字段不具权威性：只有控制台新近返回的 authorize 结果才能确立适配器的主体/家庭上下文。

idempotencyKey 是请求/回执标识，不是权限。它当前可以支持重复读取重放，但绝不能让不可信客户端取得 scene:activate 资格。因此 Phase 1 只暴露 ai:chat，并且不注册任何物理写能力。

后续工作必须保留此顺序：

1. **家庭读取完善：** 增加以下版本化清单和过滤读取 API，并添加明确的家庭级暴露策略。保留自动化 Token 信封；不要恢复会话绑定作为第二条标准路径。
2. **操作前置条件：** 增加场景别名、暴露及场景版本、必要时的显式确认，以及由控制台负责的持久化 Blob 操作账本。必须在调用小米分发之前，基于主体、家庭、幂等键、规范操作哈希和版本原子创建账本认领。
3. **注册操作能力：** 只有前置条件已部署并完成并发测试后，控制台才能从可信上下文签发/确认操作范围。适配器/Python 仍须将其视为服务端派生权限，并且每轮只允许一次终态写入、不得重试。
4. **退役旧路径：** 将所有剩余调用方迁移至标准 Token 信封，在约定窗口内确认会话绑定流量为零，然后一并删除会话绑定路由、模式、测试和部署配置。不要保留回退行为。

### 15.1 当前 API 清单

| 当前工具 | 当前行为 | 设计决策 |
|---|---|---|
| authorize | 校验请求上下文并返回 {ok: true} | 保留为内部健康/认证操作，不暴露为模型工具 |
| list_scenes | 返回已启用的手动场景，以主体/家庭范围的不透明别名、名称、通用说明和操作数量表示 | 复用别名机制；将粗略目录替换为带暴露、版本和可搜索摘要的目录 |
| get_home_status | 返回脱敏的温度、湿度、空气质量、气压和电量读数，并包括完整性和警告 | 复用采集器；作为过滤后的 get_home_environment 暴露给 Agent |
| get_device_status | 返回范围受限的房间/设备投影，状态为 on、off 或 unknown，并含在线状态 | 复用采集器；增加助手暴露及校验后的房间/类别/状态过滤器 |
| activate_scene | 校验 scope、别名、参数及幂等性，然后始终拒绝执行；预览只读 | 持续禁用，直到持久账本、版本绑定、授权策略和功能门控就绪 |

环境采集器已经具备多项理想语义：读取公开 MIoT 规格、选择可读属性、规范单位、批量读取属性、过滤离线来源、保留部分失败，并省略小米原始标识符。设备采集器使用与仪表盘相同的设备同步模型，保留 unknown、省略原始 ID/spec 元组并限制结果大小。应将它们作为与提供方无关的 Agent 能力实现，而不是从头重写。

### 15.2 当前控制台契约的缺口

1. **没有助手暴露策略。** 状态工具返回所有符合条件的家庭数据，list_scenes 返回所有已启用手动场景。小米中已启用不等同于已批准供 AI 使用。
2. **没有能力清单。** Agent 必须预先知道硬编码的控制台操作和模式列表。
3. **没有过滤读取。** 两个状态工具都要求空参数，因此查询单个房间也会读取整个范围受限的家庭投影。
4. **场景别名不绑定版本。** 编辑场景不会改变别名，因此此前审核的操作可能在语义悄然改变后继续被执行。
5. **场景摘要不足以安全选择。** Agent 只收到通用说明和操作数量，没有脱敏操作摘要或版本。
6. **没有持久化操作账本。** 请求校验和幂等字符串无法提供跨进程原子执行认领。
7. **没有物理执行路径。** 控制台小米层有 runManualScene，但远程助手路由有意不调用它。
8. **固定分发不是动态能力组装。** 它无法表达逐家庭/逐主体可用性，除非在路由外增加策略。

### 15.3 推荐的版本化内部 API

由于不要求迁移兼容性，应增加版本化助手 API，并在调用方迁移后退役现有路由：

~~~text
POST /api/internal/assistant/v1/capabilities
POST /api/internal/assistant/v1/tools:invoke
~~~

两个端点都将标准自动化 Token 信封解析为单一内部 ResolvedHomeContext。此上下文而非模型输入包含主体、所选家庭、scopes、预览模式、暴露版本和授权有效期。不要仅为保留会话绑定兼容而增加第二条运行路径。

capabilities 返回范围受限且经过净化的清单，例如：

~~~json
{
  "contextVersion": "1",
  "exposureRevision": "exp_...",
  "capabilities": [
    {"name": "get_home_environment", "available": true, "risk": "home_read"},
    {"name": "get_device_status", "available": true, "risk": "home_read"},
    {"name": "find_scenes", "available": true, "risk": "home_read"},
    {"name": "activate_scene", "available": false, "risk": "home_write_scene"}
  ],
  "projection": {
    "rooms": ["客厅"],
    "measurementTypes": ["temperature", "humidity"],
    "deviceKinds": ["light"],
    "roomMetrics": {"客厅": ["temperature", "humidity"]},
    "roomDeviceKinds": {"客厅": ["light"]},
    "sceneSearchAvailable": true
  }
}
~~~

Agent 负责面向模型的 JSON Schema，绝不将任意远程说明或 Schema 注入模型提示词。遇到家庭相关问题时，模型先选择本地发现工具；Agent 获取暴露清单，再据其中获准的房间、测量类型和设备类别生成受约束的家庭读取工具 Schema。选中的读取工具只发起一次 tools:invoke 请求；控制台独立地重新检查当前可用性、授权和暴露范围。控制台复用一次设备发现结果，同时完成清单校验和所选采集器调用。读取环境数据时，它先将请求过滤条件与当前暴露范围求交，再为这些房间与测量类型组合批量读取 MIoT 属性。Agent 发送请求前按清单校验参数；控制台返回脱敏结果，Agent 将结果与原问题一并交给模型生成最终回答。一般问题不会获取家庭暴露清单。

tools:invoke 使用以已知操作为键的严格联合类型：

| 操作 | 参数 | 结果/策略 |
|---|---|---|
| get_home_environment | 可选、已暴露的 rooms 和 metrics | 脱敏读数、完整性、警告和 capturedAt |
| get_device_status | 可选、已暴露的 rooms、封闭集合 kinds 和 states | 脱敏且范围受限的设备；unknown 保持 unknown |
| find_scenes | 有长度上限的文本 query | 已暴露的不透明别名、版本和脱敏操作摘要 |
| get_scene_details | sceneAlias | 当前已暴露版本及足够用于安全澄清/确认的细节 |
| activate_scene | sceneAlias、expectedRevision，以及必要时由策略签发的确认凭证 | 原子认领后的终态操作结果；绝不盲目重试 |

控制台必须根据暴露投影校验所有过滤条件。过滤只减少披露，不能授予访问权。家庭和主体标识符仍从可信上下文派生。

### 15.4 暴露、版本与执行归属

增加显式的家庭级助手暴露记录，涵盖房间、测量值、设备和场景。该家庭所有已授权成员共用同一配置。默认不暴露任何内容。控制台 UI 是管理它的自然位置，因为该应用已呈现权威家庭模型。

对于场景，保存或推导：

- 限定于主体和家庭的不透明别名；
- 批准配置的 exposureRevision；
- 根据动作语义及目标标识推导、排除显示名称的 sceneRevision；
- 脱敏操作摘要，以及家庭级逐项批准或已确认的跳过逐项审批设置；
- 确认要求以及是否启用执行。

最终授权和持久化操作账本由控制台负责，因为只有控制台可对小米执行操作。Agent 可以收集意图和确认，但不能自我授权。执行前，控制台须重新解析别名、校验暴露状态和两个版本、原子认领幂等键，并只调用一次 runManualScene。传输结果含糊时应标记为 outcome_unknown，绝不自动重试。

天气、日期/时间和一般问答等通用能力不属于 mijia-web-console。控制台是家庭能力提供方；Agent 负责将其与非家庭提供方组合起来。

### 15.5 绝不能成为模型工具的 API

不要向模型暴露原始小米/设备控制路由、原始设备/spec 记录、任意 MIoT 属性写入、自动化编辑器、Token/会话 API，或直接调用 runManualScene。它们必须保留为内部实现细节，并封装在范围狭窄、经过策略校验的能力之后。

## 16. 提供方可移植性


### 16.1 家庭提供方

核心不应假定小米是唯一的家庭后端：

~~~python
class HomeCapabilityProvider(Protocol):
    async def exposed_capabilities(self, ctx: AssistantContext) -> HomeProjection: ...
    async def environment(self, ctx: AssistantContext, query: EnvironmentQuery) -> HomeEnvironment: ...
    async def device_status(self, ctx: AssistantContext, query: DeviceQuery) -> DeviceSnapshot: ...
    async def find_scenes(self, ctx: AssistantContext, query: str) -> list[SceneCandidate]: ...
    async def activate_scene(self, ctx: AssistantContext, alias: str, claim: ActionClaim) -> ActionResult: ...
~~~

初始提供方：米家控制台。未来提供方：Home Assistant Assist API 或自定义 HA 集成。这样以后可以采用 Home Assistant，而不会让对话核心依赖 HA 实体 ID 或服务调用。

### 16.2 Makers 模型配置与兼容性

模型提供方属于部署配置，而非架构决策。EdgeOne Makers 支持内置 Makers Models、托管供应商密钥和自定义 OpenAI 兼容 base URL。AI_GATEWAY_MODEL 选择确切模型；必要时由 AI_GATEWAY_BASE_URL 和 AI_GATEWAY_API_KEY 选择原生或自定义 Gateway。Makers 官方文档目前列出了 OpenAI、Anthropic、Google AI Studio、DeepSeek、MiniMax、混元、智谱和 Moonshot AI 集成。

不得根据提供方名称或一次成功的文本响应推断行为兼容性。维护确切模型白名单，并通过测试夹具和预发布冒烟测试验证：

- assistant 到 tool-call 以及 tool-result 回到 assistant 的续接；
- 稳定的工具调用 ID 和格式错误参数处理；
- 流式增量顺序以及恰好一个终止事件；
- 并行调用规范化及单写入规则实施；
- 存在时解析 prompt、completion、cached 和 total 用量；
- 所选模型/Gateway 未提供用量时，明确按 estimated=true 计费。

启动时，AI_GATEWAY_MODEL 必须匹配已验证的条目。更改它属于仅配置部署，但晋级到生产前必须重新运行模型契约测试套件。引擎消费规范化事件，不按供应商名称分支。

当前部署仍要求使用配置好的 Makers AI Gateway。可移植性并不意味着可以添加用户自带密钥、直接调用提供方作为回退，或静默故障转移。未来替换 Gateway 必须是由运营人员明确控制的适配器和配置变更，并维持相同凭据隔离、模型白名单、用量核算及契约验证。

参考：[Makers Agent quick start and model selection](https://cloud.tencent.com/document/product/1552/132786)及[Makers Models overview](https://cloud.tencent.com/document/product/1552/132748)。

### 16.3 平台与基础设施依赖

以下清单覆盖两个配套仓库。边界名称描述目标职责，不代表所述接口已经实现。每项集成都应在适配器旁记录负责人、配置、必要保证、规范化错误和替换流程。

| 依赖 | 当前 EdgeOne 集成 | 所需的可替换边界与保证 |
|---|---|---|
| HTTP 托管和路由 | 控制台 Edge Functions、Makers Agent 路由、Python Cloud Functions；文件路由和平台路径前缀处理 | 入口适配器将请求转为标准助手/工具契约。更换 HTTP 或 ASGI 主机时，必须在不改变对话逻辑的前提下保留认证、请求体限制、no-store 响应、状态/错误码和渠道行为。部署前缀和文件布局留在核心之外。 |
| 运行时配置和密钥 | Edge/Agent context.env、Node process.env、Python 环境注入、平台绑定和部署凭据 | 入口适配器构造经过校验的配置并注入依赖。共享业务模块不得假定存在 Node 全局对象、Edge 上下文或自动注入的绑定。明确规范本地、测试、预览和生产策略；保留密钥隔离及预览环境禁止模型/设备访问的规则。 |
| 模型访问 | Makers AI Gateway、模型标识符、供应商专有请求/流式/用量格式 | 模型提供方规范化消息、工具调用、流式事件、用量、时限和错误。更换 Gateway 时须保留配置的模型白名单、受限循环、凭据隔离和未知用量的显式处理；不得自动回退到其他提供方。 |
| 对话持久化 | 由平台 Blob 支撑的 Makers context.store 消息和状态 API | ConversationRepository 负责展示/模型投影、范围对话身份、排序、保留、删除和重放状态。供应商 ID 和模式属于适配器细节；删除对话绝不能删除权威操作回执。 |
| 家庭暴露持久化 | 控制台使用 @edgeone/pages-blob 保存家庭级同意和暴露记录 | 暴露仓库负责默认拒绝读取、版本、审计记录和家庭共享归属。替代实现须保留授权和数据新鲜度要求，并区分记录缺失与存储不可用；存储故障时不得意外授予访问权限。 |
| 持久操作认领 | 控制台 Blob 条件创建和强一致性读取 | 操作账本提供认领、冲突/重放查询和结果记录。替代实现必须证明跨工作进程原子认领及持久权威读取，保留请求哈希和不可变回执，并在崩溃/超时后保留未知结果。最终一致的 KV 和进程内锁无法满足此边界。 |
| 配额和临时缓存 | EdgeOne KV 绑定用于规划中的软配额存储；开发使用本地/模拟存储 | 配额策略负责预留、结算、过期和保守核算；存储适配器声明一致性和故障语义。缓存适配器声明 TTL 和新鲜度，不得成为授权或执行权威。替换基础设施不代表此前延期的配额执行已启用。 |
| Agent 生命周期和取消 | Makers 对话请求头/ID、停止/删除路由、活动运行取消及平台超时 | 生命周期适配器将应用对话 ID 和运行 ID 映射为平台句柄，传递截止时间/取消并规范化终态事件。取消不能证明已分发的物理操作被撤销；跨实例及平台更换后仍须保留重放和结果规则。 |
| 部署、服务发现和可观测性 | edgeone.json、CLI/构建产物、生成的 Python 包副本、平台来源、日志和部署环境 | 将打包、路由注册、入口控制和密钥配置放在部署适配器/手册中。注入服务位置；通过可替换遥测边界发出应用请求 ID、规范化错误、延迟和用量。新主机必须保留脱敏、追踪关联、超时预算和安全控制。 |

同一规则也适用于非 EdgeOne 依赖：Caiyun/AMap 留在天气和地点解析契约后面；小米访问留在控制台家庭能力契约后面。明确的操作授权、家庭级同意、受限模型访问和错误脱敏等产品要求，在服务替换后仍须保持稳定。

### 16.4 替换和增量实现要求

“可替换”是指必要时可以更换适配器、部署配置并执行明确的数据迁移，而无需重写对话引擎、能力策略或 Web/Siri 契约。它不保证每种后端都能零工作、零停机或仅通过配置完成迁移。

1. 更换边界前，记录现有直接平台调用及其必要语义。在替代方案就绪前保留当前正常工作的 EdgeOne 集成；只抽取受影响领域操作所需的接口。
2. 使用本地模拟实现独立于 EdgeOne 凭据和网络验证规范化契约。另行验证运行时接线和真实后端保证，包括并发认领、跨实例持久化、取消和故障行为。模拟测试不能证明这些运维属性。
3. 为对话历史、暴露版本、审计记录、操作回执以及适用时的活动配额预留定义版本化导出/导入和身份映射。保留归属、保留期、隐私投影和未解决结果。凭据应单独配置，不得嵌入迁移数据。
4. 切换和回滚期间，每个操作认领命名空间只能有一个权威写入方。切换执行器前先协调进行中或结果不确定的操作；绝不为修复迁移而重试物理写入。回滚必须使用同一权威回执或经过验证的协调结果，避免重新开放已认领操作。
5. 只有在替代方案通过领域契约和部署检查后才能晋级。若它无法提供某项必要保证，则保持相关能力禁用或继续使用现有后端；不要为了迁就新服务而静默削弱安全性或一致性。

此文档更新只确立设计约束和迁移标准，不代表立即安排平台迁移、引入回退服务或声称接口抽取已完成。未来实现任务必须指出涉及哪些已列边界，并记录剩余的直接耦合。

## 17. 安全和隐私要求

- 小米凭据和协议代码仅保留在控制台。
- 所有工具模式均须具有等价于 additionalProperties: false 的限制。
- 工具参数不得携带主体、家庭、scope、同意状态、Token、内部 URL 或提供方密钥。
- 能力可用性来自可信上下文。
- 工具只返回范围受限的类型化数据；未知字段必须通过校验失败拒绝。
- 禁止外部重定向。
- 提供方和工具都必须有明确时限。
- 默认情况下，日志不得包含凭据、绑定、Token、原始标识符、完整私人工具结果或未脱敏提示词。
- 暴露和同意变更必须可审计。
- 模型文本不能绕过执行器独立宣称操作成功。
- 将外部/天气数据中的提示注入视为不可信工具内容，而非指令。
- 若加入 MCP 工具，必须配置白名单、模式快照、风险分类、输出限制和独立凭据。

## 18. 故障处理

| 故障 | 用户结果 | 重试策略 |
|---|---|---|
| 任何写入前模型不可用 | failed，返回简短服务提示 | 客户端可使用新的轮次键重试 |
| 天气不可用 | 说明实时数据不可用 | 按提供方策略进行安全且有界的重试 |
| 一般问题时控制台不可用 | 一般回答仍然成功 | 理应没有发生控制台调用 |
| 家庭读取时控制台不可用 | 仅家庭读取失败 | 未发生写入时可安全且有界地重试 |
| 模型工具调用无效 | 拒绝，并可允许一次纠正模型迭代 | 绝不执行 |
| 写入分发前遭拒 | 明确拒绝/错误 | 条件改变后方可重试 |
| 分发后超时 | outcome_unknown | 绝不自动重试 |
| 操作成功后对话记录持久化失败 | 返回操作结果；记录存储故障 | 绝不重复操作 |
| 循环/时限耗尽 | failed 或部分且有依据的回答 | 不再调用工具 |

## 19. 可观测性

发出结构化事件：

- 轮次开始/完成/失败；
- 确定性路径命中/未命中；
- 模型迭代、延迟、Token 用量、结束原因；
- 能力已提供/选择/拒绝/完成；
- 策略决策和规则标识符；
- 操作认领/分发/结果/协调；
- 外部提供方数据新鲜度和延迟；
- 对话记录/模型历史持久化结果。

指标应包括直接回答率、工具选择准确率、澄清率、平均模型迭代次数、工具延迟、操作结果未知率、按能力/渠道分类的 Token 成本、一般问题的控制台规避率和脱敏故障率。

绝不能用“HTTP 200”作为物理操作成功的证据。

## 20. 测试策略

### 20.1 核心行为

- 米家控制台离线时，一般问题仍能成功回答。
- 当前信息问题绝不返回编造的实时数据。
- 缺少天气地点时会请求澄清。
- 提供明确地点时返回有依据的天气回答。
- 家庭问题只调用家庭读取工具。
- 工具结果在循环限制内交还模型。
- 预算内允许多次读取。
- 拒绝每轮超过一次写入。

### 20.2 安全性

- 否定、引用、假设和未来意图绝不触发操作。
- 模型编造的工具、字段和别名均安全失败。
- 未暴露设备/场景无法读取或操作。
- 真实 ID 和凭据不得进入模型请求、响应、日志、记忆或客户端载荷。
- 跨对话/工作进程/重启的操作重放只执行一次。
- 场景动作变更会使逐项批准失效；仅重命名不会。已确认的家庭级跳过逐项审批仍适用于当前版本。
- 分发后的取消和超时保持为未知，而非失败或重试。

### 20.3 提供方契约

- 录制模拟天气提供方契约测试。
- 米家控制台模式测试覆盖所有成功、部分、空、过期和错误响应。
- 模型提供方测试夹具覆盖文本、工具调用、格式错误调用、并行调用、工具结果续接及用量核算。
- EdgeOne 适配器测试覆盖认证、停止、删除、历史脱敏和重放。

### 20.4 评估集

维护版本化中英文评估语料，包含：

- 一般问题；
- 实时信息问题；
- 家庭读取；
- 精确和含糊的场景请求；
- 追问和代词；
- 注入尝试；
- 隐私泄露探测；
- 不支持的敏感操作。

每次变更模型白名单时运行此评估集。

## 21. 交付计划

### Phase 0 — 架构验证

- 实现与提供方无关的消息及工具调用规范化。
- 使用模拟 get_weather 能力验证两步工具循环。
- 验证一般问题无需调用家庭提供方即可完成。
- 将 mijia-agent-local-prod 改造为标准 Phase 0 验证工具：保留安全/生命周期外壳，替换旧 /ai/command 协议，并增加 fake 和 live-read 配置。
- 运行 §14.2 的 CLI 验收矩阵；保持物理写入禁用，并保证 fake 配置不需要小米或生产凭据。
- 定义新响应契约和 EdgeOne 运行时限制。
- 从多个已部署工作进程并发测试 EdgeOne Blob onlyIfNew 认领和强一致性读取。
- 针对初始 AI_GATEWAY_MODEL 运行工具/流式/用量契约测试套件。
- 将旧路由器标为弃用，通过显式开关控制访问并增加流量遥测；冻结其功能开发。

**退出条件：** 单元测试证明直接回答、澄清、读取工具和终态写入流程；改造后的 CLI 在物理执行禁用时通过 §14.2 的 fake 和 live-read 验收。

### Phase 1 — 通用助手 MVP

- 标准助手接口。
- 通用助手提示词。
- Caiyun Weather v2.6 和 AMap 地理编码适配器、归属声明、部署路径延迟探测、缓存及城市/区域处理。
- 日期/时间工具。
- 基于 Makers context.store 的 ConversationRepository，分别保存展示投影和脱敏模型投影。
- LLM 前的精确命令确定性快速路径，并使用相同能力策略。
- Web UI 集成；不执行物理写入。

**退出条件：** 一般问题和天气查询在生产环境可用，并具备成本/延迟遥测。

### Phase 2 — 家庭观察

- 在通用 ResolvedHomeContext 之上增加控制台版本化 capabilities 和 tools:invoke 接口。
- 增加默认拒绝的助手暴露记录和管理界面。
- 通过经校验、减少披露的过滤器适配现有环境及设备状态采集器。
- 在 Agent 中实现延迟家庭能力发现，并与 Agent 自有模式取交集。
- 提供结构化 UI 卡片和简短 Siri 渲染。
- 使用绑定受众的自动化 Token 实现 Siri 直连 Agent 的认证。

**退出条件：** 家庭读取有数据依据、仅限已暴露内容，并能在两个渠道工作。

### Phase 3 — 安全场景操作

- 扩展场景发现，加入暴露状态、规范化操作摘要和版本哈希。
- 策略引擎。
- 由控制台负责的 EdgeOne Blob 操作账本，使用现有幂等键、不可变记录、onlyIfNew 和强一致性读取。
- 绑定版本的逐项批准、须确认的家庭级跳过逐项审批开关，以及显式执行门控。
- 最后执行一次选定真实场景的端到端验证。

实现进度（2026-09-25）：两个配套仓库已实现场景操作的家庭级同意、规范化场景摘要、绑定版本的逐项批准、须确认的家庭级跳过逐项审批开关，以及控制台 Blob 认领/结果账本。该开关覆盖当前家庭现有及未来新增的已启用手动场景；场景发现和授权不再受静态低风险分类限制。批准版本包含私有目标与动作信息，但不会向模型暴露；仅修改显示名称不会使批准失效。废弃的命令路由和自动化令牌工具入口目前均不可执行物理写入。canonical assistant 的动作 scope 注册和当前意图校验，须等 [docs/TODO.md](./TODO.md) 中的部署门禁通过后再完成。在此之前必须保持 AI_SCENE_EXECUTION_ENABLED 未设置。

**退出条件：** 精确一次认领语义、对用户可见的未知结果，并且不盲目重试。

### Phase 4 — 运维加固

- 配额结算和渠道预算。
- 在支持时提供文本流式输出。
- 完整评估工具、仪表盘、保留策略和协调操作。
- 完成门槛和观察窗口通过后，移除旧路由器模块、路由、模式、配置、测试及部署接线。

### Phase 5 — 扩展性和记忆

- 基于同意的偏好和摘要。
- 受限的提供方/插件框架。
- 评估 MCP 客户端支持。
- 评估可选 Home Assistant 提供方。
- 只有在持久调度器和同意设计完成后才加入提醒。

## 22. 架构决策

1. **通用助手，而非场景路由器。** 场景激活只是一项能力。
2. **干净核心、精简适配器。** Web 和 Siri 不应各自拥有决策引擎。
3. **受限工具循环。** 工具结果返回模型；物理写入始终是终态。
4. **动态能力注册表。** 根据可信的逐请求上下文选择工具。
5. **选择性数据暴露。** 仅向助手暴露最低限度的家庭数据和操作。
6. **确定性执行器。** 模型提出请求；服务端策略授权；控制台执行。
7. **延迟家庭访问。** 一般问题不依赖小米服务可用性。
8. **分离展示记录与模型历史。** 有用的回答不必将私人状态泄露到未来提示词。
9. **优先采用原生提供方。** 天气和米家是类型化原生能力；MCP 用于未来扩展。
10. **不设迁移约束。** 只在现有文件符合新架构时复用。
11. **控制台持有家庭权限。** 小米身份、暴露、当前成员资格、场景版本和最终执行均留在 mijia-web-console。
12. **基于已知模式协商能力。** 控制台通告可用性；Agent 仅暴露本地已知、版本化的工具模式。
13. **优先 Caiyun。** 中国大陆 Phase 1 MVP 使用 Caiyun Weather v2.6；预警策略完成前明确不支持预警。
14. **基于 Blob 的操作认领。** 使用 EdgeOne Blob 条件创建和强读取执行现有幂等键；KV 不具权威性。
15. **Makers 存储置于自有接口之后。** 通过 ConversationRepository 使用 context.store，分别保存展示和脱敏模型投影。
16. **Phase 1 加入快速路径。** 第一版通用助手即交付精确的确定性命令，并使用相同工具策略。
17. **家庭级暴露。** 所有已授权成员共享由控制台管理的一份家庭暴露配置。
18. **Siri 直连入口。** Siri 使用绑定受众的自动化 Token 直接调用标准 Agent。
19. **通过验证配置选择模型。** AI_GATEWAY_MODEL 选择模型，但只有通过契约测试的确切模型才能进入生产白名单。
20. **旧路由器只是临时方案。** 立即弃用并冻结；标准助手通过完成门槛和生产观察窗口后再删除。
21. **复用本地 CLI 外壳，不保留其旧路由契约。** mijia-agent-local-prod 作为 Phase 0 运维工具，在改为标准引擎后继续使用；旧 /ai/command 行为仅允许通过显式弃用模式访问，绝不能作为回退路径。
22. **先用 EdgeOne，依赖须可替换。** 当前交付可直接使用 EdgeOne，同时所有平台依赖都保留明确的适配器边界和 §16.3–16.4 所述替换标准。替换服务时须保留领域契约和安全保证，且不重写助手核心。

## 23. 已确定的实现决策

| 主题 | 决策 |
|---|---|
| 平台可移植性 | EdgeOne 是首个实现；运行时、配置、模型、存储、配额、生命周期、部署和遥测均须保留 §16.3 所述可替换边界；可增量抽取 |
| 天气 | AMap 在服务端解析中国城市/区域名称；Caiyun Weather v2.6 获取天气；明确不支持预警 |
| 操作身份 | 保留现有幂等键，并将其绑定到规范请求哈希 |
| 操作存储 | 在 mijia-web-console 使用 EdgeOne Blob；通过 onlyIfNew 原子认领、强读取和不可变生命周期记录；KV 永不具权威性 |
| 对话存储 | Makers context.store 置于 ConversationRepository 后；用通用消息用于展示，state 保存脱敏模型历史 |
| 确定性快速路径 | Phase 1 交付 |
| 暴露 UI | mijia-web-console 的“家庭 → AI 助手访问”；米家为初始来源，未来可添加 Home Assistant 提供方 |
| Siri | 通过绑定受众的自动化 Token 直连标准 Agent |
| 模型 | 由 AI_GATEWAY_MODEL 选择确切模型；提供方无关适配器加上由契约测试支持的生产白名单 |
| 暴露归属 | 每个家庭共用一份配置，适用于当前所有已授权成员 |
| 旧路由器 | 立即弃用、不加新功能、只留显式临时开关；核心项目完成时删除 |
| Phase 0 本地 CLI | 复用 mijia-agent-local-prod 的安全和进程外壳，改为面向标准助手，并隔离 fake 与生产 live-read 配置；不得以此证明已部署的 EdgeOne 持久化或操作语义 |

剩余工作是验证而非产品选择：从已部署 EdgeOne 路径测量 Caiyun 延迟，在并发下验证 Blob 冲突行为，并通过工具/流式/用量契约测试认证初始选定模型。

## 24. 第一批实现事项

1. 将旧路由器标记为弃用，添加临时部署开关和流量指标，并记录功能冻结要求。
2. 引入 mijia_assistant.conversation 和与提供方无关的事件模型。
3. 实现 Capability、CapabilityRegistry 和 CapabilityResult。
4. 使用模拟提供方/工具测试数据实现最多四轮迭代的引擎。
5. 将路由器提示词替换为通用助手契约。
6. 将 mijia-agent-local-prod 适配到标准接口和事件契约，增加隔离的 fake 和 live-read 配置，并实现 §14.2 验收矩阵。
7. 添加 get_current_datetime 和 Caiyun Weather v2.6 提供方，支持 Token 配置、缓存、归属声明和延迟遥测。
8. 添加标准响应结果及渠道渲染器。
9. 实现 ConversationRepository 及其 Makers context.store 适配器，分别维护展示/模型投影。
10. 通过同一能力注册表和策略检查实现 Phase 1 确定性识别器。
11. 定义版本化控制台 capabilities 和 tools:invoke 契约，以及 ResolvedHomeContext。
12. 在 mijia-web-console 增加家庭级 AI Assistant Access 模式和 UI。
13. 为现有控制台环境和设备采集器套上考虑暴露范围的过滤操作。
14. 构建不受旧兼容约束的 Agent 接口、EdgeOne 适配器调用和 Siri Token 直连接入。
15. 在启用场景执行前实现并发测试 EdgeOne Blob 账本适配器。
16. 添加精确模型契约测试套件，并在生产启动时校验 AI_GATEWAY_MODEL 白名单。
17. 移植凭据隔离、严格模式、重定向和禁止重试的安全测试。
18. 增加验收测试，证明“今天天气怎么样”会询问地点，而“今天新加坡天气怎么样”会查询实时数据。
19. 文档化完成门槛通过后，移除弃用路由器栈。

---

本文档取代面向未来开发的场景路由器方向。现有迁移和 steward 对齐文档仍是历史依据，直到它们被归档或重写。
