#!/usr/bin/env python3
"""Bear Code 的 Agentic Harness Runtime。

本模块把模型调用、双协议消息历史、工具路由、权限、预算、上下文压缩、Memory、
Skills、MCP 和子 Agent 串成持续循环。Agent.chat 是单轮总入口，真正的模型循环
位于 _chat_anthropic 与 _chat_openai；所有工具最终先经过 _execute_tool_call 分流。

BearCode2 在基础压缩流水线之外增加了结构化 Session Memory folding：既可在上下文
达到阈值时自动触发，也允许模型通过 compact_context 工具主动触发。

推荐阅读顺序：先看 ``Agent.chat`` 理解单轮入口，再看 ``_chat_anthropic`` 或
``_chat_openai`` 理解“模型 -> tool call -> tool result”的循环，最后看
``_execute_tool_call``、压缩流水线和在线 Skill 演化。构造函数中的大量字段只是这些
子系统的会话级状态，不必在第一次阅读时逐个记忆。

没有 Harness 基础时，可以先记住下面这个最小心智模型：

1. 大模型本身不会直接读文件或执行命令，只会返回文本，或生成“想调用哪个工具及参数”。
2. Harness 保存消息历史、把工具 schema 发给模型，并决定模型提出的工具调用是否获准。
3. 获准后由本地 Python 代码真正执行工具，再把结果作为新消息交还模型。
4. 模型根据工具结果继续推理；只要它仍返回工具调用，Harness 就重复这个循环。
5. 当模型只返回正文、不再调用工具时，当前用户请求才算完成。

因此 ``Agent`` 不是另一个模型，而是包在模型 API 外面的编排层（orchestrator）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Callable, Awaitable, Any

import anthropic
import openai

from agents.mcp_client import McpManager
from agents.memory import MemoryPrefetch, start_memory_prefetch, format_memories_for_injection
from agents.prompt import build_system_prompt
from agents.session_memory import (
    FOLD_SESSION_MEMORY_SYSTEM,
    build_anthropic_transcript,
    build_folding_user_prompt,
    build_openai_transcript,
    fallback_folded_memory,
    format_folded_memory,
    parse_folded_memory,
)
from agents.session import save_folded_session_memory, save_session
from agents.subagent import get_sub_agent_config
from agents.tools import ToolDef, tool_definitions, execute_tool, CONCURRENCY_SAFE_TOOLS, check_permission, \
    get_active_tool_definitions
from agents.ui import print_info, print_divider, print_assistant_text, print_sub_agent_start, print_sub_agent_end, \
    start_spinner, stop_spinner, print_cost, print_tool_call, print_tool_result, print_confirmation, print_retry, \
    print_error


# 指数退避重试


def _is_retryable(error: Exception) -> bool:
    """只把限流、服务过载和短暂网络错误交给指数退避重试。"""
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status in (429, 503, 529):
        return True
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
        return True
    return False


def _safe_utf8_text(value: object) -> str:
    """替换非法 Unicode 字节，避免终端输出或 SDK 序列化中断整个会话。"""
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def _sanitize_for_utf8(value: Any) -> Any:
    """递归清洗即将写入协议消息或发送给模型的容器。"""
    if isinstance(value, str):
        return _safe_utf8_text(value)
    if isinstance(value, list):
        return [_sanitize_for_utf8(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_for_utf8(item) for item in value)
    if isinstance(value, dict):
        return {
            _sanitize_for_utf8(key): _sanitize_for_utf8(item)
            for key, item in value.items()
        }
    return value


async def _with_retry(fn, max_retries: int = 3):
    """执行异步模型请求；仅对可恢复错误做带抖动的指数退避。"""
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as error:
            if attempt >= max_retries or not _is_retryable(error):
                raise
            delay = min(1000 * (2 ** attempt), 30000) / 1000 + (hash(str(time.time())) % 1000) / 1000
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            reason = f"HTTP {status}" if status else (getattr(error, "code", None) or "network error")
            print_retry(attempt + 1, max_retries, reason)
            await asyncio.sleep(delay)

MODEL_CONTEXT = {
    "claude-opus-4-6": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-sonnet-4-20250514": 200000,
    "claude-haiku-4-5-20251001": 200000,
    "claude-opus-4-20250514": 200000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "deepseek-chat":200000
}

def _get_context_windows(model:str)->int:
    """返回模型标称上下文；未知兼容模型采用项目的保守默认值。"""
    return MODEL_CONTEXT.get(model, 200000)


#多层级压缩常数
SNIP_THRESHOLD = 0.60
AUTO_COMPACT_THRESHOLD = 0.70
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell"}
MICROCOMPACT_IDLE_S = 5 * 60  # 5 minutes

KEEP_RECENT_RESULTS = 3



def _get_max_output_tokens(model: str) -> int:
    """按模型族选择单次最大输出，为 thinking 与正文共同预留空间。"""
    m = model.lower()
    if "opus-4-6" in m:
        return 64000
    if "sonnet-4-6" in m:
        return 32000
    if any(x in m for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 32000
    return 16384

#转换tool的形式到openai
def _to_openai_tools(tools: list[ToolDef]) -> list[dict]:
    """把内部 Anthropic 风格 schema 适配成 OpenAI function tools。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


class Agent:
    """同时支撑主 Agent 与隔离子 Agent 的有状态运行时。

    is_sub_agent、自定义 system prompt 和工具集合共同决定能力边界。实例同时维护
    Anthropic 与 OpenAI 两套协议消息容器，但一次运行只会使用其中一套。
    """

    def __init__(self,
                 *,
                 permission_mode:str="default",
                 model:str="deepseek-chat",
                 api_base: str | None=None,
                 anthropic_base_url: str | None=None,
                 api_key: str | None=None,
                 thinking: bool=False,
                 max_cost_usd: float | None=None,
                 max_turns: int | None=None,
                 confirm_fn:Callable[[str], Awaitable[bool]] | None=None,
                 custom_system_prompt: str | None=None,
                 custom_tools: list[ToolDef] | None=None,
                 is_sub_agent: bool=False,):
        """创建一份会话级 Runtime 状态，并按 API 协议初始化对应客户端。

        ``custom_system_prompt`` 和 ``custom_tools`` 主要供子 Agent/Skill fork 使用；
        主 Agent 默认从磁盘动态构建 prompt，并拥有完整内置工具集合。
        """
        # ── 运行策略：决定模型、权限、工具边界以及何时停止 ──
        self.permission_mode = permission_mode
        self.thinking = thinking
        self.model = model
        self.use_openai = bool(api_base)
        self.is_sub_agent = is_sub_agent
        self.tools = custom_tools or tool_definitions
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.confirm_fn = confirm_fn
        self._custom_system_prompt = custom_system_prompt
        self.effective_window=_get_context_windows(model) -20000
        self.session_id = uuid.uuid4().hex[:8]
        self.session_start_time= time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())

        # ── 计量状态：每次模型响应后累计，用于 /cost 和预算熔断 ──
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        self.current_turns = 0
        self.last_api_call_time = 0


        # ── 控制状态：中止、交互确认和“编辑前必须读取”都只在当前会话生效 ──
        self._aborted = False
        #存储异步任务
        self._current_task:asyncio.Task | None = None
        #权限白名单
        self._confirmed_paths: set[str] = set()


        # ── Plan Mode：保存进入前模式，并只允许计划文件成为写入例外 ──
        self._pre_plan_mode: str | None=None
        self._plan_file_path: str | None=None
        self._plan_approval_fn : Callable[[str], Awaitable[bool]] | None=None
        self._context_cleared : bool=False

        # thinking 模式由“用户开关 + 模型能力”共同决定，不能只看 CLI 参数。
        self._thinking_mode = self._resolve_thinking_mode()

        # ── 输出缓冲：子 Agent/run_once 收集文本，主 REPL 则直接流式打印 ──
        self._output_buffer: list[str] | None=None
        self._turn_output_buffer: list[str] | None = None

        # 编辑前读取
        self._read_file_state: dict[str, float] ={}

        # ── 外部能力：MCP 在第一次 chat 时懒连接，避免启动 CLI 就拉起子进程 ──
        self._mcp_manager = McpManager()
        self._mcp_initialized = False

        # ── 长期 Memory：去重集合与字节预算防止同一事实反复注入 ──
        #记忆agent已经回答过的信息
        self._already_surfaced_memories: set[str] = set()
        #当前会话占用的字节数
        self._session_memory_bytes = 0

        # ── 对话历史：两种 API 协议格式不同，因此分别保存，运行时只使用其中一套 ──
        self._anthropic_messages: list[str] = []
        self._openai_messages: list[str] = []
        # ── 在线 Skill：保留本轮检索证据，并等待下一轮用户反馈补全抽取窗口 ──
        self._last_retrieved_skill_reference: dict[str, Any] | None = None
        self._last_retrieved_skill_hits: list[dict[str, Any]] = []
        self._pending_skill_extraction_window: dict[str, Any] | None = None
        self._background_skill_tasks: set[asyncio.Task] = set()
        # ── Session Memory：它服务于当前任务续跑，不等同于跨会话长期 Memory ──
        # 每次上下文折叠都保留结构化记录，并维护用于提示模型的运行期触发信号。
        self._folded_session_memories: list[dict[str, Any]] = []
        self._fold_last_time: float = 0.0
        self._fold_count: int = 0
        self._tool_error_streak: int = 0
        self._same_tool_repeat_count: int = 0
        self._last_tool_name: str = ""

        # system prompt 是当前能力快照；Plan/Fold 提示在基础 prompt 之上动态拼接。
        self._base_system_prompt = custom_system_prompt or build_system_prompt()

        if self.permission_mode == "plan":
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt

        # 只初始化一种协议客户端；use_openai 同时决定消息格式和后续 Agent Loop。
        if self.use_openai:
            self._openai_client = openai.AsyncOpenAI(base_url=api_base, api_key=api_key)
            self._anthropic_client = None
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        else:
            kwargs : dict[str,Any] = {}
            if api_key:
                kwargs["api_key"] = api_key
            if anthropic_base_url:
                kwargs["base_url"] = anthropic_base_url
            self._anthropic_client = anthropic.AsyncAnthropic(**kwargs)
            self._openai_client = None

        self._refresh_runtime_system_prompt()

    #判断返回模型的思考模式
    def _resolve_thinking_mode(self) -> str:
        """把 thinking 请求降级为模型实际支持的 disabled/enabled/adaptive。"""
        if not self.thinking:
            return "disabled"
        if not self._model_supports_thinking():
            return "disabled"

        if self._mode_supports_adaptive_thinking(self.model):
            return "adaptive"
        return "enabled"

    def _model_supports_thinking(self) -> bool:
        """按模型名做兼容性判断，避免向旧模型发送不支持的参数。"""
        m = self.model.lower()
        if "claude-3-" in m or "3-5-" in m or "3-7-" in m:
            return False
        if "claude" in m and any(x in m for x in ("opus", "sonnet", "haiku")):
            return True
        return False
    def _model_supports_adaptive_thinking(self) -> bool:
        """判断是否可由模型自行调节 thinking 预算。"""
        m = self.model.lower()
        return "opus-4-6" in m or "sonnet-4-6" in m

    #生成一个用于保存 AI 计划（Plan）的 Markdown 文件的绝对路径。
    def _generate_plan_file_path(self) -> str:
        """为本会话生成唯一计划文件，作为 Plan Mode 唯一可写例外。"""
        d = Path.home() / ".bear" / "plans"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"plan-{self.session_id}.md")

    def _build_plan_mode_prompt(self) -> str:
        """生成只读规划约束，并把本会话计划文件路径明确告知模型。"""
        return f"""

    # Plan Mode Active

    Plan mode is active. You MUST NOT make any edits (except the plan file below), run non-readonly tools, or make any changes to the system.

    ## Plan File: {self._plan_file_path}
    Write your plan incrementally to this file using write_file or edit_file. This is the ONLY file you are allowed to edit.

    ## Workflow
    1. **Explore**: Read code to understand the task. Use read_file, list_files, grep_search.
    2. **Design**: Design your implementation approach. Use the agent tool with type="plan" if the task is complex.
    3. **Write Plan**: Write a structured plan to the plan file including:
       - **Context**: Why this change is needed
       - **Steps**: Implementation steps with critical file paths
       - **Verification**: How to test the changes
    4. **Exit**: Call exit_plan_mode when your plan is ready for user review.

    IMPORTANT: When your plan is complete, you MUST call exit_plan_mode. Do NOT ask the user to approve — exit_plan_mode handles that."""

    #判断当前的任务所有的任务是否完成
    @property
    def is_processing(self)->bool:
        return self._current_task is not None and not self._current_task.done()

    #大模型调用的工厂方法,构建一个用于记忆召回（memory recall）的 sideQuery 可调用对象，兼容anthropic, openai。
    def _build_side_query(self, *, max_tokens: int = 256):
        """构造不污染主历史的轻量模型调用，供 Memory/Skill 判定复用。

        side query 使用相同客户端和模型，但只携带专用 system/user prompt；因此召回、
        折叠和评审结果不会把辅助对话写进主 Agent Loop。
        """
        if self._anthropic_client:
            client = self._anthropic_client
            model = self.model
            async def _sq(system:str, user_message:str)->str:

                resp = await client.messages.create(
                    model=model, max_tokens=max(1, int(max_tokens)), system=system,
                messages=[{"role": "user", "content": user_message}],
                )
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                if not text.strip():
                    block_types = [str(getattr(b, "type", "")) for b in getattr(resp, "content", [])]
                    logging.warning(
                        "side_query returned empty Anthropic-compatible response: model=%s stop_reason=%s content_block_types=%s",
                        getattr(resp, "model", model),
                        getattr(resp, "stop_reason", ""),
                        block_types,
                    )
                return text
            return _sq
        if self._openai_client:
            client = self._openai_client
            model = self.model
            async def _sq_openai(system:str, user_message:str)->str:
                resp = await client.chat.completions.create(
                    model=model,
                    max_tokens=max(1, int(max_tokens)),
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],

                )
                if not resp.choices:
                    logging.warning("side_query returned no OpenAI-compatible choices: model=%s", model)
                    return ""
                choice = resp.choices[0]
                content = choice.message.content or ""
                if not content.strip():
                    logging.warning(
                        "side_query returned empty OpenAI-compatible response: model=%s finish_reason=%s message=%s",
                        model,
                        getattr(choice, "finish_reason", ""),
                        choice.message,
                    )
                return content
            return _sq_openai
        return None
    #异步任务取消（Abort）
    def abort(self) -> None:
        """标记当前循环中止，并取消正在等待的模型任务。"""
        self._aborted = True
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def set_confirm_fn(self, fn:Callable[[str], Awaitable[bool]]) -> None:
        self.confirm_fn = fn

    def set_plan_approval_fn(self, fn:Callable[[str], Awaitable[bool]]) -> None:
        self._plan_approval_fn = fn


    #计划模式开关（“状态切换与现场保护”机制）
    def toggle_plan_mode(self) -> str:
        """
               1. 退出计划模式（从 plan 切回原模式）
               当当前模式已经是 plan 时，执行 if 分支：
               恢复之前的状态：self.permission_mode = self._pre_plan_mode or "default"。
                   在进入计划模式时，程序会把原本的模式保存在 _pre_plan_mode 里。退出时，就把它重新拿出来赋值回去，恢复到切换前的状态。
               清理计划模式的痕迹：把 _pre_plan_mode 和 _plan_file_path（计划文件路径）清空，并将系统提示词 _system_prompt 恢复为最基础的 _base_system_prompt。
               同步 OpenAI 消息：如果底层使用的是 OpenAI 接口，它还会同步更新消息列表里的第一条系统提示词，确保 AI 的上下文也跟着切换回来。
               反馈返回：打印退出提示，并返回恢复后的模式名称。

               2. 进入计划模式（从其他模式切入 plan）
       当当前模式不是 plan 时，执行 else 分支：
       保护当前现场：self._pre_plan_mode = self.permission_mode。先把当前正在使用的模式（比如正常模式或自动接受模式）暂存起来，方便以后能原路返回。
       切换并初始化：将当前模式设为 "plan"，生成一个专属的计划文件路径，并扩展系统提示词。通过拼接 _build_plan_mode_prompt()，给 AI 注入“只动脑不动手、输出结构化计划”的专属指令。
       同步 OpenAI 消息：同样地，如果使用 OpenAI，也会实时更新上下文里的系统提示词。
       反馈与返回：打印进入提示（包含计划文件的路径），并返回 "plan"。
        """
        if self.permission_mode == "plan":
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] =self._system_prompt
            print_info(f"Exited plan mode -> {self.permission_mode} mode")
            return self.permission_mode
        else:
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            print_info(f"Entered plan mode. Plan file: {self._plan_file_path}")
            return "plan"

    def get_token_usage(self) -> dict:
        """返回包含子 Agent 消耗在内的会话累计 token。"""
        return {"input":self.total_input_tokens, "output":self.total_output_tokens}

    #主入口

    async def  chat(self, user_message:str)->None:
        """执行一轮完整对话，并在主回复后调度 Skill 反馈与演化任务。

        original_user_message 始终保留纯用户输入；实际发给模型的 user_message 可能
        追加检索到的 Skill 上下文。MCP 在主 Agent 的第一次对话中懒加载。

        从调用关系看，CLI/REPL 只负责把输入交给这里；这里准备运行时能力后，再把控制权
        交给某个协议循环。协议循环可能请求模型多次，所以“一次 chat”不等于“一次 API
        请求”，而是直到模型给出最终文本为止的一整轮 Agent 任务。
        """
        # 阶段 1：首次对话发现 MCP 工具；子 Agent 不再重复创建 MCP 子进程。
        if not self._mcp_initialized and not self.is_sub_agent:
            self._mcp_initialized = True
            try:
                await self._mcp_manager.load_and_connect()
                mcp_defs = self._mcp_manager.get_tool_definitions()
                if mcp_defs:
                    self.tools = self.tools + mcp_defs
            except Exception as e:
                print_error(f"MCP init failed: {e}")

        # 阶段 2：保留纯用户输入用于审计，同时给实际模型输入追加相关 Skill 摘要。
        original_user_message = _safe_utf8_text(user_message)
        ready_skill_extraction_window: dict[str, Any] | None = None
        self._last_retrieved_skill_reference = None
        self._last_retrieved_skill_hits = []
        if not self.is_sub_agent:
            ready_skill_extraction_window = self._pop_pending_skill_extraction_window(original_user_message)
            user_message, self._last_retrieved_skill_reference = self._augment_user_message_with_skill_context(
                original_user_message
            )

        # 阶段 3：选择协议循环。两条循环语义相同，但消息/tool result 格式不同。
        # create_task 让 abort() 可以持有并取消整条 Agent 执行链，而不只是停止终端输出。
        self._aborted = False
        self._turn_output_buffer = []
        coro = self._chat_openai(user_message) if self.use_openai else self._chat_anthropic(user_message)
        self._current_task = asyncio.create_task(coro)
        try:
            await self._current_task
        except asyncio.CancelledError:
            self._aborted = True

        finally:
            self._current_task = None
        # 阶段 4：主回复完成后再做统计与在线演化，避免辅助模型请求增加响应延迟。
        # _emit_text() 在本轮 Agent Loop 中持续写入该缓冲区；这里将流式文本片段
        # 合并为完整回复，供 Skill 使用情况评估和下一轮的演化证据窗口复用。
        assistant_text = "".join(self._turn_output_buffer or []).strip()
        # 本轮收集已经结束，及时置空可避免后续非 chat 输出被误计入本轮回复。
        self._turn_output_buffer = None
        # 子 Agent 不负责全局 Skill 学习；被用户中止的回复也不应作为有效样本。
        if not self.is_sub_agent and not self._aborted:
            # 把任务丢进后台异步执行，不阻塞主对话响应, 后台判断本轮自动检索出的 Skill 是否相关、是否真正被模型采用。
            self._schedule_background_skill_task(self._run_skill_usage_tracking(original_user_message, assistant_text))
            # ready_skill_extraction_window 属于上一轮：当前用户输入已经作为对上一轮结果的
            # 反馈补入窗口，因此现在可以异步提炼或演化可复用的 Skill。
            if ready_skill_extraction_window:
                self._schedule_background_skill_task(self._run_online_skill_evolution(ready_skill_extraction_window))
            # 暂存当前问答以及本轮最高分 Skill 引用；等下一条用户消息到来后，
            # 再把那条消息视为结果反馈，组成下一次在线演化的完整证据窗口。
            self._set_pending_skill_extraction_window(
                original_user_message=original_user_message,
                assistant_text=assistant_text,
                retrieved_reference=self._last_retrieved_skill_reference,
            )
        # 只有主 Agent 负责终端轮次分隔和会话持久化，避免子 Agent 污染主界面与存档。
        if not self.is_sub_agent:
            print_divider()
            self._auto_save()



   #执行一次对话，收集本轮模型输出文本，并返回本轮消耗的 token 数
    async def run_once(self, prompt:str)->None:
        """以非交互方式执行一轮，并返回文本和本轮 token 增量。

        子 Agent 和 fork Skill 依赖这个接口把结果交还父 Agent，而不是直接写终端。
        """
        self._output_buffer = []
        prev_in = self.total_input_tokens
        prev_out = self.total_output_tokens
        await self.chat(prompt)
        text = "".join(self._output_buffer)
        self._output_buffer = None
        return {
            "text": text,
            "tokens":{
                "input":self.total_input_tokens-prev_in,
                "output":self.total_output_tokens-prev_out
            },
        }

    #输出工具：统一处理模型输出文本。根据当前是否处于“收集输出”的模式
    # 决定是把文本存进缓冲区，还是直接打印到终端。
    def _emit_text(self, text:str)->None:
        text = _safe_utf8_text(text)
        if self._turn_output_buffer is not None:
            self._turn_output_buffer.append(text)
        if self._output_buffer is not None:
            self._output_buffer.append(text)
        else:
            print_assistant_text(text)

    def _build_fold_guidance_section(self) -> str:
        """把上下文占用、工具失败和重复调用信号追加到动态 system prompt。"""
        if self._custom_system_prompt is not None:
            return ""
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0.0
        last_fold = "never" if not self._fold_last_time else f"{int((time.time() - self._fold_last_time) / 60)}m ago"
        return (
            "\n\n# Runtime Fold Guidance\n"
            f"- Current context utilization: {utilization:.0%}\n"
            f"- Recent tool error streak: {self._tool_error_streak}\n"
            f"- Same tool repeat count: {self._same_tool_repeat_count}\n"
            f"- Last fold: {last_fold}\n"
            "- If the context is getting long, the same tool is being retried without progress, or tool failures are accumulating, call `compact_context` before trying more tools.\n"
            "- If you folded very recently and the next step is clear, prefer continuing rather than folding again.\n"
        )

    def _refresh_runtime_system_prompt(self) -> None:
        """重新扫描动态能力，并同步当前 Plan/Fold 状态到 system prompt。"""
        if self._custom_system_prompt is not None:
            return
        self._base_system_prompt = build_system_prompt()
        if self.permission_mode == "plan":
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt
        self._system_prompt += self._build_fold_guidance_section()
        if self.use_openai and self._openai_messages:
            self._openai_messages[0]["content"] = self._system_prompt

    def _record_tool_outcome(self, tool_name: str, success: bool) -> None:
        """累计连续失败与同名工具重复次数，供模型判断是否应主动折叠。"""
        if tool_name == self._last_tool_name:
            self._same_tool_repeat_count += 1
        else:
            self._same_tool_repeat_count = 1
        self._last_tool_name = tool_name
        if success:
            self._tool_error_streak = 0
        else:
            self._tool_error_streak += 1

    def _record_fold_event(self) -> None:
        """记录折叠时间并清空已经被折叠吸收的工具异常信号。"""
        self._fold_last_time = time.time()
        self._fold_count += 1
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""

    def _looks_like_tool_failure(self, tool_name: str, raw: str, result: str) -> bool:
        """从统一工具文本中做保守的失败标记；结果仅用于提示，不决定执行成败。"""
        text = f"{raw}\n{result}".lower()
        if any(marker in text for marker in ("error", "denied", "timed out", "timeout")):
            return True
        if tool_name == "compact_context" and "no context compaction" in text:
            return True
        return False

    def _augment_user_message_with_skill_context(self, user_message: str) -> tuple[str, dict[str, Any] | None]:
        """检索最多三个相关 Skill，将摘要注入输入并保留命中证据。"""
        try:
            from .skills import format_retrieved_skill_context

            context, top_ref = format_retrieved_skill_context(user_message, limit=3)
        except Exception:
            return user_message, None
        if top_ref and isinstance(top_ref.get("all_hits"), list):
            self._last_retrieved_skill_hits = list(top_ref.get("all_hits") or [])
        if not context.strip():
            return user_message, top_ref
        return f"{user_message}\n\n{context}", top_ref

    def _strip_runtime_injections(self, text: str) -> str:
        """移除 Runtime 添加的 Skill 上下文，避免其被误当作用户反馈学习。"""
        return re.sub(r"\n*<retrieved_skills>.*?</retrieved_skills>\s*", "", str(text or ""), flags=re.DOTALL).strip()

    def _message_text(self, msg: dict[str, Any]) -> str:
        content = msg.get("content")
        if isinstance(content, str):
            return self._strip_runtime_injections(content)
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(str(block.get("text") or ""))
                    elif "content" in block and block.get("type") not in {"tool_result", "tool_use"}:
                        parts.append(str(block.get("content") or ""))
            return self._strip_runtime_injections("\n".join(parts))
        return ""

    def _recent_dialog_messages(self, *, max_messages: int = 8) -> list[dict[str, str]]:
        """抽取不含工具块的近期对话，作为在线 Skill 抽取的最小证据窗口。"""
        raw_messages = self._openai_messages if self.use_openai else self._anthropic_messages
        out: list[dict[str, str]] = []
        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            text = self._message_text(msg)
            if text:
                out.append({"role": role, "content": text})
        return out[-max(2, int(max_messages)) :]

    async def _confirm_online_skill_write(self, summary: str) -> bool:
        if self.permission_mode in {"bypassPermissions", "acceptEdits"}:
            return True
        if self.permission_mode in {"plan", "dontAsk"}:
            return False
        if self.confirm_fn is None:
            return False
        print_confirmation(summary)
        try:
            return bool(await self.confirm_fn(summary))
        except Exception:
            return False

    async def _confirm_background_online_skill_write(self, summary: str) -> bool:
        return self.permission_mode in {"bypassPermissions", "acceptEdits"}

    def _online_evolution_enabled(self) -> bool:
        raw = os.environ.get("BEAR_AUTO_SKILL_EVOLUTION", "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    def _schedule_background_skill_task(self, coro) -> None:
        """托管在线 Skill 后台任务；Plan Mode 中直接关闭协程以保持只读。"""
        if self.permission_mode == "plan":
            try:
                coro.close()
            except Exception:
                pass
            return
        task = asyncio.create_task(coro)
        self._background_skill_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self._background_skill_tasks.discard(done_task)
            try:
                done_task.result()
            except Exception:
                pass

        task.add_done_callback(_done)

    async def drain_background_skill_tasks(self) -> None:
        """等待尚未结束的 Skill 统计/演化任务，供 CLI 退出前安全收尾。"""
        tasks = [task for task in self._background_skill_tasks if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _pop_pending_skill_extraction_window(self, next_user_feedback: str) -> dict[str, Any] | None:
        """用下一轮反馈补全上一轮对话窗口，再交给在线 Skill 抽取器。"""
        pending = self._pending_skill_extraction_window
        self._pending_skill_extraction_window = None
        if not pending:
            return None
        messages = list(pending.get("messages") or [])
        feedback = _safe_utf8_text(next_user_feedback).strip()
        if feedback:
            messages.append({"role": "user", "content": feedback})
        pending["messages"] = messages[-10:]
        pending["next_user_feedback"] = feedback
        return pending

    def _set_pending_skill_extraction_window(
        self,
        *,
        original_user_message: str,
        assistant_text: str,
        retrieved_reference: dict[str, Any] | None,
    ) -> None:
        """暂存本轮问答，下一轮用户消息将作为结果反馈补入该窗口。"""
        if not original_user_message.strip() or not assistant_text.strip():
            return
        self._pending_skill_extraction_window = {
            "messages": self._recent_dialog_messages(max_messages=8),
            "latest_user": original_user_message,
            "latest_assistant": assistant_text,
            "retrieved_reference": self._compact_retrieved_reference(retrieved_reference),
            "session_id": self.session_id,
        }

    def _compact_retrieved_reference(self, ref: dict[str, Any] | None) -> dict[str, Any] | None:
        if not ref:
            return None
        return {k: v for k, v in ref.items() if k != "all_hits"}

    async def _run_online_skill_evolution(self, window: dict[str, Any], *, interactive_confirm: bool = False) -> None:
        """把完整反馈窗口交给 Extractor/Maintainer，并在写入后刷新能力快照。"""
        if not self._online_evolution_enabled() or self.permission_mode == "plan":
            return
        messages = list(window.get("messages") or [])
        if not messages:
            return

        side_query = self._build_side_query(max_tokens=2200)
        if side_query is None:
            return

        try:
            from .online_skill_evolution import online_ingest
        except Exception:
            return

        result = await online_ingest(
            messages=messages, # 「问题 + agent 回答 + 用户反馈」
            side_query=side_query,
            retrieved_reference=window.get("retrieved_reference") or None,
            hint=str(window.get("hint") or ""),
            confirm_write=self._confirm_online_skill_write if interactive_confirm else self._confirm_background_online_skill_write,
            target=os.environ.get("BEAR_AUTO_SKILL_TARGET", "project"),
        )
        if result.get("ok"):
            if result.get("action") in {"add", "merge"}:
                self._refresh_runtime_system_prompt()
                print_info(f"Online skill {result.get('action')}: {result.get('skill')}")
        elif result.get("action") not in {"add_denied", "merge_denied"}:
            print_error(f"Online skill evolution failed: {result.get('error') or result}")

    async def _run_skill_usage_tracking(self, original_user_message: str, assistant_text: str) -> None:
        """让独立裁判模型评估检索命中的 Skill，并更新可审计的累计统计。

        这里的 ``used`` 是根据最终回答是否体现 Skill 的独特流程推断出来的，不代表
        Runtime 已确认主模型实际调用过 ``skill`` 工具；显式调用由 invocation 日志另记。
        """
        # 在线演化关闭时不产生任何辅助请求；Plan Mode 保持只读，也不更新统计文件。
        if not self._online_evolution_enabled() or self.permission_mode == "plan":
            return
        # 这些命中来自本轮请求前的自动检索，包含名称、描述、when_to_use、检索分数等，
        # 但不包含完整 SKILL.md 正文。
        hits = list(self._last_retrieved_skill_hits or [])
        # 没有候选就无从判断；空回复通常表示模型未正常完成，也不作为有效使用样本。
        if not hits or not assistant_text.strip():
            return
        # side_query 使用相同模型发起一条与主对话历史隔离的辅助请求；700 token
        # 只供输出结构化判断，不会把裁判过程写回主 Agent 的消息列表。
        side_query = self._build_side_query(max_tokens=700)
        try:
            from .online_skill_evolution import judge_retrieved_skill_usage
            from .skills import record_usage_judgments

            # 裁判同时看到原始用户请求、最终回复和候选摘要，并为每个候选返回
            # relevant（是否适用）与 used（回答是否体现其特有流程）。
            judgments = await judge_retrieved_skill_usage(
                hits=hits,
                user_message=original_user_message,
                assistant_text=assistant_text,
                side_query=side_query,
            )
            # 将本轮判断累加到 skill_usage_stats.json；达到长期无效阈值时可能归档 Skill。
            result = record_usage_judgments(judgments)
            if result.get("pruned"):
                # 归档会改变当前可用 Skill 清单，因此必须重建 system prompt。
                self._refresh_runtime_system_prompt()
        except Exception:
            # 统计属于回答后的尽力而为任务，裁判或写盘失败不能影响已经完成的主回复。
            return

    async def extract_now(self, hint: str = "") -> dict[str, Any]:
        """手动消费 pending window；用于 REPL 的 /extract_now 命令。"""
        pending = self._pending_skill_extraction_window
        if not pending:
            return {"ok": False, "error": "no pending online skill extraction window"}
        window = dict(pending)
        window["hint"] = hint
        await self._run_online_skill_evolution(window, interactive_confirm=True)
        self._pending_skill_extraction_window = None
        return {"ok": True}


    def clear_history(self)->None:
        """清空协议历史和会话统计，但保留客户端、工具与当前权限配置。"""
        self._anthropic_messages = []
        self._openai_messages = []
        self._pending_skill_extraction_window = None
        self._last_retrieved_skill_reference = None
        self._last_retrieved_skill_hits = []
        self._fold_last_time = 0.0
        self._fold_count = 0
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""
        if self.use_openai:
            self._openai_messages.append({"role": "system", "content":self._system_prompt})
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        print_info("Conversation cleared.")

    def show_cost(self):
        total = self._get_current_cost_usd()
        budget_info = f" / ${self.max_cost_usd} budget" if self.max_cost_usd else ""
        turn_info = f" | Turns: {self.current_turns}/{self.max_turns}" if self.max_turns else ""
        print_info(
            f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out\n  Estimated cost: ${total:.4f}{budget_info}{turn_info}")

    #获取当前的花费，
    def _get_current_cost_usd(self) -> float:
        return (self.total_input_tokens / 1_000_000) * 3 + (self.total_output_tokens / 1_000_000) * 15

    #检查预算
    def _check_budget(self) -> dict:
        if self.max_cost_usd is not None and self._get_current_cost_usd() >= self.max_cost_usd:
            return {"exceeded": True, "reason": f"Cost limit reached (${self._get_current_cost_usd():.4f} >= ${self.max_cost_usd})"}
        if self.max_turns is not None and self.current_turns >= self.max_turns:
            return {"exceeded": True, "reason": f"Turn limit reached ({self.current_turns} >= {self.max_turns})"}
        return {"exceeded": False}

    #压缩会话
    async def compact(self)->None:
        """处理 REPL 的手动压缩请求；历史不足时只提示而不改写消息。"""
        compacted = await self._compact_conversation(trigger="manual")
        if not compacted:
            print_info("Nothing to compact yet.")


    #恢复会话信息
    def restore_session(self, data:dict)->None:
        """恢复持久化消息与 folding 记录；协议消息会在使用前再次规范化。"""
        if data.get("anthropicMessages"):
            self._anthropic_messages = self._normalize_anthropic_messages(_sanitize_for_utf8(data["anthropicMessages"]))
        if data.get("openaiMessages"):
            self._openai_messages = _sanitize_for_utf8(data["openaiMessages"])
        if isinstance(data.get("foldedSessionMemories"), list):
            self._folded_session_memories = _sanitize_for_utf8(data["foldedSessionMemories"])
        print_info(f"Session restored ({self._get_message_count()} messages).")



#整理 Anthropic 的历史消息，修正部分角色错误，并丢弃不合法的工具调用消息。
    def _normalize_anthropic_messages(self, messages: list[dict]) -> list[dict]:
        role_normalized = []
        for msg in messages:
            copied = dict(msg)
            content = copied.get("content")
            if copied.get("role") == "user" and isinstance(content, list):
                if any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content):
                    copied["role"] = "assistant"
            role_normalized.append(copied)

        normalized = []
        i = 0
        while i < len(role_normalized):
            msg = role_normalized[i]
            tool_use_ids = self._anthropic_tool_use_ids(msg)
            if not tool_use_ids:
                normalized.append(msg)
                i += 1
                continue

            next_msg = role_normalized[i + 1] if i + 1 < len(role_normalized) else None
            result_ids = self._anthropic_tool_result_ids(next_msg) if next_msg else set()
            if tool_use_ids.issubset(result_ids):
                normalized.append(msg)
                normalized.append(next_msg)
                i += 2
                continue

            i += 1
        return normalized

    @staticmethod
    def _anthropic_tool_use_ids(msg: dict | None) -> set[str]:
        if not msg or msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
            return set()
        return {
            block.get("id")
            for block in msg["content"]
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
        }

    @staticmethod
    def _anthropic_tool_result_ids(msg: dict | None) -> set[str]:
        if not msg or msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            return set()
        return {
            block.get("tool_use_id")
            for block in msg["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id")
        }

    def _get_message_count(self) -> int:
        return len(self._openai_messages) if self.use_openai else len(self._anthropic_messages)

    def _auto_save(self) -> None:
        try:
            save_session(self.session_id, {
                "metadata": {
                    "id": self.session_id,
                    "model": self.model,
                    "cwd": str(Path.cwd()),
                    "startTime": self.session_start_time,
                    "messageCount": self._get_message_count(),
                },
                "anthropicMessages": _sanitize_for_utf8(self._anthropic_messages) if not self.use_openai else None,
                "openaiMessages": _sanitize_for_utf8(self._openai_messages) if self.use_openai else None,
                "foldedSessionMemories": _sanitize_for_utf8(self._folded_session_memories),
            })
        except Exception:
            pass

    #自动压缩
    async def _check_and_compact(self)->None:
        """输入 token 超过有效窗口阈值后触发自动结构化折叠。"""
        if self.last_input_token_count > self.effective_window * AUTO_COMPACT_THRESHOLD:
            print_info("Context window filling up, compacting conversation...")
            await self._compact_conversation(trigger="auto")

    async def _compact_conversation(self, *, trigger: str = "manual")->bool:
        """按当前协议折叠上下文，并返回本次是否真的发生压缩。"""
        if self.use_openai:
            compacted = await self._compact_openai(trigger=trigger)
        else:
            compacted = await self._compact_anthropic(trigger=trigger)
        if compacted:
            print_info("Conversation compacted.")
        return compacted

    async def _compact_anthropic(self, *, trigger: str)->bool:
        """将 Anthropic 原始历史替换为一条结构化 Session Memory 用户消息。"""
        if len (self._anthropic_messages)<4:
            return False

        transcript = build_anthropic_transcript(_sanitize_for_utf8(self._anthropic_messages))
        if not transcript.strip():
            return False
        memory = await self._generate_folded_session_memory(transcript)
        self._record_folded_session_memory(trigger, memory)
        self._record_fold_event()
        self._anthropic_messages = [{"role": "user", "content": format_folded_memory(memory)}]
        self.last_input_token_count = 0
        self._refresh_runtime_system_prompt()
        return True

    async def _compact_openai(self, *, trigger: str)->bool:
        """保留 OpenAI system 消息，其余历史折叠为结构化用户消息。"""
        if len (self._openai_messages)<4:
            return False
        system_msg = self._openai_messages[0]
        transcript = build_openai_transcript(_sanitize_for_utf8(self._openai_messages))
        if not transcript.strip():
            return False
        memory = await self._generate_folded_session_memory(transcript)
        self._record_folded_session_memory(trigger, memory)
        self._record_fold_event()
        self._openai_messages=[
            system_msg,
            {"role": "user", "content": format_folded_memory(memory)},
        ]
        self.last_input_token_count=0
        self._refresh_runtime_system_prompt()
        return True

    async def _generate_folded_session_memory(self, transcript: str) -> dict[str, Any]:
        """调用同模型 side query 生成折叠 JSON，失败时退回截断转录。"""
        side_query = self._build_side_query(max_tokens=6000)
        if side_query is None:
            return fallback_folded_memory(transcript)
        try:
            raw = await side_query(FOLD_SESSION_MEMORY_SYSTEM, build_folding_user_prompt(transcript))
            return parse_folded_memory(raw)
        except Exception:
            return fallback_folded_memory(transcript)

    def _record_folded_session_memory(self, trigger: str, memory: dict[str, Any]) -> None:
        """同时把折叠记录保存到内存会话状态和项目级审计文件。"""
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trigger": trigger,
            "session_id": self.session_id,
            **memory,
        }
        self._folded_session_memories.append(record)
        try:
            save_folded_session_memory(self.session_id, _sanitize_for_utf8(record))
        except Exception:
            pass

    #多层级压缩流水线
    def _run_compression_pipeline(self)->None:
        if self.use_openai:
            self._budget_tool_results_openai()
            self._snip_stale_results_openai()
            self._microcompact_openai()
        else:
            self._budget_tool_results_anthropic()
            self._snip_stale_results_anthropic()
            self._microcompact_anthropic()

    #第一层级压缩，预算压缩
    def _budget_tool_results_anthropic(self)->None:
        #计算利用率：utilization = 已用Token / 有效窗口大小。
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        #如果利用率低于 50%，说明空间还很充裕，直接返回，不做任何处理。
        if utilization < 0.5:
            return
        #动态预算（Budget）：危急状态（>70%）：如果利用率很高，允许单个工具结果保留 15,000 个字符。
        # 警戒状态（50%-70%）：如果利用率中等，只允许保留 30000 个字符。
        budget = 15000 if utilization > 0.7 else 30000

        for msg in self._anthropic_messages:

            #只处理 role 为 "user" 的消息。在工具调用流程中，工具的执行结果通常是以“用户”的身份反馈给模型的。

            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and len(block["content"]) > budget:
                    #计算保留长度 (keep)：keep = (budget - 80) // 2 这里预留了约 80 个字符的空间给中间的提示语，剩下的长度平分给开头和结尾。
                    keep = (budget - 80) // 2
                    #重组新内容 = 开头部分 + 提示语 + 结尾部分
                    block["content"] = block["content"][:keep] + f"\n\n[... budgeted: {len(block['content']) - keep * 2} chars truncated ...]\n\n" + block["content"][-keep:]

    def _budget_tool_results_openai(self)->None:
        #计算利用率：utilization = 已用Token / 有效窗口大小。
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        #如果利用率低于 50%，说明空间还很充裕，直接返回，不做任何处理。
        if utilization < 0.5:
            return
        #动态预算（Budget）：危急状态（>70%）：如果利用率很高，允许单个工具结果保留 15,000 个字符。
        # 警戒状态（50%-70%）：如果利用率中等，只允许保留 30000 个字符。
        budget = 15000 if utilization > 0.7 else 30000

        for msg in self._openai_messages:
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and len(msg["content"]) > budget:
                keep = (budget - 80) // 2
                msg["content"] = msg["content"][:keep] + f"\n\n[... budgeted: {len(msg['content']) - keep * 2} chars truncated ...]\n\n" + msg["content"][-keep:]


    #第二级策略：修剪过期的工具执行结果
    def _snip_stale_results_anthropic(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return
        results = []
        for mindex,  msg in enumerate(self._anthropic_messages):
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue

            for bindex, block in enumerate(msg["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] != SNIP_PLACEHOLDER:
                    tool_use_id = block.get("tool_use_id")
                    # 对每个 tool_result，通过 tool_use_id 反查它来自哪个工具
                    tool_info = self._find_tool_use_by_id(tool_use_id)
                    if tool_info and tool_info["name"] in SNIPPABLE_TOOLS:
                        results.append({"mindex": mindex, "bindex": bindex, "name": tool_info["name"], "file_path": tool_info.get("input", {}).get("file_path")})

        if len(results) <= KEEP_RECENT_RESULTS:
            return

        to_snip =  set()
        seen_files: dict[str, list[int]] = {}

        for i, r in enumerate(results):
            if r["name"] == "read_file" and r.get("file_path"):
                seen_files.setdefault(r["file_path"], []).append(i)
        #如果一个文件被读取了多次，只保留最后一次读取的结果，把前面几次读取的内容全部标记为“修剪”（Snip）。
        for indices in seen_files.values():
            if len (indices) >1 :
                for j in indices[:-1]:
                    to_snip.add (j)

        snip_before = len(results) - KEEP_RECENT_RESULTS
        for i in range (snip_before):
            to_snip.add(i)

        for idx in to_snip:
            r = results[idx]
            self._anthropic_messages[r["mindex"]]["content"][r["bindex"]]["content"] = SNIP_PLACEHOLDER

    def _snip_stale_results_openai(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return
        tool_msgs = []
        for i, msg in enumerate(self._openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] != SNIP_PLACEHOLDER:
                tool_msgs.append(i)
        if len(tool_msgs) <= KEEP_RECENT_RESULTS:
            return
        snip_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(snip_count):
            self._openai_messages[tool_msgs[i]]["content"] = SNIP_PLACEHOLDER

    #微压缩

    #基于“时间”的上下文瘦身策略，
    #如果已经很久没说话了，说明之前的工具执行结果你已经看完了，那就把它们清理掉，腾出空间

    def _microcompact_anthropic(self) -> None:
        if not self.last_api_call_time or (time.time() - self.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return

        all_results = []
        for mindex, msg in enumerate(self._anthropic_messages):
            if msg.get("role")!="user" or not isinstance(msg.get("content"), list):
                continue
            for bindex, block in enumerate(msg["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                    all_results.append((mindex, bindex))

        clear_count = len(all_results) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            mi, bi = all_results[i]
            self._anthropic_messages[mi]["content"][bi]["content"] = "[Old result cleared]"

    def _microcompact_openai(self) -> None:
        if not self.last_api_call_time or (time.time() - self.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        tool_msgs = []
        for i, msg in enumerate(self._openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                tool_msgs.append(i)
        clear_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            self._openai_messages[tool_msgs[i]]["content"] = "[Old result cleared]"

    def _find_tool_use_by_id(self, tool_use_id: int) -> dict | None:
        for msg in self._anthropic_messages:
            if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
                continue

            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") == tool_use_id:
                    return {"name": block["name"], "input": block.get("input", {})}

    #大结果持久化
    #如果工具返回的结果太大（超过 30KB），不要硬塞进上下文里，而是把它存成一个临时文件。
    # 然后在对话里只留一个‘文件路径’和‘内容预览’。如果模型后面还需要看完整内容，它可以再次调用工具去读取这个文件

    def _persist_large_result(self, tool_name: str, result: str) -> str:
        THRESHOLD = 30 * 1024  # 30 KB
        #转换成字节
        if (len (result.encode())) <= THRESHOLD:
            return result

        d = Path.home() / ".bear-code" / "tool-results"
        d.mkdir(parents=True, exist_ok=True)
        filename = f"{int(time.time() * 1000)}-{tool_name}.txt"
        filepath = d / filename
        filepath.write_text(result, encoding="utf-8")

        lines = result.split("\n")
        preview = "\n".join(lines[:200])
        size_kb = len(result.encode()) / 1024

        return (
            f"[Result too large ({size_kb:.1f} KB, {len(lines)} lines). "
            f"Full output saved to {filepath}. "
            f"You can use read_file to see the full result.]\n\n"
            f"Preview (first 200 lines):\n{preview}"
        )

    #执行工具入口(cy)

    async def _execute_tool_call(self, name: str, inp: dict) -> str:
        """统一分发折叠、Plan、子 Agent、Skill、MCP 与普通内置工具。

        这层是工具路由器，不负责理解模型意图。外层协议循环已经解析参数并完成权限检查；
        这里根据工具名选择真正的 Python 实现，最后统一返回字符串。该字符串随后会被包装成
        ``tool_result`` 或 ``role=tool`` 消息，成为模型下一次 API 请求的新观察结果。
        """
        # 这些工具要读写 Agent 自身状态，因此留在 Runtime 内处理，不能当成普通文件工具。
        if name == "compact_context":
            return await self._execute_compact_context_tool(inp)
        if name in ("enter_plan_mode", "exit_plan_mode"):
            return await self._execute_plan_mode_tool(name)
        if name == "agent":
            return await self._execute_agent_tool(inp)
        if name == "skill":
            return await self._execute_skill_tool(inp)
        # MCP 工具由外部 Server 实现；本进程只通过 McpManager 转发名称和参数。
        if self._mcp_manager.is_mcp_tool(name):
            return await self._mcp_manager.call_tool(name, inp)
        # 其余名称落到 tools.py：这里包含读写文件、搜索、Shell 等内置能力。
        result = await execute_tool(name, inp, self._read_file_state)
        if name in {"skill_create", "skill_evolve"}:
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and parsed.get("ok"):
                    self._refresh_runtime_system_prompt()
            except Exception:
                pass
        return result

    async def _execute_compact_context_tool(self, inp: dict) -> str:
        """执行模型主动折叠，并通知当前工具循环停止使用已经被替换的历史。"""
        reason = str(inp.get("reason") or "").strip()
        compacted = await self._compact_conversation(trigger="tool")
        if not compacted:
            self._record_tool_outcome("compact_context", False)
            return "No context compaction was performed because there is not enough conversation history yet."
        self._record_tool_outcome("compact_context", True)
        self._context_cleared = True
        suffix = f"\nReason: {reason}" if reason else ""
        return (
            "Context compacted into structured session memory. "
            "Continue from the folded memory now present in the conversation context."
            f"{suffix}"
        )


    async def _execute_skill_tool(self, inp: dict) -> str:
        from .skills import execute_skill
        result = execute_skill(inp.get("skill_name", ""), inp.get("args", ""))

        if not result:
            return f"Unknown skill: {inp.get('skill_name', '')}"

        #fork 表示这个 skill 不直接把 prompt 塞回当前对话，而是要启动一个子 Agent 单独完成任务。
        if result["context"] == "fork":
            # result["allowed_tools"] - 直接访问
            tools = (
                [t for t in self.tools if t["name"] in  result["allowed_tools"] ]
                #result.get("allowed_tools") - 安全访问
                # 存在key：返回对应的值（可能是 None、[]、["tool1"] 等）
                # 不存在key：返回 None（不会抛异常）
                if result.get("allowed_tools")
                else  [t for t in self.tools if t["name"] != "agent"]
            )

            print_sub_agent_start("skill-fork", inp.get("skill_name", ""))
            sub_agent = Agent(
                model=self.model,
                api_base=str(self._openai_client.base_url) if self.use_openai and self._openai_client else None,
                custom_system_prompt=result["prompt"],
                custom_tools=tools,
                is_sub_agent=True,
                permission_mode="plan" if self.permission_mode == "plan" else "bypassPermissions",
            )
            try:
                sub_result = await sub_agent.run_once(inp.get("args") or "Execute this skill task.")
                self.total_input_tokens += sub_result["tokens"]["input"]
                self.total_output_tokens += sub_result["tokens"]["output"]
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return sub_result["text"] or "(Skill produced no output)"
            except Exception as e:
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return f"Skill fork error: {e}"

        return f'[Skill "{inp.get("skill_name", "")}" activated]\n\n{result["prompt"]}'

    async def _execute_plan_mode_tool(self, name):
        if name == "enter_plan_mode":
            if self.permission_mode == "plan":
                return "Already in plan mode."
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path =  self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            print_info("Entered plan mode (read-only). Plan file: " + self._plan_file_path)
            return f"Entered plan mode. You are now in read-only mode.\n\nYour plan file: {self._plan_file_path}\nWrite your plan to this file. This is the only file you can edit.\n\nWhen your plan is complete, call exit_plan_mode."
        if name == "exit_plan_mode":
            if self.permission_mode != "plan":
                return "Not in plan mode."
            plan_content = "(No plan file found)"
            if self._plan_file_path and Path(self._plan_file_path).exists():
                plan_content = self._plan_file_path
            # 交互式审批流程（如果有审批函数）
            if self._plan_approval_fn:
                result = self._plan_approval_fn(plan_content)
                choice = result.get("choice", "manual-execute")

                if choice =="keep-planning":
                    feedback = result.get("feedback") or "Please revise the plan."
                    return (
                        f"User rejected the plan and wants to keep planning.\n\n"
                        f"User feedback: {feedback}\n\n"
                        f"Please revise your plan based on this feedback. When done, call exit_plan_mode again."
                    )

                if choice == "clear-and-execute":
                    target_mode = "acceptEdits"
                elif choice == "execute":
                    target_mode = "acceptEdits"
                else:  # manual-execute
                    target_mode = self._pre_plan_mode or "default"

                #离开计划模式
                self._pre_plan_mode = target_mode
                self._pre_plan_mode = None
                saved_plan_path = self._plan_file_path
                self._plan_file_path = None
                self._system_prompt = self._base_system_prompt
                if self.use_openai and self._openai_messages:
                    self._openai_messages[0]["content"] = self._system_prompt

                if choice == "clear-and-execute":
                    self._clear_history_keep_system()
                    self._context_cleared = True
                    print_info(f"Plan approved. Context cleared, executing in {target_mode} mode.")
                    return (
                        f"User approved the plan. Context was cleared. Permission mode: {target_mode}\n\n"
                        f"Plan file: {saved_plan_path}\n\n"
                        f"## Approved Plan:\n{plan_content}\n\n"
                        f"Proceed with implementation."
                    )
                print_info(f"Plan approved. Executing in {target_mode} mode.")
                return (
                    f"User approved the plan. Permission mode: {target_mode}\n\n"
                    f"## Approved Plan:\n{plan_content}\n\n"
                    f"Proceed with implementation."
                )
            # 没有审批函数时的回退（例如子代理）
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt

            print_info("Exited plan mode. Restored to " + self.permission_mode + " mode.")
            return f"Exited plan mode. Permission mode restored to: {self.permission_mode}\n\n## Your Plan:\n{plan_content}"

        return f"Unknown plan mode tool: {name}"

    def _clear_history_keep_system(self) -> None:
        """清空历史信息，但是保留系统prompt."""
        self._anthropic_messages = []
        self._openai_messages = []
        if self.use_openai:
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self.last_input_token_count = 0
        self._fold_last_time = 0.0
        self._fold_count = 0
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""

    async def _execute_agent_tool(self, inp:dict) -> str:
        agent_type = inp.get("type", "general")
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")
        print_sub_agent_start(agent_type, description)

        config = get_sub_agent_config(agent_type)

        sub_agent = Agent(
            model=self.model,
            api_base=str(self._openai_client.base_url) if self.use_openai and self._openai_client else None,
            custom_system_prompt=config["system_prompt"],
            custom_tools=config["tools"],
            is_sub_agent=True,
            permission_mode="plan" if self.permission_mode == "plan" else "bypassPermissions",
        )
        try:
            result = await sub_agent.run_once(prompt)
            self.total_input_tokens += result["tokens"]["input"]
            self.total_output_tokens += result["tokens"]["output"]
            print_sub_agent_end(agent_type, description)
            return result["text"] or "(Sub-agent produced no output)"
        except Exception as e:
            print_sub_agent_end(agent_type, description)
            return f"Sub-agent error: {e}"

#--------------Anthropic 后端---------------
    async def  _chat_anthropic(self, user_message: str) -> None:
        """运行 Anthropic tool_use/tool_result 协议循环，并提前执行并发安全工具。

        Anthropic 把一次回复表示成多个 content block：普通正文是 ``text``，工具意图是
        ``tool_use``。Harness 执行后要用相同 id 返回 ``tool_result``，模型才能知道每个
        结果对应哪个调用。
        """
        self._anthropic_messages = self._normalize_anthropic_messages(_sanitize_for_utf8(self._anthropic_messages))
        user_message = _safe_utf8_text(user_message)
        # 先把本轮用户输入放入 Anthropic 消息历史，后续每轮模型调用都会带上这段上下文。
        self._anthropic_messages.append({"role": "user", "content": user_message})

        # 异步内存预取：主 agent 才需要查 memory，sub agent 不额外注入记忆。
        # 这里只启动后台任务，不阻塞当前模型调用流程。
        memory_prefetch:MemoryPrefetch | None = None
        if not self.is_sub_agent:
            sq = self._build_side_query()
            if sq:
                memory_prefetch = start_memory_prefetch(
                    user_message, sq,
                    self._already_surfaced_memories, self._session_memory_bytes,
                )
        # while 的一次迭代就是一次内部 Agent turn：调模型一次，必要时再执行一批工具。
        # 它不同于用户看到的一轮 chat；一个 chat 往往会包含多个这样的内部 turn。
        while True:
            # 外部请求中止时，结束整个 agent loop。
            if self._aborted:
                break

            # 每轮调用模型前尝试压缩上下文，避免消息历史过长。
            self._run_compression_pipeline()

            # 如果记忆预取任务已经完成，就把取回来的 memory 内容追加到最后一条用户消息里。
            # consumed 用来保证同一批 memory 只注入一次。
            if memory_prefetch and memory_prefetch.settled and not memory_prefetch.consumed:
                memory_prefetch.consumed = True
                try:
                    memories = memory_prefetch.task.result()
                    if memories:
                        injection_text = format_memories_for_injection(memories)
                        injection_text = _safe_utf8_text(injection_text)
                        last = self._anthropic_messages[-1] if self._anthropic_messages else None
                        if last and last.get("role") == "user":
                            content = last.get("content", "")
                            if isinstance(content, str):
                                # 字符串不可变，需要重新赋值回 message。
                                last["content"] = content + "\n\n" + injection_text
                            elif isinstance(content, list):
                                # list 是可变对象，append 会直接修改 last["content"] 指向的列表。
                                content.append({"type": "text", "text": injection_text})
                        else:
                            # 如果最后一条不是 user message，就单独追加一条用户消息承载 memory。
                            self._anthropic_messages.append({"role": "user", "content": injection_text})

                        for m in memories:
                            # 记录本 session 已经注入过的 memory，后续检索时可避免重复 surfaced。
                            self._already_surfaced_memories.add(m.path)
                            self._session_memory_bytes += m.size
                except:
                    # memory 注入失败不应该中断主对话流程。
                    pass

            if not self.is_sub_agent:
                start_spinner()


            # 保存“提前执行”的工具任务。key 是 Anthropic 返回的 tool_use block id。
            early_executions: dict[str, asyncio.Task] = {}


            def _on_tool_block(block:dict):
                # 流式响应中一旦完整收到 tool_use block，如果工具是并发安全且权限允许，
                # 就可以提前开始执行，减少等待完整模型响应后的空档时间。
                if block["name"] in CONCURRENCY_SAFE_TOOLS:
                    perm = check_permission(block["name"], block["input"], self.permission_mode, self._plan_file_path)
                    if perm["action"]=="allow":
                        task =asyncio.create_task(self._execute_tool_call(block["name"], block["input"]))
                        early_executions[block["id"]] = task


            # 调用 Anthropic 流式接口。system、tools 和完整消息历史都会随本次请求发出；
            # 模型没有隐藏的本地状态，只能看到 Harness 明确放进请求的内容。
            # 流式过程中完成 tool block 时会触发 _on_tool_block。
            response = await self._call_anthropic_stream(on_tool_block_complete=_on_tool_block)
            if not self.is_sub_agent:
                stop_spinner()

            # 记录本次模型调用的耗时点和 token 消耗，用于成本展示与预算控制。
            self.last_api_call_time = time.time()
            self.total_input_tokens += response.usage.input_tokens
            self.total_output_tokens += response.usage.output_tokens
            self.last_input_token_count = response.usage.input_tokens

            # Anthropic 的响应内容里可能混有 text block 和 tool_use block，这里只挑出工具调用。
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            # 把模型返回的所有 content block 写入消息历史，后续 tool_result 要与这些 tool_use 对应。
            self._anthropic_messages.append({
                "role": "assistant",
                "content": [self._block_to_dict(b) for b in response.content],
            })

            # 没有工具调用，说明模型已经给出最终回复，本轮对话结束。
            # 若存在 tool_use，即使同时输出了文字，也还不是完整结束：工具结果需再喂回模型。
            if not tool_uses:
                if not self.is_sub_agent:
                    print_cost(self.total_input_tokens, self.total_output_tokens)
                break

            # 有工具调用时，进入下一轮工具执行。这里同时检查 turn/budget 限制。
            self.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                self._anthropic_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": f"Tool execution skipped: {budget['reason']}",
                        }
                        for tu in tool_uses
                    ],
                })
                break


            # 收集本轮所有工具结果，之后作为 tool_result 消息回传给模型。
            tool_results: list[dict] = []
            context_break = False

            for tu in tool_uses:
                # context_break 表示某个工具执行期间清理了上下文，需要停止继续处理本轮剩余工具。
                if context_break or self._aborted:
                    break

                # 将工具入参转为普通 dict，便于权限检查、打印和实际执行。
                inp = dict(tu.input) if hasattr(tu, "items") else tu.input
                print_tool_call(tu.name, inp)

                # 如果这个工具已经在流式阶段提前开始执行，这里只需要等待它完成并收集结果。
                early_task = early_executions.get(tu.id)
                if early_task:
                    try:
                        raw = await early_task
                    except Exception as e:
                        raw = f"Error executing tool: {e}"
                    raw = _safe_utf8_text(raw)
                    res = self._persist_large_result(tu.name, raw)
                    print_tool_result(tu.name, res)
                    self._record_tool_outcome(tu.name, not self._looks_like_tool_failure(tu.name, raw, res))
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})
                    continue

                # 如果不是提前执行的工具，就在真正执行前做权限检查。

                perm = check_permission(tu.name, inp, self.permission_mode, self._plan_file_path)
                if perm["action"] == "deny":
                    # 权限拒绝时，也要返回一个 tool_result，让模型知道该工具调用失败的原因。
                    print_info(f"Denied: {perm.get('message', '')}")
                    self._record_tool_outcome(tu.name, False)
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                         "content": f"Action denied: {perm.get('message', '')}"})
                    continue

                if perm["action"] == "confirm" and perm.get("message") and perm["message"] not in self._confirmed_paths:
                    # 高风险操作需要用户确认；同一个 message 确认过后会缓存，避免重复询问。
                    confirmed = await self._confirm_dangerous(perm["message"])
                    if not confirmed:
                        self._record_tool_outcome(tu.name, False)
                        tool_results.append(
                            {"type": "tool_result", "tool_use_id": tu.id, "content": "User denied this action."})
                        continue
                    self._confirmed_paths.add(perm["message"])

                # 权限通过后执行工具，并把大输出持久化为可回传的摘要或引用。
                try:
                    raw = await self._execute_tool_call(tu.name, inp)
                except Exception as e:
                    raw = f"Error executing tool: {e}"
                raw = _safe_utf8_text(raw)
                res = self._persist_large_result(tu.name, raw)
                print_tool_result(tu.name, res)
                self._record_tool_outcome(tu.name, not self._looks_like_tool_failure(tu.name, raw, res))

                if self._context_cleared:
                    # 工具执行过程中如果清理了上下文，就把结果作为新的用户消息写入，
                    # 并停止继续处理本轮剩余工具，避免旧上下文和新上下文混在一起。
                    self._context_cleared = False
                    self._anthropic_messages.append({"role": "user", "content": res})
                    context_break = True
                    break

                # Anthropic 要求 tool_result 使用 tool_use_id 对应到前面的 tool_use block。
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})

            if not context_break and tool_results:
                # Anthropic 要求 assistant/tool_use 后面紧跟一条 user/tool_result 消息，
                # 且这条消息必须包含本轮所有 tool_use 的对应结果。
                self._anthropic_messages.append({"role": "user", "content": tool_results})

            self._context_cleared = False

            # 工具结果可能很长，每轮工具执行后检查是否需要压缩上下文。
            self._refresh_runtime_system_prompt()
            await self._check_and_compact()
            # 不 break 就回到 while 顶部。下一次模型请求会带上刚追加的 tool_result，
            # 模型据此决定继续调用工具，还是生成最终回答。

    @staticmethod
    def _block_to_dict(block) -> dict:
        if block.type == "text":
            return {"type": "text", "text": _safe_utf8_text(block.text)}
        if block.type == "tool_use":
            raw_input = dict(block.input) if hasattr(block.input, 'items') else block.input
            return {"type": "tool_use", "id": _safe_utf8_text(block.id), "name": _safe_utf8_text(block.name), "input": _sanitize_for_utf8(raw_input)}
        # Fallback
        return {"type": _safe_utf8_text(block.type)}

    async def _call_anthropic_stream(self, on_tool_block_complete=None):
        """消费 Anthropic 流事件，并还原一条完整 assistant 消息。

        流式传输只是降低首字延迟，并没有改变 Agent 语义。正文分片可以立即展示；工具参数
        必须先把 partial JSON 拼完整，之后才能安全地交给工具执行层。
        """

        async def _do():
            max_output =  _get_max_output_tokens(self.model)

            create_params: dict[str, Any] = {
                # system 告诉模型角色/环境，tools 声明可请求的动作，messages 保存任务状态。
                # tools 只是 JSON schema，并不会因为放进请求就自动执行任何本地代码。
                "model": self.model,
                "max_tokens": max_output if self._thinking_mode != "disabled" else 16384,
                "system": _safe_utf8_text(self._system_prompt),
                "tools": _sanitize_for_utf8(get_active_tool_definitions(self.tools)),
                "messages": _sanitize_for_utf8(self._anthropic_messages),
            }
            #如果开启了思考模式，就给 Anthropic 请求加上 thinking 参数。
            if self._thinking_mode  in ("adaptive", "enabled"):
                create_params["thinking"]={"type": "enabled", "budget_tokens": max_output - 1}

            first_text = True

            tool_blocks_by_index: dict[int, dict] = {}

            async with self._anthropic_client.messages.stream(**create_params)as stream:
                async for event in stream:
                    if not hasattr(event, 'type'):
                        continue
                    # 当事件是工具调用开始：
                    if event.type == "content_block_start":
                        cb = getattr(event, 'content_block', None)
                        #如果 block 类型是 tool_use，就记录这个工具调用：
                        if cb and getattr(cb, 'type', None) == "tool_use":
                            #因为工具参数 JSON 是流式分片返回的，所以先准备一个空字符串 input_json。
                            tool_blocks_by_index[event.index]= {
                                "id": cb.id, "name": cb.name, "input_json": "",
                            }
                    #当事件是内容增量，分三种情况。
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        # 第一种，普通文本：模型输出正文时，
                        # 调用 _emit_text()。如果是普通交互，就打印；
                        # 如果是 run_once()，就写入 _output_buffer。
                        if hasattr(delta, "text"):
                            if first_text:
                                stop_spinner()
                                self._emit_text("\n")
                                first_text = False
                            self._emit_text(delta.text)
                        #第二种，thinking 内容：
                        #如果模型返回思考内容，也输出出来，并在开头加：[thinking]
                        elif hasattr(delta, 'thinking'):
                            if first_text:
                                stop_spinner()
                                self._emit_text("\n  [thinking] ")
                                first_text = False
                            self._emit_text(delta.thinking)
                        #第三种，工具参数 JSON 片段：工具调用的参数不是一次性返回，
                        # 而是一段一段返回，所以这里不断拼接到 input_json。
                        elif hasattr(delta, 'partial_json'):
                            tb = tool_blocks_by_index.get(event.index)
                            if tb:
                                tb["input_json"] += _safe_utf8_text(delta.partial_json)
                    #当一个 content block 结束：
                    #如果结束的是之前记录的工具调用，就把拼好的 JSON 解析出来：
                    elif event.type == "content_block_stop":
                        tb = tool_blocks_by_index.pop(event.index, None)
                        if tb and on_tool_block_complete:
                            import json as _json
                            try:
                                parsed = _json.loads(tb["input_json"] or "{}")
                            except Exception:
                                parsed = {}
                            #然后调用回调：
                            #这个回调的作用通常是：工具调用一完整，
                            # 就可以提前开始执行工具，不必等整条 assistant 消息全部结束。
                            on_tool_block_complete({
                                "type": "tool_use", "id": _safe_utf8_text(tb["id"]),
                                "name": _safe_utf8_text(tb["name"]), "input": _sanitize_for_utf8(parsed),
                            })
                final_message = await stream.get_final_message()

            #过滤思考的message（因为 thinking 内容一般不应该进入历史消息，否则后续上下文会变大，也可能不符合 API 消息格式要求。）
            final_message.content = [b for b in final_message.content if b.type != "thinking"]
            return final_message
#调用 _do()，如果遇到可重试错误，就由 _with_retry() 负责重试。
        return await _with_retry(_do)

    #openAI后端

    async def _chat_openai(self, user_message:str) -> None:
        """运行 OpenAI tool_calls/role=tool 协议循环，并批量执行只读工具。

        OpenAI 协议中，模型先返回带 ``tool_calls`` 的 assistant 消息；Harness 再为每个
        ``tool_call_id`` 追加一条 ``role=tool`` 消息。两者必须同时保留在历史里，下一次
        请求才能形成完整的“提出调用 -> 得到结果”因果链。
        """
        user_message = _safe_utf8_text(user_message)
        # _openai_messages 是当前会话的真实状态；API 本身不会替 Harness 保存这段历史。
        self._openai_messages.append({"role": "user", "content": user_message})

        # 主 Agent 异步预取 Memory；子 Agent 依靠自己的隔离 prompt，不读取长期记忆。
        memory_prefetch: MemoryPrefetch | None = None
        if not self.is_sub_agent:
            sq = self._build_side_query()
            if sq:
                memory_prefetch = start_memory_prefetch(
                    user_message, sq,
                    self._already_surfaced_memories, self._session_memory_bytes,
                )

        # 一次 while 迭代对应一次模型 API 请求，以及该回复要求的一批本地工具执行。
        while True:
            # abort() 会取消当前 Task；循环边界也检查标记，避免继续发起下一次模型请求。
            if self._aborted:
                break
            # 每次模型调用前先缩减旧工具结果，尽量把 token 留给有效对话。
            self._run_compression_pipeline()

            if memory_prefetch and memory_prefetch.settled and not memory_prefetch.consumed:
                # settled 只表示异步召回完成；consumed 保证同一批 Memory 只注入一次。
                # 当前这一轮已经发出去的 LLM 请求，没有记忆，继续输出，输出不中断
                # 预取记忆 settled 之后，把记忆写入本地 state.messages（内存里的对话历史）
                # 下一次工具调用 / 下一次 agent 内部 turn 迭代的时候，新的 LLM 请求带上这份 memory
                memory_prefetch.consumed = True
                try:
                    memories = memory_prefetch.task.result()
                    if memories:
                        injection_text = format_memories_for_injection(memories)
                        injection_text = _safe_utf8_text(injection_text)
                        last = self._openai_messages[-1] if self._openai_messages else None

                        if last and last.get("role") == "user":
                            # 优先附加到最后一条 user 消息，保持当前请求与召回内容相邻。
                            last["content"] = (last.get("content") or "") + "\n\n" + injection_text
                        else:
                            self._openai_messages.append({"role": "user", "content": injection_text})

                        for m in memories:
                            # 记录去重集合和会话字节预算，控制后续轮次的重复/过量召回。
                            self._already_surfaced_memories.add(m.path)
                            self._session_memory_bytes += len(m.content.encode())
                except Exception:
                    pass

            if not self.is_sub_agent:
                start_spinner()

            # 请求中携带 system/user/assistant/tool 历史和当前可见工具 schema。
            # 这里返回的是已由 _call_openai_stream 从分片重新组装好的完整响应。
            response = await self._call_openai_stream()

            if not self.is_sub_agent:
                stop_spinner()

            self.last_api_call_time = time.time()

            if response.get("usage"):
                self.total_input_tokens += response["usage"]["prompt_tokens"]
                self.total_output_tokens += response["usage"]["completion_tokens"]
                self.last_input_token_count = response["usage"]["prompt_tokens"]

            choice = response.get("choices", [{}])[0] if response.get("choices") else {}
            message = choice.get("message", {})

            # assistant 消息必须先入历史。后面追加的 role=tool 要用 tool_call_id 指向它。
            self._openai_messages.append(message)

            tool_calls = message.get("tool_calls")

            if not tool_calls:
                # 没有 tool_calls 表示模型只给出了最终正文，Agent loop 可以结束。
                if not self.is_sub_agent:
                    print_cost(self.total_input_tokens, self.total_output_tokens)
                break

            self.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                break

            oai_checked: list[dict] = []
            for tc in tool_calls:
                if self._aborted:
                    break

                if tc.get("type") != "function":
                    continue

                fn_name = tc["function"]["name"]
                try:
                    # function.arguments 在协议中是 JSON 字符串，不是已经可调用的 Python 参数。
                    inp = json.loads(tc["function"]["arguments"])
                except Exception:
                    inp = {}

                print_tool_call(fn_name, inp)

                # 模型能提出工具调用不代表一定能执行；本地权限层拥有最终决定权。
                perm = check_permission(fn_name, inp, self.permission_mode, self._plan_file_path)

                if perm["action"] == "deny":
                    print_info(f"Denied: {perm.get('message', '')}")
                    self._record_tool_outcome(fn_name, False)
                    oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False,
                                        "result": f"Action denied: {perm.get('message', '')}"})
                    continue
                if perm["action"] == "confirm" and perm.get("message") and perm["message"] not in self._confirmed_paths:
                    # 弹出终端对话框，让用户确认危险操作
                    confirmed = await self._confirm_dangerous(perm["message"])
                    # 用户点击拒绝的情况
                    if not confirmed:
                        self._record_tool_outcome(fn_name, False)
                        oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False,
                                            "result": "User denied this action."})
                        continue
                    # 用户点击同意：加入已确认集合，下次不再弹窗
                    self._confirmed_paths.add(perm["message"])
                oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})

                oai_batches: list[dict] = []
                for ct in oai_checked:
                    # 只读、无副作用的工具可放入并发批次；写文件和 Shell 等保持串行，(cy)
                    # 避免执行顺序变化导致后一个调用看不到前一个调用产生的状态。
                    safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
                    if safe and oai_batches and oai_batches[-1]["concurrent"]:
                        oai_batches[-1]["items"].append(ct)
                    else:
                        oai_batches.append({"concurrent": safe, "items": [ct]})

                # 当前工具执行阶段的熔断标记。某个工具若触发上下文压缩，后续批次必须停止，
                # 避免继续执行基于旧消息历史生成的工具调用。
                oai_context_break = False
                for batch in oai_batches:
                    # 用户中止或上下文已经被替换时，不再消费尚未执行的批次。
                    if oai_context_break or self._aborted:
                        break
                    # 并发批次只包含已通过权限检查、且声明为无副作用的工具。
                    if batch["concurrent"]:
                        async def _run_oai_safe(ct_item: dict) -> tuple[dict, str]:
                            # 每个任务独立执行工具并规范化结果；过大的结果会先落盘，
                            # 返回可安全放进模型上下文的预览或文件引用。
                            raw = await self._execute_tool_call(ct_item["fn"], ct_item["inp"])
                            raw = _safe_utf8_text(raw)
                            res = self._persist_large_result(ct_item["fn"], raw)
                            print_tool_result(ct_item["fn"], res)
                            # 连同原始调用信息返回，以便稍后取出对应的 tool_call_id。
                            return ct_item, res

                        # gather 保持结果顺序与 batch["items"] 一致，同时缩短多个只读工具的总耗时。
                        results = await asyncio.gather(*[_run_oai_safe(ct) for ct in batch["items"]])
                        for ct_item, res in results:
                            # 记录运行质量信号，供上下文折叠策略判断连续失败和重复调用。
                            self._record_tool_outcome(
                                ct_item["fn"],
                                not self._looks_like_tool_failure(ct_item["fn"], "", res),
                            )
                            # OpenAI 协议要求每个工具结果用 tool_call_id 与 assistant 的调用一一对应；
                            # 下一次模型请求会携带这些 role=tool 消息，让模型继续推理。
                            self._openai_messages.append(
                                {"role": "tool", "tool_call_id": ct_item["tc"]["id"], "content": res})
                    else: # 串行批次执行
                        for ct in batch["items"]:
                            if not ct["allowed"]:# 先处理权限被拒绝的工具
                                # 即使权限拒绝，也必须返回 role=tool，让模型看到失败原因并改道。
                                self._openai_messages.append(
                                    {"role": "tool", "tool_call_id": ct["tc"]["id"], "content": ct["result"]})
                                continue
                            # 正常放行工具串行执行
                            raw = await self._execute_tool_call(ct["fn"], ct["inp"])
                            raw = _safe_utf8_text(raw)
                            res = self._persist_large_result(ct["fn"], raw)
                            print_tool_result(ct["fn"], res)
                            self._record_tool_outcome(
                                ct["fn"],
                                not self._looks_like_tool_failure(ct["fn"], raw, res),
                            )
                            # 核心特殊逻辑：self._context_cleared 上下文清空熔断
                            # 当本轮工具执行后，上下文 token 超限，触发了全局上下文清理机制（/compact 压缩或内存溢出裁剪），self._context_cleared 被标记为 True。
                            
                            # 执行动作
                            # 1 重置清空标记 self._context_cleared = False
                            # 2 这条工具结果不按标准 role:tool 塞入，改用 role:user 包裹追加（OpenAI 协议上下文重置兼容写法）
                            # 3 把全局熔断开关 oai_context_break = True
                            # 4 break 跳出当前串行批次循环
                            # 5 外层大循环检测到 oai_context_break=True，直接终止后面所有剩余工具批次不再执行
                            # 设计目的
                            # 上下文已经被大量裁剪、历史消息丢失，继续执行后续工具已经没有业务意义；强行终止工具队列，立刻把精简后的上下文丢给 LLM 进入下一轮思考，避免无效消耗 API 额度和磁盘 IO。
                            if self._context_cleared:
                                self._context_cleared = False
                                self._openai_messages.append({"role": "user", "content": res})
                                oai_context_break = True
                                break
                            # 未触发清空则正常拼装消息
                            self._openai_messages.append(
                                {"role": "tool", "tool_call_id": ct["tc"]["id"], "content": res})

            self._context_cleared = False
            self._refresh_runtime_system_prompt()
            await self._check_and_compact()
            # 回到 while 顶部后，刚写入的 role=tool 结果会随完整历史再次发给模型。

    async def _call_openai_stream(self) -> dict:
        """消费 OpenAI 流式分片，组装成协议循环易于处理的一条完整响应。"""
        async def _do():
            stream = await self._openai_client.chat.completions.create(
                model=self.model,
                # schema 只告诉模型“可以请求哪些函数”；真正执行发生在 _execute_tool_call。
                tools=_sanitize_for_utf8(_to_openai_tools(get_active_tool_definitions(self.tools))),
                messages=_sanitize_for_utf8(self._openai_messages),
                stream=True,
                stream_options={"include_usage": True},
            )

            content = ""
            first_text = True
            # 一个响应可并行提出多个调用；index 用来把各调用交错到达的参数分片分别拼接。
            tool_calls: dict[int, dict] = {}
            finish_reason = ""
            usage = None

            async for chunk in stream:
                if chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                    }

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta and delta.content:
                    # 正文分片可边收边显示，同时累积起来写入 assistant 消息历史。
                    if first_text:
                        stop_spinner()
                        self._emit_text("\n")
                        first_text = False
                    self._emit_text(delta.content)
                    content += _safe_utf8_text(delta.content)

                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        existing = tool_calls.get(tc.index)
                        if existing:
                            if tc.function and tc.function.arguments:
                                # 参数经常被拆成多段，例如 '{"path"' 和 ':"a.py"}'。
                                existing["arguments"] += _safe_utf8_text(tc.function.arguments)
                        else:
                            tool_calls[tc.index] = {
                                "id": _safe_utf8_text(tc.id or ""),
                                "name": _safe_utf8_text((tc.function.name if tc.function else "") or ""),
                                "arguments": _safe_utf8_text((tc.function.arguments if tc.function else "") or ""),
                            }

                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

            assembled = None
            if tool_calls:
                # 恢复为非流式 Chat Completions 的 tool_calls 形状，简化外层协议循环。
                assembled = [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for _, tc in sorted(tool_calls.items())
                ]

            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": assembled,
                    },
                    "finish_reason": finish_reason or "stop",
                }],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
            }

        return await _with_retry(_do)

    async def _confirm_dangerous(self, command: str) -> bool:
        print_confirmation(command)
        if self.confirm_fn:
            return await self.confirm_fn(command)
        # Fallback: blocking input
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False
