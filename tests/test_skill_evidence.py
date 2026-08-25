import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agents.agent import Agent
from agents.online_skill_eval import _skill_status
from agents.skill_evolution import (
    ONLINE_PROVENANCE_LOG,
    SKILL_USAGE_STATS,
    format_skill_stats,
    get_evolution_dir,
    load_skill_stats,
    record_online_skill_provenance,
    record_skill_invocation,
    record_skill_usage_judgments,
)
from agents.skills import SkillDefinition, execute_skill


class AgentSkillTraceTests(unittest.TestCase):
    def test_trace_aggregates_invocations_and_is_immutable(self):
        agent = Agent.__new__(Agent)
        agent._last_retrieved_skill_hits = [{"name": "retrieved-a", "score": 0.9}]
        agent._last_surfaced_skill_hits = [{"name": "retrieved-a", "score": 0.9}]
        agent._turn_invoked_skills = {}

        agent._record_turn_skill_invocation({"name": "invoked-b", "source": "project", "context": "inline"})
        agent._record_turn_skill_invocation({"name": "invoked-b", "source": "project", "context": "inline"})
        snapshot = agent._skill_trace_snapshot()

        agent._last_retrieved_skill_hits[0]["name"] = "next-turn"
        agent._turn_invoked_skills["invoked-b"]["count"] = 99

        self.assertEqual(snapshot["retrieved"][0]["name"], "retrieved-a")
        self.assertEqual(snapshot["invoked"][0]["name"], "invoked-b")
        self.assertEqual(snapshot["invoked"][0]["count"], 2)

    def test_cli_inline_initial_invocation_starts_the_same_turn_trace(self):
        agent = Agent.__new__(Agent)
        agent._start_skill_trace(
            [{"name": "slash-skill", "source": "project", "context": "inline", "skill_dir": "/tmp/slash"}]
        )

        self.assertEqual(agent._skill_trace_snapshot()["invoked"], [{
            "name": "slash-skill",
            "source": "project",
            "context": "inline",
            "skill_dir": "/tmp/slash",
            "count": 1,
        }])


class AgentUsageSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_judge_uses_explicit_previous_turn_snapshot(self):
        agent = Agent.__new__(Agent)
        agent.permission_mode = "default"
        agent._last_retrieved_skill_hits = [{"name": "next-turn", "score": 1.0}]
        captured = {}

        async def fake_judge(**kwargs):
            captured.update(kwargs)
            return []

        with (
            patch.object(agent, "_online_evolution_enabled", return_value=True),
            patch.object(agent, "_build_side_query", return_value=None),
            patch("agents.online_skill_evolution.judge_retrieved_skill_adoption", side_effect=fake_judge),
            patch("agents.skills.record_usage_judgments"),
        ):
            await agent._run_skill_usage_tracking(
                "request",
                "answer",
                retrieved_hits=[{"name": "previous-turn", "score": 0.8}],
                surfaced_names={"previous-turn"},
                invoked_names={"invoked-previous"},
            )

        self.assertEqual(captured["hits"][0]["name"], "previous-turn")
        self.assertEqual(captured["surfaced_names"], {"previous-turn"})
        self.assertEqual(captured["invoked_names"], {"invoked-previous"})


class AgentSkillExecutionTests(unittest.IsolatedAsyncioTestCase):
    def _agent(self):
        agent = Agent.__new__(Agent)
        agent._turn_invoked_skills = {}
        agent.tools = []
        agent.model = "test-model"
        agent.use_openai = False
        agent.permission_mode = "default"
        agent.total_input_tokens = 0
        agent.total_output_tokens = 0
        return agent

    async def test_inline_execution_records_invocation(self):
        agent = self._agent()
        resolved = {
            "name": "inline-skill",
            "prompt": "full instructions",
            "allowed_tools": None,
            "context": "inline",
            "source": "project",
            "skill_dir": "/tmp/inline-skill",
        }
        with patch("agents.skills.execute_skill", return_value=resolved):
            output = await Agent._execute_skill_tool(agent, {"skill_name": "inline-skill", "args": "task"})

        self.assertIn("full instructions", output)
        self.assertEqual(agent._turn_invoked_skills["inline-skill"]["count"], 1)

    async def test_unknown_skill_is_not_recorded(self):
        agent = self._agent()
        with patch("agents.skills.execute_skill", return_value=None):
            output = await Agent._execute_skill_tool(agent, {"skill_name": "missing"})

        self.assertIn("Unknown skill", output)
        self.assertEqual(agent._turn_invoked_skills, {})

    async def test_fork_execution_records_invocation_before_child_result(self):
        agent = self._agent()
        resolved = {
            "name": "fork-skill",
            "prompt": "fork instructions",
            "allowed_tools": None,
            "context": "fork",
            "source": "project",
            "skill_dir": "/tmp/fork-skill",
        }

        class FakeSubAgent:
            def __init__(self, **kwargs):
                pass

            async def run_once(self, prompt):
                return {"text": "fork complete", "tokens": {"input": 2, "output": 3}}

        with (
            patch("agents.skills.execute_skill", return_value=resolved),
            patch("agents.agent.Agent", FakeSubAgent),
            patch("agents.agent.print_sub_agent_start"),
            patch("agents.agent.print_sub_agent_end"),
        ):
            output = await Agent._execute_skill_tool(agent, {"skill_name": "fork-skill", "args": "task"})

        self.assertEqual(output, "fork complete")
        self.assertEqual(agent._turn_invoked_skills["fork-skill"]["count"], 1)
        self.assertEqual(agent.total_input_tokens, 2)
        self.assertEqual(agent.total_output_tokens, 3)

    async def test_permission_denial_never_reaches_skill_execution(self):
        agent = self._agent()
        agent.is_sub_agent = True
        agent._openai_messages = []
        agent._aborted = False
        agent._plan_file_path = None
        agent._confirmed_paths = set()
        agent._context_cleared = False
        agent.current_turns = 0
        agent.last_input_token_count = 0
        first = {
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "skill", "arguments": json.dumps({"skill_name": "denied-skill"})},
            }]}}],
        }
        final = {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

        with (
            patch.object(agent, "_call_openai_stream", new=AsyncMock(side_effect=[first, final])),
            patch.object(agent, "_execute_tool_call", new=AsyncMock()) as execute,
            patch.object(agent, "_run_compression_pipeline"),
            patch.object(agent, "_check_budget", return_value={"exceeded": False}),
            patch.object(agent, "_record_tool_outcome"),
            patch.object(agent, "_refresh_runtime_system_prompt"),
            patch.object(agent, "_check_and_compact", new=AsyncMock()),
            patch("agents.agent.check_permission", return_value={"action": "deny", "message": "blocked"}),
            patch("agents.agent.print_tool_call"),
            patch("agents.agent.print_info"),
        ):
            await agent._chat_openai("request")

        execute.assert_not_awaited()
        self.assertEqual(agent._turn_invoked_skills, {})


class SkillResolutionTests(unittest.TestCase):
    def test_failed_prompt_expansion_does_not_record_invocation(self):
        skill = SkillDefinition(
            name="broken-skill",
            description="broken",
            prompt_template="instructions",
            source="project",
            skill_dir="/tmp/broken-skill",
        )
        with (
            patch("agents.skills.get_skill_by_name", return_value=skill),
            patch("agents.skills.resolve_skill_prompt", side_effect=ValueError("cannot expand")),
            patch("agents.skills.record_skill_invocation") as record,
        ):
            with self.assertRaisesRegex(ValueError, "cannot expand"):
                execute_skill("broken-skill", "task")

        record.assert_not_called()


class SkillUsageStatsTests(unittest.TestCase):
    def setUp(self):
        self._old_cwd = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _stats(self):
        return json.loads((get_evolution_dir() / SKILL_USAGE_STATS).read_text(encoding="utf-8"))

    def test_real_invocation_updates_invoked_without_legacy_used(self):
        skill_dir = Path(self._tmp.name) / "skill-a"
        skill_dir.mkdir()
        record_skill_invocation(
            skill_name="skill-a",
            source="user",
            context="inline",
            skill_dir=str(skill_dir),
            args="task",
        )

        item = self._stats()["skill-a"]
        self.assertEqual(item["invoked"], 1)
        self.assertNotIn("used", item)

    def test_judge_cannot_increment_invoked(self):
        skill_dir = Path(self._tmp.name) / "judge-only"
        skill_dir.mkdir()
        record_skill_usage_judgments([{
            "name": "judge-only",
            "source": "user",
            "skill_dir": str(skill_dir),
            "retrieved": True,
            "surfaced": True,
            "invoked": True,
            "relevant": True,
            "inferred_used": True,
        }])

        self.assertEqual(self._stats()["judge-only"]["invoked"], 0)

    def test_legacy_used_field_is_not_loaded_or_displayed(self):
        stats_path = get_evolution_dir() / SKILL_USAGE_STATS
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(json.dumps({"legacy": {"retrieved": 2, "used": 2}}), encoding="utf-8")

        self.assertNotIn("used", load_skill_stats()["legacy"])
        rendered = format_skill_stats()
        self.assertIn("inferred_used=", rendered)
        self.assertNotIn(", used=", rendered)

    def test_provenance_persists_compact_skill_trace(self):
        trace = {
            "retrieved": [{"name": "skill-a", "score": 0.8, "instructions": "must not persist"}],
            "surfaced": [{"name": "skill-a", "score": 0.8}],
            "invoked": [{"name": "skill-b", "count": 1}],
        }
        record_online_skill_provenance(action="none", result={"ok": True}, skill_trace=trace)

        row = json.loads(
            (get_evolution_dir() / ONLINE_PROVENANCE_LOG).read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(row["skill_trace"]["invoked"][0]["name"], "skill-b")
        self.assertNotIn("instructions", row["skill_trace"]["retrieved"][0])
        self.assertNotIn("retrieved_reference", row)

    def test_pruning_uses_invoked_and_ignores_inferred_used(self):
        invoked_dir = Path(self._tmp.name) / "invoked-skill"
        inferred_dir = Path(self._tmp.name) / "inferred-only"
        invoked_dir.mkdir()
        inferred_dir.mkdir()
        record_skill_invocation(
            skill_name="invoked-skill",
            source="user",
            context="inline",
            skill_dir=str(invoked_dir),
        )
        env = {
            "BEAR_SKILL_USAGE_PRUNE_MIN_SURFACED": "1",
            "BEAR_SKILL_USAGE_PRUNE_MAX_INVOKED": "0",
        }
        with patch.dict(os.environ, env, clear=False):
            kept = record_skill_usage_judgments(
                [{
                    "name": "invoked-skill",
                    "source": "user",
                    "skill_dir": str(invoked_dir),
                    "retrieved": True,
                    "surfaced": True,
                    "relevant": False,
                    "inferred_used": False,
                }]
            )
            pruned = record_skill_usage_judgments(
                [{
                    "name": "inferred-only",
                    "source": "user",
                    "skill_dir": str(inferred_dir),
                    "retrieved": True,
                    "surfaced": True,
                    "relevant": True,
                    "inferred_used": True,
                }]
            )

        self.assertEqual(kept["pruned"], [])
        self.assertTrue(invoked_dir.is_dir())
        self.assertEqual(pruned["pruned"], ["inferred-only"])
        self.assertFalse(inferred_dir.exists())
        self.assertEqual(self._stats()["inferred-only"]["inferred_used"], 1)


class OnlineEvalGateTests(unittest.TestCase):
    def test_verified_invocation_is_required_for_healthy_status(self):
        common = {
            "replay_count": 2,
            "promotion_test_count": 1,
            "retrieved": 5,
            "surfaced": 5,
            "relevant": 5,
            "pruned": False,
            "rule_summary": {"pass_rate": 1.0, "hard_failures": 0, "promotion_test_hard_failures": 0},
            "min_replay_samples": 2,
            "min_promotion_tests": 1,
            "min_retrieved": 5,
            "min_invocations": 1,
            "min_relevance_rate": 0.35,
            "min_rule_pass_rate": 0.8,
        }
        status, reasons = _skill_status(invoked=0, **common)
        self.assertEqual(status, "incubating")
        self.assertIn("only 0 verified invocation(s)", reasons)

        status, _ = _skill_status(invoked=1, **common)
        self.assertEqual(status, "healthy")


if __name__ == "__main__":
    unittest.main()
