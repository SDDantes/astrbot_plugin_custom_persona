# astrbot_plugin_custom_persona

AstrBot 插件 —— 完全由 YAML Persona 驱动的 LLM 请求定制系统。

> **⚠️ 注意：当前项目处于个人测试阶段，没有进行过完善测试，不保证任何稳定性。在生产环境使用请自行评估风险。**

---

## 项目定位

本插件允许用户通过编写 YAML 格式的"人格文件"（Persona），完全控制发送给 LLM 的系统提示词（system prompt）与上下文（context）。在 AstrBot 原生的 `provider.system_prompt` 和 `provider.personality` 之上，提供更细粒度的控制能力：

- **多段 Preamble**：按顺序排列的 SYSTEM / USER / ASSISTANT 角色片段，支持 Jinja2 模板语法与条件渲染。
- **双层上下文管理（L1 + L2）**：L1 为可持久化的对话历史摘要（由插件账本维护），L2 为内存中的最近消息（含工具调用 / 结果）。
- **响应后处理**：NO_RESPONSE 静默拦截、分段回复、T2I / TTS 逐段触发。

简言之，它让你**用 YAML 描述一个 AI 助手的行为规则、对话风格、工具使用策略和输出格式**，而无需在 AstrBot 配置中做任何代码级修改。

---

## 核心功能

- **YAML Persona 文件**：插件从 `personas/` 目录加载人格定义，支持多个人格共存。
- **Persona 选择策略**：按优先级（会话绑定 → 全局默认 → AstrBot 原生行为）自动选择。
- **L1 历史（对话账本）**：插件自有 SQLite 账本记录所有会话消息，供 Persona 模板中的 `{{chat_history}}` 渲染。
- **L2 上下文**：内存中的最近消息窗口，含工具调用结果，支持滑窗和后台 LLM 压缩。
- **模型路由**：`default` 模式（使用原模型）与 `simple` 模式（替换为指定模型 ID）。
- **NO_RESPONSE 拦截**：LLM 输出特定标记时静默丢弃，不发送任何回复。
- **分段回复**：将 LLM 的一条长回复按 `segment_mark` 分割为多条消息，逐条发送。
- **逐段 T2I / TTS**：支持以 `✺T2I✺` / `✺TTS✺` 前缀触发文生图 / 语音合成。
- **L2 压缩**：当 L2 上下文 token 数超过上下文窗口 78% 时，后台自动调用 LLM 进行摘要压缩。
- **Web 管理页面**：可视化新建、重命名、删除、编辑、预览 Persona YAML（含语法高亮）。

---

## 配置说明

以下为插件 `_conf_schema.json` 中所有可配置参数及其含义。

### 基本设置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `true` | 插件总开关。关闭后 LLM 请求不被修改，响应也不被后处理。 |
| `timezone` | string | `"Asia/Tokyo"` | 系统时间时区，用于 `{{system_time}}` 模板变量。 |
| `personas_dir` | string | `""` | Persona YAML 文件目录。留空则使用插件 data 目录下的 `personas/`。 |
| `extra_prompt_filename` | string | `"EXTRA_PROMPT.md"` | 额外提示词文件名，在当前会话工作区中查找，注入为 `{{extra_prompt}}`。 |

### 模型路由（routing）

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `routing.mode` | string | `"default"` | 路由模式。`default`：不修改模型；`simple`：使用下方指定的模型 ID。 |
| `routing.simple_model_id` | string | `""` | Simple 模式下的模型 ID。需使用当前 LLM 提供方可识别的模型名。 |

### 默认 L2 对话窗口（dialogue_window）

Persona 未显式设置 `dialogue_window` 时使用此默认值。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `dialogue_window.max_messages` | int | `100` | 触发滑动的消息数（M1）。消息数达到此值时会裁减。 |
| `dialogue_window.keep_messages` | int | `60` | 滑动后保留的消息数（M2）。必须 ≤ max_messages。 |

### NO_RESPONSE 拦截（no_response）

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `no_response.enabled` | bool | `true` | 是否启用静默拦截。仅对**非流式**请求生效。 |
| `no_response.mark` | string | `"✺✺✺NO_RESPONSE✺✺✺"` | 默认静默标记。LLM 仅输出此字符串时，回复被丢弃。可被 Persona 级别的 `no_response_mark` 覆写。 |

### 对话账本（ledger）

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `ledger.per_chat_limit` | int | `1000` | 每个会话在 SQLite 账本中保留的最大消息数，超出时自动删旧。 |

---

## Persona YAML 参数说明

每个 Persona 文件是一个 YAML 文档，以下为完整可配置字段及其含义。

### 基本信息

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `name` | string | **是** | 内部标识名，仅允许字母、数字、`.`、`_`、`-`。 |
| `display_name` | string | 否 | 显示名称，在管理页面中展示。 |
| `description` | string | 否 | 人格描述文本。 |

### 激活配置（activation）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `activation.global_default` | bool | `false` | 是否作为全局默认人格。**多个 Persona 同时设置时行为不确定，请只设一个。** |
| `activation.session_bindings` | list | `[]` | 会话绑定列表，**优先级最高**。格式：`- session_id: "aiocqhttp:GroupMessage:123456"` |

### 对话历史（chat_history）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `chat_history.max_turns` | int | `30` | 最大保留轮次（一轮 = user + assistant）。 |
| `chat_history.format_template` | string | `"[{sender_name}/{timestamp}]: {content}"` | 单条历史的格式化模板，支持 `{sender_name}`、`{timestamp}`、`{content}`、`{role}`。 |
| `chat_history.preset_dialogs` | string | `""` | 无历史记录时使用的预设对话文本。 |
| `chat_history.max_tokens` | int | `8000` | 历史文本的最大 token 数（按 4 字符 ≈ 1 token 估算），超出时截断。 |

### 对话窗口（dialogue_window）—— 可选

若设置此项，则**覆盖**全局 `dialogue_window` 配置。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `dialogue_window.max_messages` | int | `100` | Persona 级别的 M1。 |
| `dialogue_window.keep_messages` | int | `60` | Persona 级别的 M2。 |

### 分段回复（segmented_reply）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `segmented_reply.enabled` | bool | `false` | 是否启用分段回复。启用后 LLM 输出中的 `segment_mark` 会被解析为分隔符。 |
| `segmented_reply.segment_mark` | string | `"✺SEG✺"` | 分段标记。在模板中可用 `{{segment_mark}}` 告知 LLM。 |
| `segmented_reply.interval_min` | float | `1.5` | 段间最小延迟（秒）。 |
| `segmented_reply.interval_max` | float | `3.5` | 段间最大延迟（秒），实际值在此区间随机。 |
| `segmented_reply.t2i_trigger` | string | `"✺T2I✺"` | T2I 触发前缀。段首含此前缀时，将该段转为图片发送。 |
| `segmented_reply.tts_trigger` | string | `"✺TTS✺"` | TTS 触发前缀。段首含此前缀时，将该段转为语音发送。 |
| `segmented_reply.t2i_template` | string | `""` | T2I 渲染模板名。留空使用 AstrBot 全局配置的 `t2i_active_template`。 |
| `segmented_reply.t2i_dual_output` | bool | `false` | T2I 段是否同时发送图片和文本。 |
| `segmented_reply.tts_dual_output` | bool | `false` | TTS 段是否同时发送语音和文本。 |

### 压缩（compression）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `compression.assistant_template` | string | （见源码） | 压缩后插入的 assistant 消息模板。 |
| `compression.user_template` | string | `"Compressed conversation summary:\n{{ summary }}"` | 压缩后插入的 user 消息模板，`{{ summary }}` 为摘要内容。 |
| `compression.custom_instructions` | string | `""` | 自定义摘要指令。为空时使用插件内置的 `COMPRESSION_PROMPT`。 |

### 其他

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `no_response_mark` | string | `"✺✺✺NO_RESPONSE✺✺✺"` | Persona 级别的静默标记，优先级高于全局配置。 |
| `tool_call_prompt` | string | `""` | 工具调用规则。为空时使用 AstrBot 内置的默认工具调用提示。 |
| `live_mode_prompt` | string | `""` | 实时对话模式的额外指令。仅在 AstrBot 直播模式触发时注入。为空时使用插件内置默认值。 |
| `skill_whitelist` | list/null | `null` | 技能白名单。`null` 表示不限制；空列表 `[]` 表示禁用全部技能；否则仅列出指定的技能名。 |

### 片段配置（segments）

每个 segment 定义一个 Preamble 片段：

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `id` | string | 否 | 片段标识。 |
| `role` | string | 否 | `SYSTEM` / `USER` / `ASSISTANT`。`SYSTEM` 片段合并为 system_prompt；其他角色注入到 contexts。默认 `USER`。 |
| `depth` | int | 否 | 排序深度（越小越靠前）。 |
| `condition` | string | 否 | Jinja2 条件表达式。为空表示始终渲染。可用条件变量见下文。 |
| `template` | string | 否 | Jinja2 模板内容。可用模板变量见下文。 |

---

## 模板变量

所有 Persona 片段（`segments[].template`）中均可使用以下 Jinja2 变量：

### 通用变量

| 变量 | 来源 | 说明 |
|------|------|------|
| `{{chat_history}}` | ConversationLedger | L1 格式化历史（不含工具调用回合） |
| `{{system_time}}` | 配置时区 | 当前日期/时间 |
| `{{segment_mark}}` | 当前 Persona 配置 | 分段标记 |
| `{{t2i_trigger}}` | 当前 Persona 配置 | T2I 触发前缀 |
| `{{tts_trigger}}` | 当前 Persona 配置 | TTS 触发前缀 |
| `{{skill_list}}` | SkillManager（会话过滤） | 可用技能列表，已格式化 |
| `{{tool_list}}` | `ProviderRequest.func_tool` | 可用工具及描述 |
| `{{tool_schema_mode}}` | AstrBot 配置 | 工具 schema 模式（`full` / `skills_like`） |
| `{{tool_call_prompt}}` | Persona 配置 | 工具调用规则文本 |
| `{{live_mode_prompt}}` | Persona 配置 + 事件 | 直播模式指令（非直播时为空） |
| `{{extra_prompt}}` | 工作区 `EXTRA_PROMPT.md` | 额外提示词内容 |
| `{{extra_prompt_path}}` | 工作区 | `EXTRA_PROMPT.md` 的绝对路径 |
| `{{user_id}}` | 事件 | 当前用户 ID |
| `{{user_nickname}}` | 事件 | 当前用户昵称 |
| `{{group_name}}` | 事件 | 群名称（群聊时） |
| `{{platform_name}}` | 事件 | 平台适配器名称 |
| `{{message_type}}` | 事件 | `GROUP_MESSAGE` / `FRIEND_MESSAGE` / `OTHER_MESSAGE` |
| `{{session_id}}` | `event.unified_msg_origin` | 唯一会话标识 |
| `{{no_response_mark}}` | 当前 Persona 配置 | NO_RESPONSE 标记 |
| `{{is_admin}}` | 事件 | 当前用户是否为管理员 |
| `{{admin_ids}}` | AstrBot 配置 | 管理员 ID 列表 |

### 条件变量（用于 `condition` 字段）

| 变量 | 说明 |
|------|------|
| `streaming` | 当前请求是否使用流式 |
| `is_group` | 是否为群聊消息 |
| `is_private` | 是否为私聊消息 |
| `has_images` | 消息是否包含图片 |
| `tool_list` | 是否有可用工具（非空为真） |
| `t2i_enabled` | T2I 功能是否已启用 |
| `tts_enabled` | TTS 功能是否已启用 |
| `live_mode_prompt` | 是否为直播模式 |
| `extra_prompt` | 是否有额外提示词 |

条件语法使用 Jinja2 表达式，支持 `and`、`or`、`not`。也可以使用 `!` 前缀（插件会自动转换为 `not`）。

---

## 与 AstrBot 原生功能的冲突说明

> **理解以下冲突至关重要，否则可能导致预期外的行为。**

1. **系统提示词完全接管**：插件在 `on_llm_request` 中将 `req.system_prompt` 和 `req.contexts` 完全覆写。这意味着 AstrBot 原生配置中的 `provider.system_prompt`、`provider.personality` **不会生效**。你需要在 Persona YAML 中手动重建所有需要的系统指令。

2. **`req.conversation = None`**：插件将请求的 `conversation` 设为 `None`，阻止 AstrBot 将渲染后的 Preamble 和工具调用消息写入持久化历史。响应后，插件通过 `ConversationManager.add_message_pair()` 自行保存纯净的 user/assistant 消息对。**这会导致 AstrBot 原生的对话记忆机制（LTM）对此插件管理的会话不再生效。**

3. **工具调用提示词**：如果 Persona 中设置了 `tool_call_prompt`，则会覆盖 AstrBot 内置的 `TOOL_CALL_PROMPT`。如果没设置，插件会根据 `tool_schema_mode` 自动选择 AstrBot 的原生提示。

4. **技能列表**：`{{skill_list}}` 使用的是 AstrBot `SkillManager` 提供的技能，但会经过会话级插件过滤和 Persona 的 `skill_whitelist` 二次过滤。如果你在 Persona 中没有包含技能相关片段，LLM 将不知道有哪些可用技能。

5. **直播模式**：`live_mode_prompt` 仅在 AstrBot 直播模式（`action_type == "live"`）触发时注入。普通对话中该变量为空字符串，对应的条件块不会被渲染。

6. **`/reset` 与 `/new` 命令**：插件会检测 AstrBot 内置的上下文重置命令，并同步清除自身会话状态和账本记录。但你需要在 Persona 的 `preset_dialogs` 中定义无历史时的引导语，否则 L1 历史将为空。

7. **群聊上下文注入**：插件将 ConversationLedger 中的最近用户消息（含发送者名和时间戳）注入到 user prompt 前面。这可能导致 LLM 看到比原生 AstrBot 更多的会话上下文，也意味着每条消息之间没有"遗忘"。

---

## 使用注意事项

1. **Persona 文件命名**：文件名只能包含字母、数字、`.`、`_`、`-`。建议与 `name` 字段保持一致。

2. **YAML 语法**：Persona 文件使用严格的 YAML 语法。多行模板请使用 `|`（保留换行）或 `>`（折叠换行）块标量。缩进必须使用空格（不能使用 Tab）。

3. **全局默认唯一性**：请确保只有一个 Persona 设置 `global_default: true`。多个全局默认时的选择顺序取决于文件读取顺序，不具有确定性。

4. **模板性能**：Preamble 渲染在每次 LLM 请求时执行。模板中避免复杂计算（Jinja2 本身不支持复杂逻辑），只做简单的变量替换和条件判断。

5. **L2 压缩**：压缩功能依赖当前会话的 LLM 提供方。如果会话没有可用的 LLM 提供方，压缩会静默跳过。压缩阈值固定为上下文窗口的 78%，不可配置。

6. **流式与 NO_RESPONSE**：NO_RESPONSE 拦截**仅对非流式请求生效**。流式请求中 LLM 逐 token 输出，无法在完成前判断完整文本是否匹配静默标记。

7. **分段回复的限制**：分段回复功能**仅在结果链全部为 Plain 组件时生效**。如果 AstrBot 其他插件在结果中插入了图片、语音等非纯文本组件，分段会被跳过。

8. **首次使用**：插件首次加载时会将 `personas/` 目录中的内置文件复制到插件 data 目录。如果 data 目录已存在 YAML 文件则不会覆盖。

---

## 快速开始

1. 将本插件放入 AstrBot 的插件目录，重启 AstrBot。
2. 打开 WebUI → 插件管理 → 自定义人格系统 → Personas 管理页面。
3. 新建或编辑一个 Persona YAML 文件，设置 `activation.global_default: true`。
4. 使用预览功能查看渲染效果，确认无误后保存。
5. 发送一条消息测试，观察 LLM 行为是否符合预期。

---

## 项目结构

```
astrbot_plugin_custom_persona/
├── main.py                  # 插件入口，协调各模块
├── metadata.yaml            # 插件元数据
├── _conf_schema.json        # 配置 schema
├── core/
│   ├── models.py            # 所有数据模型（PersonaConfig 等）
│   ├── persona_store.py     # Persona 文件的加载、增删改查
│   ├── renderer.py          # Jinja2 Preamble 渲染器
│   ├── response.py          # 响应后处理（NO_RESPONSE、分段、L2）
│   ├── compression.py       # LLM 上下文压缩（三级降级）
│   ├── history.py           # 历史文本格式化、去重、截断
│   ├── ledger.py            # SQLite 对话账本
│   ├── state.py             # 会话状态管理（含 TTL 淘汰）
│   ├── message_utils.py     # 消息序列化辅助
│   ├── template_vars.py     # 模板变量构建
│   ├── web_api.py           # Web API 控制器
│   ├── retry.py             # 异步重试（指数退避 + 抖动）
│   └── __init__.py
├── personas/
│   └── default.yaml         # 内置默认 Persona
├── pages/personas/          # Web 管理页面
│   ├── index.html
│   ├── app.js
│   └── style.css
├── scripts/
│   └── smoke_test.py        # 基础冒烟测试
└── docs/
    └── development.md       # 开发指南
```
