from __future__ import annotations

import unittest

from evals.datasets import load_skill_curriculum, load_toolhop_samples
from evals.scorers import (
    api_call_metrics,
    score_folding_predictions,
    score_skill_predictions,
)


class DatasetAdapterTests(unittest.TestCase):
    def test_fixed_low_cost_curriculum(self):
        curriculum = load_skill_curriculum()
        self.assertEqual(list(curriculum["families"]), ["BookHotel", "QueryStock"])
        self.assertEqual(
            list(curriculum["families"]["BookHotel"]),
            ["add", "merge", "discard", "heldout"],
        )
        self.assertEqual(curriculum["unrelated"]["family"], "QueryRegistration")
        schema = curriculum["families"]["BookHotel"]["add"]["tools"][0]
        self.assertEqual(schema["name"], "BookHotel")
        self.assertEqual(
            schema["input_schema"]["properties"]["room_count"]["type"], "integer"
        )

    def test_toolhop_is_first_four_long_chain_samples(self):
        samples = load_toolhop_samples()
        self.assertEqual([sample["id"] for sample in samples], ["0", "1", "2", "3"])
        self.assertTrue(all(len(sample["subtasks"]) >= 4 for sample in samples))
        self.assertTrue(all("functions" not in sample for sample in samples))


class ScorerTests(unittest.TestCase):
    def test_api_call_name_and_unordered_json_exact_match(self):
        gold = [{"name": "BookHotel", "arguments": {"a": 1, "b": 2}}]
        predicted = [{"name": "BookHotel", "arguments": {"b": 2, "a": 1}}]
        self.assertEqual(api_call_metrics(predicted, gold)["task_success"], 1.0)
        wrong_type = [{"name": "BookHotel", "arguments": {"a": "1", "b": 2}}]
        self.assertEqual(api_call_metrics(wrong_type, gold)["api_call_accuracy"], 0.0)
        extra = predicted + [{"name": "BookHotel", "arguments": {"a": 3, "b": 4}}]
        self.assertEqual(api_call_metrics(extra, gold)["api_call_accuracy"], 0.5)

    def test_skill_retrieval_and_evolution_metrics(self):
        rows = [
            {
                "id": "train",
                "condition": "evolved_skill",
                "phase": "add",
                "family": "BookHotel",
                "gold_calls": [],
                "evolution_expected": "add",
                "evolution_observed": "add",
                "expected_skill_names": [],
                "trace": [],
                "tokens": {},
            },
            {
                "id": "held",
                "condition": "evolved_skill",
                "phase": "heldout",
                "family": "BookHotel",
                "gold_calls": [{"name": "BookHotel", "arguments": {"a": 1}}],
                "expected_skill_names": ["hotel-skill"],
                "trace": [
                    {"event": "skill_retrieval", "hits": [
                        {"name": "other"}, {"name": "hotel-skill"}
                    ]},
                    {"event": "skill_activation", "skill_name": "hotel-skill", "found": True},
                    {"event": "tool_call", "phase": "start", "name": "BookHotel", "input": {"a": 1}},
                ],
                "tokens": {},
            },
        ]
        metrics = score_skill_predictions(rows)["conditions"]["evolved_skill"]
        self.assertEqual(metrics["task_success"], 1.0)
        self.assertEqual(metrics["recall_at_3"], 1.0)
        self.assertEqual(metrics["mrr"], 0.5)
        self.assertEqual(metrics["skill_activation_rate"], 1.0)
        self.assertEqual(metrics["evolution_action_accuracy"], 1.0)

    def test_toolhop_path_repeat_memory_and_hallucination_metrics(self):
        tools = [
            {"name": "lookup_a"},
            {"name": "lookup_b"},
        ]
        trace = [
            {"event": "tool_call", "phase": "start", "name": "lookup_a", "input": {}},
            {"event": "tool_call", "phase": "end", "name": "lookup_a", "output": "Alice"},
            {"event": "tool_call", "phase": "start", "name": "lookup_a", "input": {}},
            {"event": "tool_call", "phase": "end", "name": "lookup_a", "output": "Alice"},
            {"event": "tool_call", "phase": "start", "name": "lookup_b", "input": {}},
            {"event": "tool_call", "phase": "end", "name": "lookup_b", "output": "Bob"},
            {
                "event": "fold",
                "compression_ratio": 0.25,
                "fallback": False,
                "memory": {
                    "episode_memory": {"current_progress": "Alice"},
                    "tool_memory": {"tools_used": [
                        {"tool_name": "lookup_a"},
                        {"tool_name": "invented_tool"},
                    ]},
                },
            },
        ]
        rows = [{
            "id": "x",
            "condition": "folding_structured",
            "prediction": "ANSWER: done",
            "answer": "done",
            "subtasks": {"q1": "Alice", "q2": "Bob"},
            "tools": tools,
            "trace": trace,
            "tokens": {},
        }]
        sample = score_folding_predictions(rows)["samples"][0]
        self.assertEqual(sample["pass_at_1"], 1.0)
        self.assertEqual(sample["path_score"], 1.0)
        self.assertAlmostEqual(sample["repeat_tool_call_rate"], 1 / 3)
        self.assertEqual(sample["known_fact_recall"], 0.5)
        self.assertEqual(sample["unknown_tool_hallucination_rate"], 0.5)
        self.assertEqual(sample["compression_ratio"], 0.25)
        self.assertFalse(sample["invalid_intervention"])

    def test_structured_without_fold_is_invalid_intervention(self):
        metrics = score_folding_predictions([{
            "id": "x",
            "condition": "folding_structured",
            "prediction": "ANSWER: 1",
            "answer": "1",
            "subtasks": {},
            "tools": [],
            "trace": [],
            "tokens": {},
        }])
        self.assertTrue(metrics["samples"][0]["invalid_intervention"])
        self.assertEqual(metrics["conditions"]["folding_structured"]["valid_effect_samples"], 0)
        self.assertIsNone(metrics["conditions"]["folding_structured"]["pass_at_1"])


if __name__ == "__main__":
    unittest.main()
