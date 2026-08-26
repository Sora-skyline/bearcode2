import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agents.online_skill_evolution import (
    OnlineSkillCandidate,
    analyze_online_feedback,
    judge_retrieved_skill_adoption,
    maintain_online_skill_candidate,
    online_ingest,
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
            patch(
                "agents.skill_evolution.stage_skill_proposal",
                return_value={"ok": True, "action": "propose", "skill": "invoked-b", "proposal_id": "p-1"},
            ) as stage,
        ):
            result = await maintain_online_skill_candidate(candidate=candidate, side_query=side_query, skill_trace=trace)

        self.assertEqual(result["action"], "propose")
        self.assertEqual(result["requested_action"], "merge")
        stage.assert_called_once()
        self.assertEqual(stage.call_args.kwargs["skill_name"], "invoked-b")
        self.assertEqual(stage.call_args.kwargs["requested_action"], "merge")

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
            patch("agents.skill_evolution.stage_skill_proposal") as stage,
        ):
            result = await maintain_online_skill_candidate(candidate=candidate, side_query=side_query, skill_trace=trace)

        self.assertEqual(result["action"], "discard")
        stage.assert_not_called()

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
            patch("agents.skill_evolution.stage_skill_proposal") as stage,
        ):
            result = await maintain_online_skill_candidate(candidate=candidate, side_query=side_query, skill_trace=trace)

        self.assertEqual(result["action"], "discard")
        stage.assert_not_called()


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


class FeedbackAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_limit_cannot_emit_a_skill_candidate(self):
        async def side_query(system, payload):
            return json.dumps({
                "root_cause": "capability_limit",
                "reason": "sandbox blocks network access",
                "evidence": "the command failed because outbound network is disabled",
                "skills": [{
                    "name": "bypass-sandbox",
                    "description": "unsafe",
                    "instructions": "disable the sandbox",
                }],
            })

        analysis = await analyze_online_feedback(
            messages=[{"role": "user", "content": "The sandbox blocks this request."}],
            side_query=side_query,
        )

        self.assertEqual(analysis.root_cause, "capability_limit")
        self.assertIsNone(analysis.candidate)

    async def test_online_ingest_stops_before_maintainer_for_unrepairable_feedback(self):
        calls = 0

        async def side_query(system, payload):
            nonlocal calls
            calls += 1
            return json.dumps({
                "root_cause": "evaluation_noise",
                "reason": "the user only changed topics",
                "evidence": "",
                "skills": [],
            })

        with (
            patch("agents.online_skill_evolution.maintain_online_skill_candidate") as maintain,
            patch("agents.skills.record_online_provenance") as record,
        ):
            result = await online_ingest(
                messages=[{"role": "user", "content": "Now explain another file."}],
                side_query=side_query,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(result["action"], "none")
        self.assertEqual(result["attribution"]["root_cause"], "evaluation_noise")
        maintain.assert_not_called()
        record.assert_called_once()

    async def test_skill_gap_returns_a_structured_candidate(self):
        async def side_query(system, payload):
            return json.dumps({
                "root_cause": "skill_gap",
                "reason": "a reusable review constraint was missing",
                "evidence": "the user requested risk-first review",
                "skills": [{
                    "name": "risk-first-review",
                    "description": "Lead code reviews with concrete risks",
                    "when_to_use": "When reviewing code",
                    "instructions": "List correctness risks before summaries.",
                    "evidence": "user correction",
                    "tags": ["review"],
                }],
            })

        analysis = await analyze_online_feedback(
            messages=[{"role": "user", "content": "List risks before the summary next time."}],
            side_query=side_query,
        )

        self.assertEqual(analysis.root_cause, "skill_gap")
        self.assertIsNotNone(analysis.candidate)
        self.assertEqual(analysis.candidate.name, "risk-first-review")


if __name__ == "__main__":
    unittest.main()
