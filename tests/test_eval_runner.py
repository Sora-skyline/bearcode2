from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.runner import (
    _copy_mcp_config,
    _run_job,
    _write_json,
    redact_secrets,
    run_evaluation,
    score_run,
)


class RunnerTests(unittest.TestCase):
    def test_secret_redaction(self):
        redacted = redact_secrets({
            "api_key": "sk-supersecret123",
            "nested": {"message": "Bearer sk-anothersecret456"},
            "safe": "model-name",
        })
        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertNotIn("sk-", redacted["nested"]["message"])
        self.assertEqual(redacted["safe"], "model-name")

    def test_subprocess_gets_isolated_home_and_cwd_and_resumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            captured = {}

            def fake_run(command, **kwargs):
                job_path = Path(command[-1])
                job = json.loads(job_path.read_text(encoding="utf-8"))
                captured["home"] = kwargs["env"]["HOME"]
                captured["workspace"] = job["workspace"]
                captured["cwd"] = str(kwargs["cwd"])
                Path(job["result_path"]).write_text(
                    json.dumps({"job_id": job["job_id"], "rows": [{"id": "1"}]}),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="ok", stderr="")

            job = {
                "job_id": "isolation",
                "suite": "folding",
                "preset": "low-cost",
                "condition": "folding_off",
                "model": "fake",
                "sample": {},
            }
            with patch("evals.runner.subprocess.run", side_effect=fake_run) as mocked:
                _run_job(job, run_dir, timeout_s=10)
                self.assertEqual(mocked.call_count, 1)
                _run_job(job, run_dir, timeout_s=10)
                self.assertEqual(mocked.call_count, 1)
            self.assertNotEqual(captured["home"], str(Path.home()))
            self.assertNotEqual(captured["workspace"], captured["cwd"])
            self.assertIn("workspaces", captured["workspace"])

    def test_cached_scoring_does_not_construct_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            _write_json(run_dir / "config.json", {
                "run_id": "cached",
                "suite": "skill",
                "preset": "low-cost",
                "model": "fake",
                "git_sha": "deadbeef",
            })
            row = {
                "id": "sample",
                "condition": "skill_off",
                "phase": "heldout",
                "family": "BookHotel",
                "prediction": "",
                "gold_calls": [],
                "expected_skill_names": [],
                "trace": [],
                "tokens": {},
            }
            (run_dir / "predictions.jsonl").write_text(
                json.dumps(row) + "\n", encoding="utf-8"
            )
            with patch("agents.agent.Agent", side_effect=AssertionError("model path used")):
                metrics = score_run(run_dir)
            self.assertEqual(metrics["suite"], "skill")
            self.assertTrue((run_dir / "metrics.json").is_file())
            self.assertTrue((run_dir / "report.md").is_file())

    def test_dry_run_materializes_artifacts_without_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "dry"
            with patch("evals.runner._git_sha", return_value="deadbeef"), patch(
                "evals.runner._run_job", side_effect=AssertionError("worker invoked")
            ):
                result = run_evaluation(
                    suite="folding",
                    preset="low-cost",
                    model="fake",
                    run_dir=run_dir,
                    dry_run=True,
                )
            self.assertEqual(result, run_dir.resolve())
            self.assertTrue((run_dir / "config.json").is_file())
            self.assertTrue((run_dir / "predictions.jsonl").is_file())
            metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
            self.assertTrue(metrics["dry_run"])
            self.assertEqual(len(metrics["planned_jobs"]), 8)

    def test_mcp_config_is_copied_only_into_isolated_locations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_home = root / "source-home"
            source_repo = root / "source-repo"
            isolated_home = root / "isolated-home"
            workspace = root / "workspace"
            (source_home / ".bear").mkdir(parents=True)
            (source_repo / ".bear").mkdir(parents=True)
            isolated_home.mkdir()
            workspace.mkdir()
            (source_home / ".bear" / "settings.json").write_text('{"user": true}')
            (source_repo / ".mcp.json").write_text('{"mcpServers": {}}')
            with patch("evals.runner.Path.home", return_value=source_home), patch(
                "evals.runner.REPO_ROOT", source_repo
            ):
                _copy_mcp_config(workspace, isolated_home)
            self.assertTrue((isolated_home / ".bear" / "settings.json").is_file())
            self.assertTrue((workspace / ".mcp.json").is_file())

    def test_json_writer_redacts_keys_on_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            _write_json(path, {"OPENAI_API_KEY": "sk-leaked-secret"})
            self.assertNotIn("sk-leaked", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
