from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agents.agent import Agent, RuntimeFeatures
from agents.prompt import build_system_prompt
from agents.skills import reset_skill_cache


def _tool(name: str) -> dict:
    return {
        "name": name,
        "description": name,
        "input_schema": {"type": "object", "properties": {}},
    }


def _agent(features: RuntimeFeatures, *, tools: list[dict] | None = None) -> Agent:
    return Agent(
        api_base="https://example.invalid/v1",
        api_key="test-key",
        custom_system_prompt="test system",
        custom_tools=tools if tools is not None else [],
        runtime_features=features,
    )


class RuntimeFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_and_fold_tool_gating(self):
        tools = [_tool(name) for name in (
            "skill", "skill_create", "skill_evolve", "compact_context", "echo"
        )]
        off = _agent(RuntimeFeatures(
            skill_mode="off", folding_mode="off", mcp_enabled=False, auto_save=False
        ), tools=tools)
        self.assertEqual([tool["name"] for tool in off.tools], ["echo"])

        static = _agent(RuntimeFeatures(
            skill_mode="static", folding_mode="structured", mcp_enabled=False, auto_save=False
        ), tools=tools)
        self.assertEqual(
            {tool["name"] for tool in static.tools},
            {"skill", "compact_context", "echo"},
        )
        self.assertFalse(static._online_evolution_enabled())
        self.assertIn(
            "disabled",
            await static._execute_tool_call("skill_create", {"name": "x"}),
        )
        self.assertIn(
            "disabled", await off._execute_tool_call("skill", {"skill_name": "x"})
        )

        evolve = _agent(RuntimeFeatures(
            skill_mode="evolve", folding_mode="structured", mcp_enabled=False, auto_save=False
        ), tools=tools)
        self.assertEqual({tool["name"] for tool in evolve.tools}, {tool["name"] for tool in tools})

    def test_prompt_omits_discovered_skill_when_disabled(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_file = root / ".bear" / "skills" / "unique-eval-leak" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text(
                "---\nname: unique-eval-leak\ndescription: SHOULD_NOT_LEAK\n---\nbody\n",
                encoding="utf-8",
            )
            try:
                os.chdir(root)
                reset_skill_cache()
                disabled = build_system_prompt(include_skills=False)
                reset_skill_cache()
                enabled = build_system_prompt(include_skills=True)
            finally:
                os.chdir(previous)
                reset_skill_cache()
        self.assertNotIn("SHOULD_NOT_LEAK", disabled)
        self.assertIn("SHOULD_NOT_LEAK", enabled)

    async def test_skill_off_skips_retrieval_and_static_skips_background_evolution(self):
        off = _agent(RuntimeFeatures(
            skill_mode="off", folding_mode="off", mcp_enabled=False, auto_save=False
        ))
        off._chat_openai = AsyncMock(return_value=None)
        off._augment_user_message_with_skill_context = unittest.mock.Mock(
            side_effect=AssertionError("retrieval leaked")
        )
        await off.chat("hello")
        off._augment_user_message_with_skill_context.assert_not_called()

        static = _agent(RuntimeFeatures(
            skill_mode="static", folding_mode="off", mcp_enabled=False, auto_save=False
        ))
        static._chat_openai = AsyncMock(return_value=None)
        static._augment_user_message_with_skill_context = unittest.mock.Mock(
            return_value=("hello", None)
        )
        static._schedule_background_skill_task = unittest.mock.Mock(
            side_effect=AssertionError("static mode scheduled mutation")
        )
        await static.chat("hello")
        static._augment_user_message_with_skill_context.assert_called_once()
        static._schedule_background_skill_task.assert_not_called()
        self.assertIsNone(static._pending_skill_extraction_window)

    async def test_custom_executor_and_trace_are_read_only(self):
        async def execute(name, arguments):
            return json.dumps({"name": name, "arguments": arguments}, sort_keys=True)

        agent = Agent(
            api_base="https://example.invalid/v1",
            api_key="test-key",
            custom_system_prompt="test",
            custom_tools=[_tool("echo")],
            custom_tool_executor=execute,
            runtime_features=RuntimeFeatures(
                skill_mode="off", folding_mode="off", mcp_enabled=False, auto_save=False
            ),
        )
        result = await agent._execute_tool_call("echo", {"b": 2, "a": 1})
        self.assertIn('"name": "echo"', result)
        trace = agent.get_trace()
        self.assertEqual([event["phase"] for event in trace], ["start", "end"])
        trace[0]["name"] = "mutated"
        self.assertEqual(agent.get_trace()[0]["name"], "echo")

    async def test_side_query_has_separate_token_accounting(self):
        class Completions:
            async def create(self, **kwargs):
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content="ok"),
                        finish_reason="stop",
                    )],
                    usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3),
                )

        agent = _agent(RuntimeFeatures(
            skill_mode="off", folding_mode="off", mcp_enabled=False, auto_save=False
        ))
        agent._openai_client = SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        query = agent._build_side_query(max_tokens=10)
        self.assertEqual(await query("system", "user"), "ok")
        usage = agent.get_token_usage()
        self.assertEqual(usage["input"], 0)
        self.assertEqual(usage["side_input"], 11)
        self.assertEqual(usage["total_output"], 3)


class FoldingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def _fold_agent(self, *, max_folds=1, folding_mode="structured"):
        agent = _agent(RuntimeFeatures(
            skill_mode="off",
            folding_mode=folding_mode,
            max_folds=max_folds,
            mcp_enabled=False,
            auto_save=False,
        ))
        agent._openai_messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Find a fact"},
            {"role": "assistant", "tool_calls": [{
                "id": "1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "1", "content": "fact=alpha"},
        ]
        return agent

    async def test_valid_fold_and_max_fold_limit(self):
        agent = self._fold_agent()
        memory = {
            "episode_memory": {
                "task_description": "Find a fact",
                "key_events": [{"step": "1", "description": "lookup", "outcome": "alpha"}],
                "current_progress": "alpha found",
            },
            "working_memory": {
                "immediate_goal": "answer",
                "current_challenges": "",
                "next_actions": [],
            },
            "tool_memory": {
                "tools_used": [{"tool_name": "lookup", "response_pattern": "alpha"}],
                "derived_rules": [],
            },
        }

        async def query(system, user):
            return json.dumps(memory)

        agent._build_side_query = lambda **kwargs: query
        self.assertTrue(await agent._compact_conversation(trigger="auto"))
        fold = next(event for event in agent.get_trace() if event["event"] == "fold")
        self.assertFalse(fold["fallback"])
        self.assertIn("alpha", json.dumps(fold["memory"]))
        agent._openai_messages.extend([
            {"role": "assistant", "content": "continue"},
            {"role": "user", "content": "more"},
        ])
        self.assertFalse(await agent._compact_conversation(trigger="auto"))
        self.assertEqual(agent._fold_count, 1)
        self.assertEqual(agent.get_trace()[-1]["reason"], "max_folds_reached")

    async def test_invalid_json_uses_fallback(self):
        agent = self._fold_agent()

        async def invalid(system, user):
            return "not json"

        agent._build_side_query = lambda **kwargs: invalid
        self.assertTrue(await agent._compact_conversation(trigger="auto"))
        fold = next(event for event in agent.get_trace() if event["event"] == "fold")
        self.assertTrue(fold["fallback"])
        self.assertIn("fact=alpha", json.dumps(fold["memory"]))

    async def test_folding_off_cannot_compact(self):
        agent = self._fold_agent(max_folds=None, folding_mode="off")
        self.assertFalse(await agent._compact_conversation(trigger="manual"))
        self.assertEqual(agent.get_trace()[-1]["reason"], "folding_disabled")


if __name__ == "__main__":
    unittest.main()
