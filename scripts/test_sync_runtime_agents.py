#!/usr/bin/env python3
"""외부 패키지 없이 런타임별 서브 에이전트 선언 생성기를 검증한다."""

from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import execution_profiles as ep  # noqa: E402
import sync_runtime_agents as sra  # noqa: E402


class GeneratedDeclarationTests(unittest.TestCase):
    def test_sync_creates_all_declarations_and_second_run_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            written = sra.sync(root)
            self.assertEqual(1 + 2 * len(ep.SUBAGENT_PROFILES), len(written))
            self.assertEqual([], sra.drift_report(root))
            self.assertEqual([], sra.sync(root))

    def test_codex_toml_matches_table_and_parses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sra.sync(root)
            config = tomllib.loads((root / ".codex" / "config.toml").read_text(encoding="utf-8"))
            economy = ep.RUNTIME_MODEL_TABLES["codex"]["economy_high"]
            self.assertEqual(economy["model"], config["agents"]["default_subagent_model"])
            self.assertEqual(economy["effort"], config["agents"]["default_subagent_reasoning_effort"])
            self.assertEqual(ep.SUBAGENT_CONCURRENCY_LIMIT, config["agents"]["max_concurrent_threads_per_session"])
            for profile in ep.SUBAGENT_PROFILES:
                agent = ep.EXECUTION_PROFILES[profile]["agent"]
                path = root / ".codex" / "agents" / f"{sra.agent_file_stem(agent)}.toml"
                payload = tomllib.loads(path.read_text(encoding="utf-8"))
                row = ep.RUNTIME_MODEL_TABLES["codex"][profile]
                self.assertEqual(agent, payload["name"])
                self.assertEqual(row["model"], payload["model"])
                self.assertEqual(row["effort"], payload["model_reasoning_effort"])
                self.assertEqual(ep.AGENT_INSTRUCTIONS[agent], payload["developer_instructions"])

    def test_claude_markdown_has_model_and_effort_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sra.sync(root)
            for profile in ep.SUBAGENT_PROFILES:
                agent = ep.EXECUTION_PROFILES[profile]["agent"]
                text = (root / ".claude" / "agents" / f"{sra.agent_file_stem(agent)}.md").read_text(encoding="utf-8")
                row = ep.RUNTIME_MODEL_TABLES["claude"][profile]
                self.assertTrue(text.startswith(f"---\nname: {agent}\n"))
                self.assertIn(f"\nmodel: {row['model']}\n", text)
                self.assertIn(f"\neffort: {row['effort']}\n", text)
                self.assertIn(ep.AGENT_INSTRUCTIONS[agent], text)
                for forbidden in ep.FORBIDDEN_MODELS["claude"]:
                    self.assertNotIn(f"model: {forbidden}", text)

    def test_drift_detects_edits_and_stray_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sra.sync(root)
            edited = root / ".claude" / "agents" / "study-note-worker.md"
            edited.write_text(edited.read_text(encoding="utf-8") + "\n손으로 고침\n", encoding="utf-8")
            (root / ".codex" / "agents" / "ghost.toml").write_text('name = "ghost"\n', encoding="utf-8")
            problems = sra.drift_report(root)
            self.assertTrue(any("study-note-worker.md" in problem for problem in problems))
            self.assertTrue(any("ghost.toml" in problem for problem in problems))


if __name__ == "__main__":
    unittest.main()
