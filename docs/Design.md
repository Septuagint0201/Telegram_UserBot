# Telegram Personal AI Digital Twin
## Telegram 真人账号 AI 分身系统 — 项目总体设计

## 1. 项目定位

本项目的目标是在 Telegram 上运行一个长期存在的个人 AI 分身。

系统直接控制一个真实 Telegram 用户账号，使该账号能够同时接受 AI 和真人本人控制，并保持统一的身份、统一的聊天历史、统一的人格和长期记忆。

最终表现为：

- 对方始终是在和同一个 Telegram 真人账号聊天。
- 普通情况下由 AI 自动处理消息。
- 真人本人可以随时直接进入 Telegram 手动回复。
- 真人发送出去的内容自动进入 AI 上下文，并被视作该 AI 自己过去说过的话。
- 可以通过独立 Telegram 控制 Bot 切换 AI / HUMAN 等运行模式。
- AI 拥有长期记忆，并持续对记忆进行整理、压缩、更新和遗忘。
- AI 能理解时间流逝、事件、承诺、人与人之间多久没有联系等时间因素。
- AI 可以在适当情况下主动向联系人发送消息。
- AI 的语言风格、关系判断、主动程度和过去行为长期保持连续。

项目最终目标不是传统客服机器人，而是一个长期运行的：

**Personal AI Agent / Digital Twin / AI Persona**

---

# 2. 总体架构

核心结构：

```text
                       Telegram
                           |
             +-------------+-------------+
             |                           |
      真人用户账号                    Control Bot
      Telegram User                  Telegram Bot
             |                           |
          MTProto                     Bot API
             |                           |
         Telethon                       |
             |                           |
             +-------------+-------------+
                           |
                    Backend Core
                           |
        +------------------+------------------+
        |                  |                  |
 Conversation Engine   Memory Agent    Proactive Agent
        |              GPT 5.4 nano    DeepSeek v4 Flash
        |                  |                  |
        |             Memory System      Time/Event System
        |                  |                  |
        +------------------+------------------+
                           |
                    Context Builder
                           |
                    Main AI Agent
                    GPT 5.6 Luna
                           |
                    Response Engine
                           |
                     Telegram Send
```

整个系统分为几个彼此独立但协同工作的部分：

1. Telegram 真人账号层
2. Telegram 控制 Bot
3. Conversation Engine
4. Main AI Agent
5. Memory Agent
6. Memory System
7. Time / Proactive Agent
8. Scheduler
9. Context Builder
10. 数据库
11. Vector Memory
12. Cache
13. Human Override 系统

---

# 3. Telegram 真人账号

系统的主要 Telegram 身份是一个真实用户账号，而不是 Bot 账号。

实现方式：

```text
Telegram User Account
        |
      MTProto
        |
     Telethon
        |
     Backend
```

账号通过 Telegram MTProto 登录。

首次登录需要：

- Telegram API ID
- Telegram API Hash
- 手机号
- Telegram 登录验证码
- 如果开启，则需要 2FA 密码

成功后保存 Telethon Session。

之后 Ubuntu 服务器可以长期维持该 Telegram 用户会话。

这个账号既可以：

- 被后端程序控制
- 被 AI 自动回复
- 被真人从 Telegram 手机客户端直接操作
- 被真人从 Telegram Desktop 操作

因此 AI 和真人共享完全相同的 Telegram 身份。

---

# 4. 人机共用账号

这是系统最核心的设计原则之一。

无论消息实际来自：

```text
AI
真人
主动 AI
```

Telegram 对外显示的始终都是：

```text
同一个 Telegram User Account
```

系统内部才区分消息实际来源。

例如：

```text
Telegram 对方看到：

Alice:
好的，我晚上看看。
```

数据库可能记录：

```json
{
  "role": "assistant",
  "source": "human",
  "content": "好的，我晚上看看。"
}
```

或者：

```json
{
  "role": "assistant",
  "source": "ai",
  "content": "好的，我晚上看看。"
}
```

对 LLM 来说，两者原则上都会表现为：

```json
{
  "role": "assistant",
  "content": "好的，我晚上看看。"
}
```

这样可以保持身份连续性。

---

# 5. 真人消息回灌 AI 上下文

这是本项目的重要特性。

真人本人通过 Telegram App 直接发送出去的消息，Telethon 后端也需要捕获。

例如历史：

```text
对方：
最近项目怎么样？

AI：
还在继续推进。

真人随后手动发送：
今天刚把核心功能跑通。
```

数据库记录：

```text
user:
最近项目怎么样？

assistant / source=ai:
还在继续推进。

assistant / source=human:
今天刚把核心功能跑通。
```

下一次对方问：

```text
那下一步准备做什么？
```

发送给主模型的上下文为：

```json
[
  {
    "role": "user",
    "content": "最近项目怎么样？"
  },
  {
    "role": "assistant",
    "content": "还在继续推进。"
  },
  {
    "role": "assistant",
    "content": "今天刚把核心功能跑通。"
  },
  {
    "role": "user",
    "content": "那下一步准备做什么？"
  }
]
```

因此 AI 会认为：

> “今天刚把核心功能跑通”是自己以前说过的话。

这保证真人和 AI 共同塑造同一个长期人格。

---

# 6. 消息来源模型

数据库内部建议明确保存：

```text
role
source
```

其中：

```text
role:
user
assistant
system
```

`source` 可以包括：

```text
telegram_user
ai
human
proactive_ai
control
system
```

典型记录：

```text
role=user
source=telegram_user
```

代表联系人发来的消息。

```text
role=assistant
source=ai
```

代表正常 AI 自动回复。

```text
role=assistant
source=human
```

代表真人本人手动回复。

```text
role=assistant
source=proactive_ai
```

代表 AI 主动发出的消息。

---

# 7. Conversation Engine

Conversation Engine 是实时消息系统的中心。

职责包括：

- 接收 Telegram 消息
- 识别会话
- 判断联系人
- 判断当前工作模式
- 保存原始消息
- 获取最近聊天历史
- 请求 Memory System
- 构造主模型 Context
- 调用 Main AI
- 将结果发送到 Telegram
- 记录最终回复

核心流程：

```text
Telegram Message
        |
 Conversation Engine
        |
    Save Message
        |
  Check Chat Mode
        |
  Build Context
        |
  Main AI Agent
        |
   Send Message
        |
    Save Reply
```

---

# 8. 会话模式

建议至少提供三种主要模式。

## AUTO

AI 全自动处理。

```text
对方消息
   |
Main AI
   |
自动发送
```

适合普通聊天。

---

## HUMAN

真人接管。

AI 不自动发送回复。

```text
对方消息
   |
保存
   |
通知真人
   |
真人 Telegram 回复
```

真人发送的内容仍然进入 AI 历史。

---

## COPILOT

AI 只生成建议回复，不直接发送。

例如：

```text
对方：
晚上有空吗？

AI Draft：
应该有，怎么了？
```

真人可以：

- 直接发送
- 修改
- 忽略

真人最终发出去的文本进入 `assistant` 历史。

这个模式非常适合训练 AI 学习真实表达风格。

---

# 9. Telegram Control Bot

另外建立一个独立 Telegram Bot：

```text
@xxx_control_bot
```

它不承担聊天。

它只承担系统控制。

例如命令：

```text
/ai
/human
/copilot
```

按联系人：

```text
/ai 123456789
/human 123456789
/copilot 123456789
```

其他建议命令：

```text
/status
```

查看当前模式。

```text
/memory
```

查看该联系人的长期记忆摘要。

```text
/context
```

查看当前会送给主模型的上下文。

```text
/pause
```

暂停 AI。

```text
/resume
```

重新运行。

```text
/forget
```

删除特定记忆。

```text
/proactive off
```

关闭某联系人主动消息。

```text
/proactive on
```

启用。

Control Bot 必须设置管理员白名单。

例如：

```text
allowed_admin_ids
```

只有指定 Telegram User ID 可以控制服务器。

---

# 10. Main AI Agent

主模型：

**GPT 5.6 Luna**

Main AI 只负责高价值认知任务：

- 最终聊天回复
- 人格表达
- 复杂推理
- 语气和情绪
- 根据关系决定表达方式
- 消化长期记忆
- 使用相关历史
- 对主动消息进行最终生成
- 必要时对主动行为进行最终检查

Main AI 不承担高频数据库整理工作。

---

# 11. Main AI Context

每次调用主模型之前，通过 Context Builder 构造：

```text
Identity
+
Personality
+
Relationship
+
Relevant Long-term Memory
+
Conversation Summary
+
Recent Raw Messages
+
Current Time Context
+
Current User Message
```

示例：

```text
SYSTEM

你是 Alice 的数字分身。

[IDENTITY]
...

[PERSONALITY]
...

[RELATIONSHIP]
当前联系人 Bob：
认识 5 年
关系：好友
交流风格：随意

[RELEVANT MEMORY]
Bob 最近换工作。
Bob 上周提到项目延期。

[RECENT CONTEXT]
...

[CURRENT TIME]
Saturday 21:30

[USER]
最近怎么样？
```

这样主模型接收到的是整理后的高价值信息，而不是整个历史数据库。

---

# 12. Memory Agent

记忆模型：

**GPT 5.4 nano**

Memory Agent 是高频运行的小模型。

它不负责直接聊天。

核心问题只有：

> 新发生的内容有什么值得长期保留？

---

# 13. Memory Agent 输入

典型输入：

```text
当前消息
最近数轮上下文
当前联系人
已有相关记忆
当前长期 profile
```

---

# 14. Memory Agent 输出

建议输出严格 JSON。

例如：

```json
{
  "memory_action": "create",
  "memory_type": "personal_fact",
  "importance": 0.82,
  "content": "Bob 下个月准备搬到东京",
  "entities": ["Bob", "东京"],
  "time_relevance": "next_month",
  "embed": true
}
```

也可以：

```json
{
  "memory_action": "none"
}
```

---

# 15. 记忆类型

至少分为以下类型。

## Identity Memory

关于 AI 本人的稳定身份。

例如：

```text
职业
经历
兴趣
重要身份信息
```

---

## Relationship Memory

联系人关系。

例如：

```text
Bob 是大学同学。
认识 5 年。
平时聊天比较随意。
```

---

## Fact Memory

事实型记忆。

例如：

```text
Bob 有一只猫。
Bob 去年去了日本。
```

---

## Preference Memory

偏好。

例如：

```text
Bob 不喜欢太正式的语气。
Bob 喜欢摄影。
```

---

## Event Memory

事件。

例如：

```text
Bob 8 月 15 日面试。
```

---

## Intention / Promise Memory

承诺和待办。

例如：

```text
我答应晚上把文件发给 Bob。
```

---

## Style Memory

真人本人的表达方式。

例如：

```text
习惯短句。
很少用长篇解释。
喜欢使用“行”“嗯”“可以”。
```

---

# 16. 原始历史

所有 Telegram 原始消息永久存入：

```text
messages
```

不依赖 LLM 的总结版本。

这样以后：

- Memory Agent 判断错了可以重新处理
- 可以重新生成 summary
- 可以更换 embedding 模型
- 可以更换主模型
- 可以审计历史

原始历史是系统的 source of truth。

---

# 17. Structured Memory

长期结构化记忆可以类似目录管理：

```text
memory/

identity
profile
relationships
preferences
facts
events
intentions
style
summaries
```

概念上也可以表现为：

```text
identity.md
profile.md
relationships/
facts/
events/
daily/
weekly/
```

实际实现可以存 PostgreSQL。

---

# 18. Summary 系统

随着历史增长，需要持续压缩。

例如：

```text
Raw Conversation
       |
   Daily Summary
       |
   Weekly Summary
       |
 Long-term Summary
```

建议层级：

```text
recent raw messages
daily summary
weekly summary
relationship summary
long-term profile
```

旧聊天不需要每次完整送给主模型。

---

# 19. Embedding / Vector Memory

Embedding 用于语义联想。

例如当前消息：

```text
最近工作还是挺累。
```

向量检索可以找到三个月前：

```text
刚换工作，有点担心适应不了。
```

即使关键词不同，也能建立语义关联。

Vector Memory 主要解决：

> “以前有没有发生过和现在类似的事情？”

---

# 20. Vector Memory 与 Structured Memory 的区别

Structured Memory 负责：

```text
明确事实
稳定信息
联系人状态
事件
承诺
人格
```

Vector Memory 负责：

```text
模糊联想
语义相关
旧经历检索
类似事件
```

两者同时存在。

不能完全依赖 embedding。

---

# 21. Memory 生命周期

每条消息进入以后：

```text
Raw Message
   |
Memory Agent
   |
+-- 无价值 -> 只保留原始历史
|
+-- 有价值
       |
       +-- Structured Memory
       |
       +-- Embedding
       |
       +-- Time Relevant Memory
```

---

# 22. 记忆冲突

例如旧记忆：

```text
Bob 喜欢咖啡。
```

新消息：

```text
最近把咖啡戒了。
```

Memory Agent 应识别：

```json
{
  "action": "supersede",
  "old_memory": "...",
  "new_memory": "Bob 已经停止喝咖啡"
}
```

旧数据不要一定物理删除。

可以：

```text
active=false
superseded_by=id
```

保持历史变化轨迹。

---

# 23. 记忆淡化

记忆可以随时间降低权重。

例如：

```text
importance
confidence
recency
usage_count
```

综合决定检索优先级。

但重要的稳定人格信息：

```text
身份
重要关系
核心偏好
重要经历
```

不应该因为时间简单删除。

---

# 24. Time / Proactive Agent

时间感知模型：

**DeepSeek v4 Flash**

这是系统的主动性核心。

职责：

> 判断当前时间点有没有值得主动做的事情。

它不负责最终自然语言表达。

---

# 25. Proactive Agent 的时间维度

需要同时理解：

## Absolute Time

```text
现在几点
日期
星期几
```

---

## Relationship Time

```text
多久没联系
最后一次是谁先发消息
上一次 AI 主动联系是什么时候
```

---

## Event Time

```text
生日
面试
旅行
截止日期
项目节点
```

---

## Promise Time

例如：

```text
我晚上看看。
```

系统需要形成：

```text
Promise:
晚上检查某件事情
```

---

## Conversation Time

例如：

```text
昨天聊过
刚刚聊完
五分钟之前用户还在说话
```

避免不自然地主动插话。

---

## Social Time

例如：

```text
凌晨三点
工作日上午
周末晚上
```

影响是否适合主动联系。

---

# 26. Proactive Agent 不直接读取全部数据库

为了控制 token 和稳定性，在 Memory System 和 Proactive Agent 中间加入：

**Time Context Builder**

它负责从数据库构造压缩输入。

例如：

```json
{
  "now": "2026-08-08T21:30:00+09:00",

  "relationship": {
    "last_contact_hours": 72,
    "relationship_level": "close_friend",
    "last_initiator": "user"
  },

  "pending": [
    {
      "content": "Bob 明天参加面试",
      "importance": 0.9
    }
  ],

  "events": [],

  "relevant_memory": [
    "Bob 最近对面试比较紧张"
  ],

  "agent_state": {
    "proactive_messages_today": 0
  }
}
```

这样一次轮询可以覆盖多个维度。

---

# 27. Proactive Tick

建议 Scheduler 周期运行。

例如：

```text
每 30 分钟
```

或者：

```text
每 1 小时
```

一次 Proactive Tick：

```text
Scheduler
    |
Time Context Builder
    |
DeepSeek v4 Flash
    |
Decision
```

输出：

```json
{
  "action": "send",
  "priority": 0.81,
  "reason": "follow_up",
  "topic": "job_interview"
}
```

或者：

```json
{
  "action": "none"
}
```

---

# 28. 主动消息最终生成

Proactive Agent 只决定：

```text
是否说
什么时候说
为什么说
说什么主题
```

Main AI 负责：

```text
具体怎么说
```

例如时间模型：

```json
{
  "action": "send",
  "reason": "Bob 明天面试且最近比较紧张",
  "topic": "interview"
}
```

Main AI 最终生成：

```text
明天是不是面试了？准备得怎么样了
```

这样语言仍然保持人格一致。

---

# 29. 主动消息预算

必须避免 AI 变成骚扰系统。

每个联系人建议维护：

```text
proactive_enabled
daily_limit
minimum_interval
relationship_level
quiet_hours
```

例如：

```text
close_friend:
一天最多 2 次

normal_friend:
一天最多 1 次

acquaintance:
默认很少主动
```

还需要：

```text
last_proactive_at
```

防止连续主动联系。

---

# 30. 主动消息状态

主动消息发送后：

```text
role=assistant
source=proactive_ai
```

之后它和正常聊天一样成为历史的一部分。

因此下一次 AI 会知道：

> “我之前主动问过他这个问题。”

---

# 31. 真人行为学习

真人本人产生的数据价值非常高。

例如 AI 草稿：

```text
好的，我之后看一下。
```

真人实际发送：

```text
行 晚点看
```

长期积累以后 Memory Agent 可以提取：

```text
真人偏好：
短句
口语
较少使用完整书面句
```

这些内容更新：

```text
style profile
```

这样主模型会逐渐接近真人表达方式。

---

# 32. 模型分工

最终模型配置：

## Main AI

```text
GPT 5.6 Luna
```

职责：

- 最终聊天
- 人格
- 复杂推理
- 情绪表达
- 主动消息生成
- 高层决策

调用频率相对较低，但每次价值高。

---

## Memory Agent

```text
GPT 5.4 nano
```

职责：

- 记忆抽取
- 事实整理
- Summary
- 冲突判断
- 记忆更新
- Embedding 调度
- 时间相关信息抽取

调用频率高。

---

## Proactive Agent

```text
DeepSeek v4 Flash
```

职责：

- 时间感知
- 事件判断
- 关系时间
- 承诺检查
- 主动性评分
- 是否需要触发 Main AI

周期调用。

---

# 33. Embedding 模型

Embedding 建议独立。

Memory Agent 不负责真正“生成向量”。

流程：

```text
Memory Agent
    |
判断 embed=true
    |
Embedding API
    |
Vector DB
```

这样未来可以单独替换 embedding 模型。

---

# 34. 数据库设计

建议使用 PostgreSQL。

核心表：

```text
users
contacts
conversations
messages
conversation_modes
memories
memory_relations
summaries
events
intentions
relationship_states
agent_states
proactive_logs
control_logs
```

---

# 35. messages

示例字段：

```text
id
telegram_message_id
chat_id
sender_id
role
source
content
reply_to
created_at
edited_at
metadata
```

---

# 36. memories

```text
id
contact_id
type
content
importance
confidence
active
created_at
updated_at
last_accessed_at
superseded_by
metadata
```

---

# 37. events

```text
id
contact_id
title
start_time
end_time
importance
status
source_memory_id
```

---

# 38. intentions

```text
id
contact_id
owner
content
expected_time
status
importance
```

`owner` 可以是：

```text
self
contact
```

表示是谁的承诺。

---

# 39. relationship_states

```text
contact_id
relationship_level
last_contact_at
last_user_message_at
last_assistant_message_at
last_proactive_at
interaction_frequency
```

---

# 40. agent_states

系统级运行状态，例如：

```text
global_ai_enabled
default_mode
proactive_enabled
maintenance_mode
```

也可以保存：

```text
current_social_energy
daily_message_budget
```

但这些拟人格状态应该保持简单和可解释。

---

# 41. Vector DB

可以使用：

```text
pgvector
```

这样 PostgreSQL 本身就可以同时承担：

- 普通数据库
- embedding vector storage

初期不一定需要单独部署 Pinecone、Milvus 等系统。

这可以显著降低复杂度。

---

# 42. Context Builder

这是系统质量非常关键的一层。

主模型的上下文不应该简单拼接。

建议顺序：

```text
System Identity
Personality
Current Relationship
Current Time
Relevant Structured Memories
Relevant Semantic Memories
Conversation Summary
Recent Raw Messages
Current Message
```

并给不同层设置 token budget。

---

# 43. 上下文预算

例如：

```text
Identity / personality:
固定预算

Relationship:
小预算

Relevant memory:
中预算

Summary:
中预算

Recent raw messages:
较大预算
```

随着历史增长，也不会无限扩大。

---

# 44. Cache

因为个人 AI 大量 Prompt 内容重复，因此缓存是重要的成本优化手段。

建议至少四层缓存。

## L1 Identity Cache

几乎长期不变：

```text
identity
personality
core style
```

长时间缓存。

---

## L2 Relationship / Profile Cache

更新较慢：

```text
关系摘要
联系人基本信息
长期背景
```

---

## L3 Conversation Cache

最近会话。

生命周期较短。

---

## L4 Retrieval Cache

Embedding 检索结果。

相同或近似主题短时间内可以复用。

---

# 45. Prompt Cache 设计原则

为了提高缓存命中率，Prompt 尽量稳定排序。

例如固定：

```text
SYSTEM
IDENTITY
PERSONALITY
RELATIONSHIP
MEMORY
RECENT
USER
```

不要每次随机改变前缀内容。

稳定部分尽量放在 Prompt 前方。

动态内容放后方。

这样更利于 provider 侧 prompt caching。

---

# 46. 调度

Ubuntu Server 上建议：

```text
Docker Compose
```

服务可以拆分：

```text
telegram-userbot
control-bot
api-backend
memory-worker
proactive-worker
scheduler
postgres
redis
```

---

# 47. Redis

Redis 可以承担：

```text
消息队列
任务锁
缓存
短期状态
rate limit
```

例如：

```text
Telegram message
   |
Queue
   |
AI Worker
```

避免消息并发导致同一联系人回复顺序错乱。

---

# 48. 同一会话锁

必须保证：

```text
chat_id
```

级别串行。

否则：

用户连续发：

```text
A
B
C
```

可能发生：

```text
AI回复B
AI回复A
AI回复C
```

因此应设置：

```text
conversation lock
```

---

# 49. 消息防抖

Telegram 真人聊天通常连续发送多条：

```text
你今晚
有没有空
一起吃饭？
```

不要每条调用一次主模型。

建议等待一个短 debounce window，将连续消息合并。

例如：

```text
2～5 秒
```

然后作为一个 conversation turn 处理。

---

# 50. 真人与 AI 并发冲突

这是必须处理的问题。

例如：

AI 正在生成回复。

真人此时手动发送消息。

系统检测到：

```text
outgoing human message
```

立即取消该 AI 回复任务。

否则可能出现：

```text
真人：
行，没问题

AI 2 秒后：
好的，我看看时间。
```

造成身份割裂。

因此规则：

```text
Human outgoing message
    |
cancel pending AI response
```

---

# 51. Human takeover 自动判断

除了控制 Bot 手动 `/human`，也可以设置：

真人只要在该会话发一条消息：

```text
自动进入 temporary HUMAN
```

例如保持：

```text
10 分钟
```

真人停止后再恢复 AUTO。

这个策略可以做成可配置项。

---

# 52. 安全

Telethon Session 等价于账号登录权限。

必须：

- 不上传 Git
- 文件权限严格限制
- Session 加密备份
- 服务器使用 SSH Key
- 禁止暴露管理端口
- Control Bot 设置 User ID 白名单
- 数据库禁止公网直连

---

# 53. Telegram 风控

Userbot 属于用户账号自动化。

需要避免：

```text
大规模陌生人私聊
广告群发
极高发送频率
自动批量加群
批量添加联系人
```

本项目定位为个人 AI 分身，相对更接近正常个人通信模式。

发送速率需要符合正常人类行为。

---

# 54. 日志与审计

建议保存：

```text
AI request
AI response
memory changes
proactive decision
mode changes
control commands
errors
```

特别是主动行为：

```text
为什么主动发送
使用了哪些记忆
由哪个模型决定
```

必须可追踪。

---

# 55. Proactive Log

例如：

```json
{
  "contact": "Bob",
  "timestamp": "...",
  "decision": "send",
  "reason": "interview tomorrow",
  "priority": 0.82,
  "generated_message": "明天是不是面试了？"
}
```

这样可以调整主动性策略。

---

# 56. 失败保护

如果 Main AI API 失败：

```text
不发送
```

不要发送异常内容。

如果 Memory Agent 失败：

```text
原始消息仍然保存
稍后可重新整理
```

如果 Proactive Agent 失败：

```text
本轮跳过
```

主动系统的原则应该是：

```text
宁可少发，不要乱发。
```

---

# 57. 主动性最终安全阀

即使 Proactive Agent 输出：

```text
send=true
```

也要经过规则层检查：

```text
proactive enabled?
quiet hours?
daily limit?
minimum interval?
chat currently active?
human mode?
blocked contact?
```

满足后才进入 Main AI。

---

# 58. 项目最终运行逻辑

普通被动聊天：

```text
Telegram Incoming
      |
Conversation Engine
      |
Save Raw Message
      |
Memory Agent
      |
Context Builder
      |
Main AI
      |
Telegram Send
      |
Save assistant/source=ai
```

真人回复：

```text
Telegram Outgoing Human
      |
Telethon Listener
      |
Save assistant/source=human
      |
Memory Agent
      |
Update Long-term Memory
```

主动消息：

```text
Scheduler
   |
Time Context Builder
   |
Proactive Agent
   |
Decision
   |
Rules
   |
Main AI
   |
Telegram Send
   |
Save assistant/source=proactive_ai
```

---

# 59. 最终哲学

系统中的人格并不存在于某一个单独模型中。

人格由以下内容共同形成：

```text
长期身份
+
历史行为
+
真人消息
+
结构化记忆
+
关系记忆
+
语言风格
+
时间连续性
+
主动行为
```

Main AI 只是运行时的推理和表达核心。

Memory Agent 提供长期连续性。

Proactive Agent 提供时间和主动性。

Telegram 真人账号提供稳定的社会身份。

真人本人不断产生的真实消息则持续修正 AI。

最终形成：

```text
真人
  ↕
AI Digital Twin
  ↕
同一个 Telegram 身份
```

随着使用时间增加，AI 应逐渐获得：

- 更准确的个人语言风格
- 更完整的人际关系记忆
- 更稳定的长期人格
- 更自然的时间感
- 更合理的主动沟通能力
- 更接近真人本人的判断模式

整个系统最终可以概括为：

**一个由真实 Telegram 用户账号承载，以 GPT 5.6 Luna 为人格与推理核心、GPT 5.4 nano 为长期记忆管理员、DeepSeek v4 Flash 为时间与主动性管理器，并结合结构化记忆、语义向量记忆、真人实时接管以及 Telegram Control Bot 的长期运行个人 AI 数字分身系统。**