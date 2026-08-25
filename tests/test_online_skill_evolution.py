import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agents.online_skill_evolution import (
    OnlineSkillCandidate,
    judge_retrieved_skill_adoption,
    maintain_online_skill_candidate,
)


def _skill(name):
    return SimpleNamespace(
        name=name,
        description=f"description for {name}",
        when_to_use=f"when {name}",
        source="project",
        context="inline",
        prompt_template=f"instructions for {name}",
    )


class MaintainerIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_unique_invoked_skill_wins_over_top_retrieved_skill(self):
        async def side_query(system, payload):
            return json.dumps({"action": "merge", "target_skill": "", "reason": "feedback"})

        candidate = OnlineSkillCandidate("new-rule", "new durable rule", instructions="apply new rule")
        trace = {
            "retrieved": [{"name": "retrieved-a", "score": 0.95}],
            "surfaced": [{"name": "retrieved-a", "score": 0.95}],
            "invoked": [{"name": "invoked-b", "count": 1}],
        }
        with (
            patch("agents.skills.discover_skills", return_value=[_skill("retrieved-a"), _skill("invoked-b")]),
            patch("agents.skills.retrieve_relevant_skills", return_value=[{"name": "retrieved-a", "score": 0.2}]),
            patch("agents.skills.evolve_skill", return_value={"ok": True, "skill": "invoked-b"}) as evolve,
        ):
            result = await maintain_online_skill_candidate(candidate=candidate, side_query=side_query, skill_trace=trace)

        self.assertEqual(result["action"], "merge")
        evolve.assert_called_once()
        self.assertEqual(evolve.call_args.kwargs["skill_name"], "invoked-b")

    async def test_retrieved_top_one_is_never_merge_fallback(self):
        async def side_query(system, payload):
            return json.dumps({"action": "merge", "target_skill": "", "reason": "unspecified"})

        candidate = OnlineSkillCandidate("new-rule", "new durable rule", instructions="apply new rule")
        trace = {
            "retrieved": [{"name": "retrieved-a", "score": 0.99}],
            "surfaced": [{"name": "retrieved-a", "score": 0.99}],
            "invoked": [],
        }
        with (
            patch("agents.skills.discover_skills", return_value=[_skill("retrieved-a")]),
            patch("agents.skills.retrieve_relevant_skills", return_value=[{"name": "retrieved-a", "score": 0.2}]),
            patch("agents.skills.evolve_skill") as evolve,
        ):
            result = await maintain_online_skill_candidate(candidate=candidate, side_query=side_query, skill_trace=trace)

        self.assertEqual(result["action"], "discard")
        evolve.assert_not_called()

    async def test_multiple_invoked_skills_do_not_create_an_ambiguous_fallback(self):
        async def side_query(system, payload):
            return json.dumps({"action": "merge", "target_skill": "", "reason": "unspecified"})

        candidate = OnlineSkillCandidate("new-rule", "new durable rule", instructions="apply new rule")
        trace = {
            "retrieved": [{"name": "retrieved-a", "score": 0.99}],
            "surfaced": [{"name": "retrieved-a", "score": 0.99}],
            "invoked": [{"name": "invoked-b"}, {"name": "invoked-c"}],
        }
        skills = [_skill("retrieved-a"), _skill("invoked-b"), _skill("invoked-c")]
        with (
            patch("agents.skills.discover_skills", return_value=skills),
            patch("agents.skills.retrieve_relevant_skills", return_value=[{"name": "retrieved-a", "score": 0.2}]),
            patch("agents.skills.evolve_skill") as evolve,
        ):
            result = await maintain_online_skill_candidate(candidate=candidate, side_query=side_query, skill_trace=trace)

        self.assertEqual(result["action"], "discard")
        evolve.assert_not_called()


class AdoptionJudgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_judge_never_infers_usage_from_skill_name(self):
        judgments = await judge_retrieved_skill_adoption(
            hits=[{"name": "code-review", "source": "project", "skill_dir": "/tmp/code-review", "score": 0.8}],
            user_message="review this",
            assistant_text="I used code-review",
            surfaced_names={"code-review"},
            invoked_names=set(),
            side_query=None,
        )

        self.assertEqual(judgments[0]["surfaced"], True)
        self.assertEqual(judgments[0]["invoked"], False)
        self.assertEqual(judgments[0]["inferred_used"], False)
        self.assertNotIn("used", judgments[0])


if __name__ == "__main__":
    unittest.main()
