#!/usr/bin/env python3
"""외부 패키지 없이 공통 실행 프로필과 런타임별 모델표를 검증한다."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import execution_profiles as ep  # noqa: E402


class ProfileContractTests(unittest.TestCase):
    def test_profiles_carry_no_runtime_model_names(self) -> None:
        # 프로필은 비용·품질 계약이지 모델명이 아니다. 모델명은 런타임 표에만 있어야 한다.
        for profile, contract in ep.EXECUTION_PROFILES.items():
            self.assertNotIn("model", contract, profile)
            self.assertNotIn("codex_model", contract, profile)
            self.assertNotIn("codex_agent", contract, profile)
            self.assertIn(contract["executor"], {"python", "subagent"})
        self.assertEqual(set(ep.PROFILE_ORDER), set(ep.EXECUTION_PROFILES))
        self.assertEqual(set(ep.SUBAGENT_PROFILES), {p for p, c in ep.EXECUTION_PROFILES.items() if c["executor"] == "subagent"})

    def test_every_subagent_profile_resolves_in_every_runtime(self) -> None:
        for runtime in ep.RUNTIMES:
            for profile in ep.SUBAGENT_PROFILES:
                resolved = ep.resolve(profile, runtime)
                self.assertEqual(runtime, resolved["runtime"])
                self.assertEqual(ep.EXECUTION_PROFILES[profile]["agent"], resolved["agent"])
                self.assertTrue(resolved["model"], (runtime, profile))
                self.assertIn(resolved["effort"], ep.EFFORT_LEVELS, (runtime, profile))
        local = ep.resolve("local_python", "claude")
        self.assertIsNone(local["model"])
        self.assertIsNone(local["effort"])
        self.assertEqual("python", local["executor"])

    def test_claude_table_pins_full_model_ids_sonnet_for_economy_opus_for_quality_and_review(self) -> None:
        # 별칭이 아니라 전체 ID로 고정해, 새 세대가 나와도 검증 전에는 사용감이 바뀌지 않는다.
        table = ep.RUNTIME_MODEL_TABLES["claude"]
        self.assertEqual("claude-sonnet-5", table["economy_high"]["model"])
        self.assertEqual("claude-opus-5", table["review_high"]["model"])
        self.assertEqual("claude-opus-5", table["quality_high"]["model"])
        self.assertEqual("claude-opus-5", table["quality_xhigh"]["model"])
        for row in table.values():
            self.assertNotIn(row["model"], {"sonnet", "opus", "haiku", "inherit"}, "별칭 금지")
        for runtime, rows in ep.RUNTIME_MODEL_TABLES.items():
            for profile, row in rows.items():
                self.assertNotIn(row["model"], ep.FORBIDDEN_MODELS[runtime], (runtime, profile))

    def test_codex_table_matches_project_policy(self) -> None:
        # Codex 값은 기존 정책의 회귀 기준이다.
        table = ep.RUNTIME_MODEL_TABLES["codex"]
        self.assertEqual({"model": "gpt-5.6-luna", "effort": "high"}, table["economy_high"])
        self.assertEqual({"model": "gpt-6-astra", "effort": "high"}, table["review_high"])
        self.assertEqual({"model": "gpt-6-astra", "effort": "medium"}, table["quality_high"])
        self.assertEqual({"model": "gpt-6-astra", "effort": "high"}, table["quality_xhigh"])

    def test_snapshot_table_overrides_project_table(self) -> None:
        # 상태 파일에 남긴 스냅샷이 우선해야, 표가 나중에 바뀌어도 과거 실행을 같은 기준으로 검증한다.
        snapshot = ep.runtime_table("claude")
        snapshot["economy_high"]["model"] = "sonnet-legacy"
        resolved = ep.resolve("economy_high", "claude", table=snapshot)
        self.assertEqual("sonnet-legacy", resolved["model"])
        # 모델표 변경이 진행 중인 Codex 실행의 모델·effort를 소급 변경하지 않는다.
        codex_snapshot = ep.runtime_table("codex")
        for profile, effort in (("review_high", "high"), ("quality_high", "high"), ("quality_xhigh", "xhigh")):
            codex_snapshot[profile].update(model="gpt-5.6-sol", effort=effort)
            resolved = ep.resolve(profile, "codex", table=codex_snapshot)
            self.assertEqual(("gpt-5.6-sol", effort), (resolved["model"], resolved["effort"]))
        with self.assertRaises(ep.ProfileError):
            ep.resolve("economy_high", "unknown-runtime")
        with self.assertRaises(ep.ProfileError):
            ep.resolve("no-such-profile", "claude")


class RuntimeDetectionTests(unittest.TestCase):
    def test_detects_claude_and_codex_from_environment(self) -> None:
        self.assertEqual("claude", ep.detect_runtime({"CLAUDECODE": "1"}))
        self.assertEqual("codex", ep.detect_runtime({"CODEX_SANDBOX": "seatbelt"}))

    def test_ambiguous_or_missing_signals_return_none(self) -> None:
        self.assertIsNone(ep.detect_runtime({}))
        self.assertIsNone(ep.detect_runtime({"CLAUDECODE": "1", "CODEX_SANDBOX": "x"}))
        # Claude 세션 안의 codex-companion 플러그인 변수는 Codex 신호가 아니다.
        self.assertEqual("claude", ep.detect_runtime({"CLAUDECODE": "1", "CODEX_COMPANION_SESSION_ID": "x"}))
        self.assertIsNone(ep.detect_runtime({"CODEX_COMPANION_SESSION_ID": "x"}))


if __name__ == "__main__":
    unittest.main()
