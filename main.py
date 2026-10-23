"""astrbot_plugin_group_window

把群聊里"主动回复"的上下文，从"几十秒前抽中的那条消息"换成"此刻群聊最近的这一段"。

v0.2.0 相对 v0.1.0 的关键改动：
  1. 等待窗口放在 on_waiting_llm_request（会话锁之前，不阻塞别人）；
     取快照放在 on_llm_request（会话锁之后，拿到最新缓冲）。
     —— 4.29 的会话锁会串行化同群请求，锁前取快照会拿到过时数据。
  2. 捕获改为 event_message_type(ALL) + regex 双通道，按 message_id 去重。
  3. 不再改 system_prompt（会破坏 prompt 缓存），改用 req.extra_user_content_parts。
  4. 增加 trace 开关：所有决策按 INFO 打印，不再是看不见的 DEBUG。
  5. 增加 /gw 指令，随时看当前群的缓冲。

不接管 AstrBot 的 active_reply：那件事仍由它负责，本插件只负责"说之前把上下文换新"。
"""

from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 4.24+ 才有；没有就退化为只用 prompt
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None  # type: ignore[assignment]

PLUGIN_ID = "astrbot_plugin_group_window"
VERSION = "0.4.3"
DEFAULT_SKIP_MARKER = "[SKIP]"


@dataclass
class ChatLine:
    ts: float
    message_id: str
    sender_name: str
    sender_id: str
    text: str
    addressed: bool
    command: bool


@dataclass
class GroupState:
    lines: deque = field(default_factory=deque)
    last_capture: float = 0.0
    last_reply_ts: float = 0.0


@register(
    PLUGIN_ID,
    "nozomi",
    "群聊窗口：让主动回复基于此刻的群聊上下文",
    VERSION,
)
class GroupWindowPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config
        self._groups: dict[str, GroupState] = {}
        self._active: dict[str, tuple[str, float]] = {}
        # Agent 工具循环会多次触发 on_llm_request，只改写第一次
        self._consumed: set[str] = set()
        # 被冷却拦下的请求，用于在发送阶段兜底
        self._suppressed: set[str] = set()
        self._stats = {
            "captured": 0,
            "skipped_self": 0,
            "skipped_empty": 0,
            "skipped_cmd": 0,
            "skipped_dup": 0,
            "llm_hook": 0,
            "addressed": 0,
            "rewritten": 0,
            "suppressed": 0,
            "cooled": 0,
            "tool_calls": 0,
            "errors": 0,
        }
        logger.info(
            "[%s] 已加载 v%s | enabled=%s dry_run=%s trace=%s window=%ss context=%ss max_lines=%s",
            PLUGIN_ID,
            VERSION,
            self._cfg_bool("enabled", True),
            self._cfg_bool("dry_run", False),
            self._cfg_bool("trace", True),
            self._cfg_int("window_seconds", 5, 0),
            self._cfg_int("context_seconds", 40, 5),
            self._cfg_int("max_messages", 25, 1),
        )
        # 排查用：把 AstrBot 注册的工具名列出来，方便确认"群聊历史查询"那个工具叫什么
        try:
            tools = getattr(getattr(self.context, "provider_manager", None), "llm_tools", None)
            func_list = getattr(tools, "func_list", None)
            if func_list is not None:
                names = [str(getattr(t, "name", type(t).__name__)) for t in func_list]
                logger.info(
                    "[%s] 已注册的 LLM 工具（%d 个）：%s",
                    PLUGIN_ID,
                    len(names),
                    ", ".join(names) if names else "(空)",
                )
        except Exception:
            logger.debug("[%s] 读取 LLM 工具列表失败（不影响运行）", PLUGIN_ID)

    # ------------------------------------------------------------------
    # 捕获：双通道 + 去重
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-100000)
    async def capture_all(self, event: AstrMessageEvent) -> None:
        await self._capture(event, "all")

    @filter.regex(r"^(?!/)(?!!)[\s\S]*$", priority=-100000)
    async def capture_regex(self, event: AstrMessageEvent) -> None:
        await self._capture(event, "regex")

    async def _capture(self, event: AstrMessageEvent, source: str) -> None:
        try:
            if not self._enabled():
                return

            group_id = self._group_id(event)
            if group_id is None:
                logger.debug("[gw] capture[%s] | 非群聊，跳过 | umo=%s", source, self._umo(event))
                return

            key = self._key(event)

            if self._is_self_message(event):
                self._stats["skipped_self"] += 1
                self._trace("capture[%s] | 机器人自己的消息 | g=%s", source, group_id)
                return

            raw = self._event_text(event)
            if not raw:
                self._stats["skipped_empty"] += 1
                self._trace(
                    "capture[%s] | 空消息 | g=%s | chain=%s",
                    source,
                    group_id,
                    self._chain_names(event),
                )
                return

            command = self._is_command(raw)
            if command and self._cfg_bool("ignore_commands", True):
                self._stats["skipped_cmd"] += 1
                self._trace("capture[%s] | 指令消息 | g=%s | %s", source, group_id, self._clip(raw, 30))
                return

            state = self._state(group_id)
            body = self._clip(raw, self._cfg_int("per_line_chars", 120, 20))
            sender_id = self._sender_id(event)

            for line in state.lines:
                if line.message_id and line.message_id == key:
                    self._stats["skipped_dup"] += 1
                    logger.debug("[gw] capture[%s] | 重复（另一通道已记） | g=%s", source, group_id)
                    return

            # 同一条消息可能先被钩子补录过（message_id 不同、内容相同），
            # 这里只更新 id，不重复入缓冲。
            twin = self._find_same_recent(state, sender_id, body)
            if twin is not None:
                twin.message_id = key
                self._stats["skipped_dup"] += 1
                logger.debug("[gw] capture[%s] | 与钩子补录的合并 | g=%s", source, group_id)
                return

            now = time.time()
            line = ChatLine(
                ts=now,
                message_id=key,
                sender_name=self._sender_name(event),
                sender_id=sender_id,
                text=body,
                addressed=self._is_addressed(event),
                command=command,
            )
            state.lines.append(line)
            state.last_capture = now
            self._stats["captured"] += 1

            self._trace(
                "capture[%s] | g=%s | buf=%d | addr=%s | id=%s | %s: %s",
                source,
                group_id,
                len(state.lines),
                line.addressed,
                key,
                line.sender_name,
                self._clip(line.text, 40),
            )
            self._prune()
        except Exception:
            self._stats["errors"] += 1
            logger.error("[%s] capture 异常：\n%s", PLUGIN_ID, traceback.format_exc())

    def _record_from_hook(self, event: AstrMessageEvent) -> None:
        """在 LLM 钩子里补录触发消息。

        handler 链可能排在会话锁后面（实测晚 9~13 秒），所以缓冲里往往没有
        当前这条。钩子里的 event 一定带着触发消息本身，这里先把它补进去。
        """
        try:
            group_id = self._group_id(event)
            if group_id is None:
                return
            text = self._event_text(event)
            if not text:
                return

            clip = self._cfg_int("per_line_chars", 120, 20)
            body = self._clip(text, clip)
            sender_id = self._sender_id(event)
            state = self._state(group_id)

            # 同一人、同样内容、30 秒内视为同一条（防止和延迟的 handler 重复记账）
            if self._find_same_recent(state, sender_id, body) is not None:
                return

            now = time.time()
            state.lines.append(
                ChatLine(
                    ts=now,
                    message_id=self._key(event),
                    sender_name=self._sender_name(event),
                    sender_id=sender_id,
                    text=body,
                    addressed=self._is_addressed(event),
                    command=self._is_command(text),
                )
            )
            state.last_capture = now
            self._stats["captured"] += 1
            self._trace(
                "capture[hook] | g=%s | buf=%d | %s: %s",
                group_id,
                len(state.lines),
                self._sender_name(event),
                self._clip(text, 40),
            )
        except Exception:
            self._stats["errors"] += 1
            logger.error("[%s] _record_from_hook 异常：\n%s", PLUGIN_ID, traceback.format_exc())

    # ------------------------------------------------------------------
    # 窗口等待：会话锁之前
    # ------------------------------------------------------------------

    @filter.on_waiting_llm_request(priority=1000)
    async def wait_window(self, event: AstrMessageEvent) -> None:
        key = self._key(event)
        try:
            if not self._enabled():
                return

            group_id = self._group_id(event)
            if group_id is None:
                return

            self._stats["llm_hook"] += 1

            # 先补录触发消息本身：handler 链可能还没跑到这条
            self._record_from_hook(event)

            if self._is_addressed(event):
                self._stats["addressed"] += 1
                self._trace(
                    "llm_hook | 已被唤醒，不做窗口 | g=%s | 原文=%s",
                    group_id,
                    self._clip(getattr(event, "message_str", "") or "", 30),
                )
                return

            state = self._groups.get(group_id)
            if state is None or not state.lines:
                self._trace("llm_hook | 缓冲为空 | g=%s | id=%s", group_id, key)
                return

            # 插件级冷却：AstrBot 在会话被唤醒后可能持续响应，不看配置的概率。
            # 这里自己兜一层，保证两次主动插话之间至少隔 cooldown 秒。
            #
            # 注意：这里【不能】用 event.stop_event()。AstrBot 内置 AI 的 on_message
            # 是个 yield 着 request_llm() 的异步生成器，中途终止传播会让它抛
            # GeneratorExit，被记成"主动回复失败"的 ERROR。改成放行 + 极短占位
            # prompt，在 on_llm_request 里换成 "." 让这次调用几乎不花钱，
            # 最后在 on_decorating_result 清空消息链，不发给群。
            cooldown = self._cfg_int("cooldown_seconds", 30, 0)
            if cooldown > 0 and state.last_reply_ts > 0:
                elapsed = time.time() - state.last_reply_ts
                if elapsed < cooldown:
                    self._stats["cooled"] += 1
                    self._suppressed.add(key)
                    self._trace(
                        "冷却中 | g=%s 距上次插话 %.1fs < %ds，本轮静默丢弃",
                        group_id,
                        elapsed,
                        cooldown,
                    )
                    return

            # 事件在捕获阶段与钩子阶段不是同一个对象（message_id 会变），
            # 无法可靠拿到"到达时间"，因此固定等满窗口。
            wait = float(
                min(
                    self._cfg_int("window_seconds", 5, 0),
                    self._cfg_int("max_wait_seconds", 6, 0),
                )
            )

            cid = uuid.uuid4().hex[:6]
            # 冷却从"放行"这一刻开始计时，而不是等回复发出去再计。
            # 否则在"开窗口 → 等锁 → 生成"这几秒里，下一次触发会溜过冷却。
            state.last_reply_ts = time.time()
            self._active[key] = (cid, time.time())

            self._trace(
                "llm_hook | cid=%s g=%s 等待=%.2fs 缓冲=%d | 原文=%s",
                cid,
                group_id,
                wait,
                len(state.lines),
                self._clip(getattr(event, "message_str", "") or "", 30),
            )
            if wait > 0:
                await asyncio.sleep(wait)
            self._trace("llm_hook | cid=%s 窗口结束，等锁取快照", cid)

        except asyncio.CancelledError:
            self._trace("llm_hook | 请求被取消 | id=%s", key)
            raise
        except Exception:
            self._stats["errors"] += 1
            logger.error("[%s] wait_window 异常：\n%s", PLUGIN_ID, traceback.format_exc())

    # ------------------------------------------------------------------
    # 取快照 + 改写：会话锁之后
    # ------------------------------------------------------------------

    # 优先级 -1000：先于其他插件改写 prompt，让记忆/知识库类插件能在窗口之上追加内容
    @filter.on_llm_request(priority=-1000)
    async def build_request(self, event: AstrMessageEvent, req: Any) -> None:
        key = self._key(event)
        cid = self._active.get(key, ("-", 0.0))[0]
        try:
            # 冷却期内的请求：换成一个几乎不花 token 的占位 prompt，
            # 结果在 on_decorating_result 里丢弃（见 suppress_skip）。
            if key in self._suppressed:
                try:
                    if isinstance(getattr(req, "prompt", None), str):
                        req.prompt = "。"
                    self._trace("冷却 | 已把本轮 prompt 换成占位符，结果将被丢弃")
                except Exception:
                    logger.warning("[gw] 冷却占位改写失败（影响很小）")
                return

            if key not in self._active:
                return

            if key in self._consumed:
                self._trace("request | cid=%s 已是后续轮次（工具循环），不改写", cid)
                return
            self._consumed.add(key)

            if self._is_addressed(event):
                return

            group_id = self._group_id(event)
            state = self._groups.get(group_id) if group_id else None
            if state is None or not state.lines:
                self._trace("request | cid=%s 缓冲为空，跳过 | g=%s", cid, group_id)
                return

            window_text, picked, span = self._build_window(state)
            if not window_text:
                return

            newest_age = time.time() - state.lines[-1].ts
            self._trace(
                "window | cid=%s g=%s 条数=%d 覆盖=%.1fs 字符=%d 最新一条=%.1fs前",
                cid,
                group_id,
                picked,
                span,
                len(window_text),
                newest_age,
            )
            if self._cfg_bool("debug_log", True):
                logger.info("[gw][%s] window 全文：\n%s", cid, window_text)

            if self._dry_run():
                self._trace("request | cid=%s dry_run，不改写", cid)
                return

            old_len = len(req.prompt) if isinstance(getattr(req, "prompt", ""), str) else -1
            injected = False
            try:
                if isinstance(getattr(req, "prompt", None), str):
                    req.prompt = window_text
                    injected = True
                else:
                    logger.warning("[gw][%s] req.prompt 不是字符串，改用 extra_user_content_parts", cid)
            except Exception:
                logger.warning("[gw][%s] 设置 req.prompt 失败，改用 extra_user_content_parts", cid)

            if not injected and TextPart is not None:
                try:
                    part = TextPart(text=window_text)
                    marker = getattr(part, "mark_as_temp", None)
                    if callable(marker):
                        part = marker()
                    container = getattr(req, "extra_user_content_parts", None)
                    if container is not None:
                        container.append(part)
                        injected = True
                        self._trace("request | cid=%s 已追加 extra_user_content_parts", cid)
                except Exception:
                    logger.warning("[gw][%s] 追加 extra_user_content_parts 失败", cid)

            if not injected:
                logger.error("[gw][%s] 两种注入方式都失败，本轮仍是旧上下文", cid)
                return

            self._stats["rewritten"] += 1
            self._trace(
                "request | cid=%s 原 prompt=%d 新 prompt=%d | req=%s",
                cid,
                old_len,
                len(window_text),
                id(req),
            )

        except Exception:
            self._stats["errors"] += 1
            logger.error("[%s] build_request 异常：\n%s", PLUGIN_ID, traceback.format_exc())

    # ------------------------------------------------------------------
    # 观察模型是否无视指令去调工具
    # ------------------------------------------------------------------

    @filter.on_using_llm_tool(priority=1000)
    async def watch_tool(self, event: AstrMessageEvent, tool: Any, tool_args: Any) -> None:
        key = self._key(event)
        if key not in self._active:
            return
        cid = self._active[key][0]
        self._stats["tool_calls"] += 1
        name = getattr(tool, "name", None) or type(tool).__name__
        logger.info("[gw][%s] tool_call | 模型调用了工具：%s（预期不该发生）", cid, name)

    # ------------------------------------------------------------------
    # [SKIP] 门控
    # ------------------------------------------------------------------

    @filter.on_decorating_result(priority=1000)
    async def suppress_skip(self, event: AstrMessageEvent) -> None:
        key = self._key(event)

        # 冷却兜底：把消息链清空，让 respond 阶段没有内容可发
        if key in self._suppressed:
            try:
                result = event.get_result()
                if result is not None:
                    result.chain = []
                self._trace("冷却 | 已清空本轮回复，不发送")
            except Exception:
                logger.warning("[gw] 冷却清理失败")
            return

        if key not in self._active:
            return
        cid = self._active[key][0]
        try:
            result = event.get_result()
            if result is None:
                return

            text = self._result_text(result)
            marker = str(self._cfg_str("skip_marker", DEFAULT_SKIP_MARKER)).strip() or DEFAULT_SKIP_MARKER

            if text.strip().startswith(marker):
                try:
                    result.chain = []
                except Exception:
                    logger.warning("[gw][%s] 清空 chain 失败", cid)
                self._stats["suppressed"] += 1
                logger.info("[gw][%s] skip | 模型判断不该插话 | 原文=%s", cid, self._clip(text, 60))
            else:
                group_id = self._group_id(event)
                state = self._groups.get(group_id) if group_id else None
                if state is not None:
                    state.last_reply_ts = time.time()
                logger.info(
                    "[gw][%s] send | 长度=%d | result=%s | %s",
                    cid,
                    len(text),
                    id(result),
                    self._clip(text, 40),
                )
        except Exception:
            self._stats["errors"] += 1
            logger.error("[%s] suppress_skip 异常：\n%s", PLUGIN_ID, traceback.format_exc())

    # ------------------------------------------------------------------
    # 指令：/gw 看当前群状态
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("gw")
    async def gw_status(self, event: AstrMessageEvent):
        group_id = self._group_id(event) or "(非群聊)"
        state = self._groups.get(group_id)
        lines = list(state.lines)[-5:] if state else []
        body = [
            f"插件：{PLUGIN_ID} v{VERSION}",
            f"群：{group_id}",
            f"缓冲：{len(state.lines) if state else 0} 条",
            f"窗口：{self._cfg_int('window_seconds', 5)}s / 回看 {self._cfg_int('context_seconds', 40)}s / 最多 {self._cfg_int('max_messages', 25)} 条，冷却 {self._cfg_int('cooldown_seconds', 30)}s",
            f"dry_run：{self._dry_run()}  trace：{self._cfg_bool('trace', True)}",
            f"统计：{self._stats}",
            "最近 5 条：",
        ]
        for line in lines:
            stamp = datetime.fromtimestamp(line.ts).strftime("%H:%M:%S")
            body.append(f"  [{stamp}] {line.sender_name}: {self._clip(line.text, 30)}")
        if not lines:
            body.append("  (空)")
        yield event.plain_result("\n".join(body))

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def terminate(self) -> None:
        logger.info("[%s] 卸载 | 统计：%s", PLUGIN_ID, self._stats)
        self._groups.clear()
        self._active.clear()
        self._consumed.clear()
        self._suppressed.clear()

    # ------------------------------------------------------------------
    # 窗口构建
    # ------------------------------------------------------------------

    def _build_window(self, state: GroupState) -> tuple[str, int, float]:
        now = time.time()
        span_limit = float(self._cfg_int("context_seconds", 40, 5))
        max_lines = self._cfg_int("max_messages", 25, 1)
        max_chars = self._cfg_int("max_chars", 1200, 100)

        recent = [ln for ln in state.lines if now - ln.ts <= span_limit]
        if not recent:
            return "", 0, 0.0
        recent = recent[-max_lines:]

        picked: list[ChatLine] = []
        used = 0
        for line in reversed(recent):
            cost = len(line.sender_name) + len(line.text) + 16
            if picked and used + cost > max_chars:
                break
            picked.append(line)
            used += cost
        picked.reverse()
        if not picked:
            return "", 0, 0.0

        rendered = "\n".join(self._render_line(line) for line in picked)
        covered = picked[-1].ts - picked[0].ts

        style = str(self._cfg_str("reply_style", "")).strip()
        extra = str(self._cfg_str("extra_instruction", "")).strip()
        marker = str(self._cfg_str("skip_marker", DEFAULT_SKIP_MARKER)).strip() or DEFAULT_SKIP_MARKER

        parts = ["【群聊最近记录】", rendered, "", "【现在轮到你说话】"]
        if style:
            parts.append(style)
        if extra:
            parts.append(extra)
        parts.append(f"如果此刻不适合插话，只输出 {marker}。")
        return "\n".join(parts), len(picked), covered

    def _render_line(self, line: ChatLine) -> str:
        stamp = datetime.fromtimestamp(line.ts).strftime("%H:%M:%S")
        return f"[{stamp}] {line.sender_name}: {line.text}"

    # ------------------------------------------------------------------
    # 事件信息
    # ------------------------------------------------------------------

    def _key(self, event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        mid = getattr(message_obj, "message_id", None)
        if mid:
            return str(mid)
        return f"obj{id(event)}"

    def _umo(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "?"))

    def _group_id(self, event: AstrMessageEvent) -> str | None:
        getter = getattr(event, "get_group_id", None)
        if not callable(getter):
            return None
        try:
            value = getter()
        except Exception:
            return None
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _sender_id(self, event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                value = getter()
                if value is not None and str(value).strip():
                    return str(value).strip()
            except Exception:
                pass
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        for name in ("user_id", "uin", "sender_id", "id"):
            value = getattr(sender, name, None)
            if value is not None and str(value).strip():
                return str(value).strip()
        return "unknown"

    def _sender_name(self, event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_name", None)
        if callable(getter):
            try:
                value = getter()
                if value is not None and str(value).strip():
                    return str(value).strip()
            except Exception:
                pass
        return self._sender_id(event)

    def _is_self_message(self, event: AstrMessageEvent) -> bool:
        getter = getattr(event, "get_self_id", None)
        if not callable(getter):
            return False
        try:
            self_id = getter()
        except Exception:
            return False
        if self_id is None:
            return False
        return str(self_id).strip() == self._sender_id(event)

    def _is_addressed(self, event: AstrMessageEvent) -> bool:
        flag = getattr(event, "is_at_or_wake_command", False)
        if callable(flag):
            try:
                flag = flag()
            except Exception:
                flag = False
        if bool(flag):
            return True
        return self._chain_mentions_self(event)

    def _chain_mentions_self(self, event: AstrMessageEvent) -> bool:
        try:
            self_id = event.get_self_id()
        except Exception:
            return False
        if not self_id:
            return False
        chain = getattr(getattr(event, "message_obj", None), "message", None)
        if not isinstance(chain, (list, tuple)):
            return False
        for component in chain:
            if type(component).__name__ != "At":
                continue
            if str(getattr(component, "qq", "")) == str(self_id):
                return True
        return False

    def _chain_names(self, event: AstrMessageEvent) -> str:
        chain = getattr(getattr(event, "message_obj", None), "message", None)
        if not isinstance(chain, (list, tuple)):
            return "?"
        return ",".join(type(c).__name__ for c in chain) or "(empty)"

    def _event_text(self, event: AstrMessageEvent) -> str:
        text = getattr(event, "message_str", "") or ""
        text = str(text).strip()
        if text:
            return text

        chain = getattr(getattr(event, "message_obj", None), "message", None)
        if not isinstance(chain, (list, tuple)):
            return ""

        parts: list[str] = []
        for component in chain:
            name = type(component).__name__
            if name == "Plain":
                parts.append(str(getattr(component, "text", "")))
            elif name == "At":
                parts.append(f"@{getattr(component, 'name', '')}")
            elif name in ("Image", "Face"):
                parts.append("[图片]")
            elif name == "Record":
                parts.append("[语音]")
            elif name == "File":
                parts.append("[文件]")
        return "".join(parts).strip()

    def _result_text(self, result: Any) -> str:
        chain = getattr(result, "chain", None)
        if not isinstance(chain, (list, tuple)):
            return ""
        parts: list[str] = []
        for component in chain:
            if type(component).__name__ == "Plain":
                parts.append(str(getattr(component, "text", "")))
            else:
                parts.append(f"[{type(component).__name__}]")
        return "".join(parts)

    # ------------------------------------------------------------------
    # 杂项
    # ------------------------------------------------------------------

    def _state(self, group_id: str) -> GroupState:
        state = self._groups.get(group_id)
        if state is None:
            state = GroupState(lines=deque(maxlen=self._cfg_int("buffer_size", 200, 10)))
            self._groups[group_id] = state
        return state

    def _find_same_recent(
        self,
        state: GroupState,
        sender_id: str,
        body: str,
        within: float = 30.0,
    ) -> ChatLine | None:
        """找最近 30 秒内同一人发的同样内容，用于合并重复记账。"""
        now = time.time()
        for line in reversed(state.lines):
            if now - line.ts > within:
                break
            if line.sender_id == sender_id and line.text == body:
                return line
        return None

    def _prune(self) -> None:
        now = time.time()
        if len(self._active) > 500:
            self._active = {k: v for k, v in self._active.items() if now - v[1] < 600}
        if len(self._consumed) > 2000:
            self._consumed.clear()
        if len(self._suppressed) > 500:
            self._suppressed.clear()
        if len(self._groups) > 200:
            stale = [k for k, s in self._groups.items() if s.last_capture < now - 3600]
            for key in stale:
                self._groups.pop(key, None)

    def _is_command(self, text: str) -> bool:
        stripped = text.lstrip()
        return stripped.startswith("/") or stripped.startswith("!")

    def _clip(self, text: str, limit: int) -> str:
        text = " ".join(str(text).split())
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)] + "…"

    def _trace(self, fmt: str, *args: Any) -> None:
        if self._cfg_bool("trace", True):
            logger.info("[gw] " + fmt, *args)
        else:
            logger.debug("[gw] " + fmt, *args)

    def _enabled(self) -> bool:
        return self._cfg_bool("enabled", True)

    def _dry_run(self) -> bool:
        return self._cfg_bool("dry_run", False)

    def _cfg_get(self, key: str, default: Any) -> Any:
        if self.config is None:
            return default
        getter = getattr(self.config, "get", None)
        if callable(getter):
            try:
                value = getter(key, default)
                return default if value is None else value
            except Exception:
                pass
        return getattr(self.config, key, default)

    def _cfg_int(self, key: str, default: int, minimum: int = 0) -> int:
        try:
            return max(minimum, int(self._cfg_get(key, default)))
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg_get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def _cfg_str(self, key: str, default: str) -> str:
        value = self._cfg_get(key, default)
        return default if value is None else str(value)
