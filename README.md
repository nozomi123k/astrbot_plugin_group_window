# astrbot_plugin_group_window

> 让群聊里的**主动回复**基于此刻的上下文，而不是几十秒前抽中的那条消息。

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.29-blue)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

---

## 问题

AstrBot 的 `active_reply` 是**逐条消息**按概率决定要不要插话。但在一个真实的群里，从"决定说话"到"消息发出去"之间隔着三段延迟：

```
触发消息 m1 ──┬── 等会话锁 ──┬── 模型生成 ──→ 回复发出
              └─ 群里又聊了 3 条
```

结果就变成：

```
群友：今晚吃什么
群友：火锅吧
群友：那家新开的？
群友：排队两小时
群友：那算了
bot ：（终于开口）今晚吃什么？                      ← 回答的是 30 秒前的第一句
```

更糟的是，AstrBot 在会话被唤醒后可能持续响应，于是同一个话题被反复回答好几遍。

## 解决

插件在"决定说话"和"真正开口"之间插一个**短窗口**，把窗口内的群聊记录打包成本次请求的输入：

```
m1 触发 → 等 5 秒（新消息继续进缓冲）→ 取快照 → 用"群聊最近记录"替换 prompt → 发送
```

```
【群聊最近记录】
[22:31:05] 小明: 今晚吃什么
[22:31:07] 小红: 火锅吧
[22:31:12] 小明: 那家新开的？
[22:31:15] 小红: 排队两小时

【现在轮到你说话】
接着最新的话题，用一句话（不超过 40 字）自然地接话。
不要回答更早的问题，不要总结记录，不要复述别人的话。
如果此刻不适合插话，只输出 [SKIP]。
```

模型看到的最后一句，就是**刚刚发生的事**。

## 特点

- **不接管** AstrBot 的主动回复判定，只负责"说之前把上下文换新"
- **不绕过** 原生管线：人格、工具、知识库、记忆插件全部照常生效
- **不改** `system_prompt`（会破坏提示词缓存），只替换 `req.prompt`
- **不碰** 全局配置、会话历史、数据库
- 自带**冷却**，避免同一话题反复插话
- `[SKIP]` 出口，让模型能判断"此刻不该说话"
- 全链路追踪日志，一条链路一个 6 位 id

## 工作方式

| 阶段 | 钩子 | 做什么 |
|---|---|---|
| ① 常驻缓冲 | `capture_all` / `capture_regex` | 每条群消息进每群的环形缓冲，不拦截、不回复 |
| ② 等待窗口 | `on_waiting_llm_request` | 非 @ 消息等待 `window_seconds`；冷却期内的直接标记丢弃 |
| ③ 取快照 | `on_llm_request` | **会话锁之后**取快照，替换 `req.prompt` |
| ④ 门控 | `on_decorating_result` | 模型输出 `[SKIP]` 或处于冷却期时，清空消息链不发送 |

> 第 ③ 步放在锁之后是刻意的：AstrBot 的 `on_waiting_llm_request` 在获取会话锁**之前**触发，在锁前取快照，等锁那十几秒里群里的新消息就全错过了。

## 安装

### 方式一：手动复制

把整个目录放进 AstrBot 的 `data/plugins/`：

```
data/plugins/astrbot_plugin_group_window/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── README.md
├── CHANGELOG.md
└── LICENSE
```

重启 AstrBot，或在 WebUI 插件页点「重载」。

### 方式二：WebUI 上传

把本目录压缩为 zip，WebUI → 插件 → 安装插件 → 上传。

### 方式三：从仓库安装

WebUI 插件页填仓库地址：`https://github.com/nozomi123k/astrbot_plugin_group_window`

## 快速开始

装好后按顺序做四件事：

1. **确认 `active_reply` 开着**（本插件依赖它决定"要不要说"）
2. **把概率调到 0.1~0.2**（`provider_ltm_settings.active_reply.possibility_reply`）
3. **确认改的是这个群正在用的配置文件**——AstrBot 支持多配置文件，改错等于没改
4. **先开 `dry_run: true` 跑一天**：插件会完整走一遍逻辑并打印窗口，但不改写任何请求

看着日志顺了，再关掉 `dry_run`。

## 配置

| 参数 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `dry_run` | `false` | 只记录不改写 |
| `window_seconds` | `5` | 触发后等多久再取快照 |
| `max_wait_seconds` | `6` | 等待上限 |
| `cooldown_seconds` | `30` | **两次插话的最小间隔**，0 = 不限制 |
| `context_seconds` | `40` | 快照回看时长 |
| `max_messages` | `25` | 快照条数上限 |
| `max_chars` | `1200` | 快照字符预算 |
| `buffer_size` | `200` | 每群缓冲条数 |
| `per_line_chars` | `120` | 单条消息截断长度 |
| `ignore_commands` | `true` | 指令消息不入缓冲 |
| `skip_marker` | `[SKIP]` | 模型不想接话时输出它 |
| `reply_style` | 见默认值 | 接话要求，写进提示词 |
| `extra_instruction` | 见默认值 | 附加指令，默认禁止它调历史查询工具 |
| `trace` | `true` | 每个决策打 INFO；关闭后降到 DEBUG |
| `debug_log` | `true` | 打印快照全文 |

### 两个参数的分工

- `possibility_reply`（AstrBot 的）：决定**想不想说**
- `cooldown_seconds`（本插件的）：决定**最多多常说**

两者正交。热聊群建议：概率 `0.15` + 冷却 `30~60`。

## 日志

一条链路一个 6 位 id，`grep "gw][a1b2c3"` 看全程：

```
[gw] capture[all] | g=... | buf=3 | addr=False | id=... | 小明: 今晚吃什么
[gw] llm_hook | cid=ab12cd 等待=5.00s 缓冲=3 | 原文=那家新开的？
[gw] window | cid=ab12cd 条数=4 覆盖=12.4s 字符=318 最新一条=0.7s前
[gw] request | cid=ab12cd 原 prompt=23 新 prompt=318 | req=1822963895856
[gw] send | cid=ab12cd 长度=18 | 哈哈哈那家确实可以
[gw] 冷却中 | g=... 距上次插话 8.3s < 30s，本轮静默丢弃
```

### 三个关键判据

| 看什么 | 正常值 | 异常说明 |
|---|---|---|
| `最新一条=X.Xs前` | < 2 秒 | > 5 秒说明缓冲跟不上，快照在答旧话 |
| `原 prompt=N 新 prompt=M` | 两数差很大 | 相同说明改写没生效 |
| `冷却中` | 偶尔出现 | 过于频繁说明插话太密，调小 `cooldown_seconds` |

## 排查

| 现象 | 先看 | 处理 |
|---|---|---|
| 完全不触发 | 日志没有 `llm_hook` | `active_reply` 没开，或改错了配置文件 |
| 窗口总是 1 条 | `capture` 很少 | 钩子没看到消息，需要换数据源 |
| 还是答旧话 | `最新一条` 很大 | 降低概率（减少排队）或缩小窗口 |
| 回复没变化 | 没有 `request` 行 | 看有没有 `两种注入方式都失败` |
| 太吵 | 频繁 `send` | 调大 `cooldown_seconds` |
| 出现空白消息 | 日志里的「冷却：已清空本轮回复」 | 报告 issue |

## 已知限制

| 限制 | 说明 |
|---|---|
| **捕获有延迟** | handler 链排在会话锁之后，实测可能晚 9~13 秒。插件在 `llm_hook` 里补录触发消息兜底，但**窗口关闭前几秒的新消息仍可能还没进缓冲**。热聊时 `最新一条` 会因此偏大 |
| **30 秒去重** | 同一人 30 秒内发完全相同的文本，会被合并成一条（防止重复记账的代价） |
| **模型不知道自己刚说过** | 窗口里没有 bot 自己的发言，所以"隔一轮又说一遍"只能靠冷却拦。根治需要会话记忆，计划后续版本 |
| **冷却不终止请求** | 为避免 AstrBot 内置 AI 的异步生成器抛 `GeneratorExit`，冷却不调用 `stop_event`，而是把 prompt 换成 `。` 并在发送前清空消息链。代价是每次冷却多一次极短的模型调用 |
| **私聊不参与** | 私聊没有群号，插件直接跳过，不影响也不受任何群的冷却影响 |

## 更新日志

见 [CHANGELOG.md](./CHANGELOG.md)。

## 许可

[MIT](./LICENSE)
