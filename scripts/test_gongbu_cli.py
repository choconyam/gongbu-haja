#!/usr/bin/env python3
"""`gongbu` 전역 명령이 과목 폴더 기준으로 엔진 스크립트를 호출하는지 검증한다."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from gongbu_haja import __version__, cli, paths  # noqa: E402


def cli_env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("CLAUDE", "CODEX_"))}
    env.pop(paths.ENGINE_HOME_ENV, None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def run_gongbu(*arguments: str, cwd: Path, expected: int = 0, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "gongbu_haja", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env or cli_env(),
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


class EngineLocationTests(unittest.TestCase):
    def test_repo_checkout_is_used_when_no_bundled_engine(self) -> None:
        engine = paths.engine_root(environ={})
        self.assertEqual(REPO_ROOT, engine)
        self.assertTrue((engine / "scripts" / "manage_run.py").is_file())
        self.assertEqual("repo", paths.install_kind(engine, environ={}))

    def test_env_override_must_point_at_a_real_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(paths.EngineNotFoundError):
                paths.engine_root(environ={paths.ENGINE_HOME_ENV: temporary})
            fake = Path(temporary)
            (fake / "scripts").mkdir()
            (fake / "scripts" / "manage_run.py").write_text("", encoding="utf-8")
            (fake / "agent_prompts").mkdir()
            (fake / "AGENTS.md").write_text("# x", encoding="utf-8")
            self.assertEqual(fake.resolve(), paths.engine_root(environ={paths.ENGINE_HOME_ENV: temporary}))

    def test_describe_points_state_and_output_inside_course_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            course = Path(temporary)
            info = paths.describe(course, environ={})
            self.assertEqual(__version__, info["version"])
            self.assertEqual(str(course.resolve() / ".gongbu"), info["state_root"])
            self.assertEqual(str(course.resolve() / "output"), info["output_root"])
            self.assertEqual(str(REPO_ROOT / "agent_prompts"), info["prompts_dir"])


class ArgumentInjectionTests(unittest.TestCase):
    COURSE = Path("C:/과목") if os.name == "nt" else Path("/과목")

    def test_record_defaults_input_root_to_course_folder(self) -> None:
        argv = cli.build_argv("record", ["--lecture-id", "w2"], self.COURSE)
        self.assertEqual(["--lecture-id", "w2", "--input-root", str(self.COURSE)], argv)
        # 사용자가 출력을 정했거나 장치 목록만 볼 때는 손대지 않는다.
        self.assertEqual(["--output", "x.wav"], cli.build_argv("record", ["--output", "x.wav"], self.COURSE))
        self.assertEqual(["--list-devices"], cli.build_argv("record", ["--list-devices"], self.COURSE))

    def test_transcribe_defaults_output_root_to_state_folder(self) -> None:
        argv = cli.build_argv("transcribe", ["a.m4a"], self.COURSE)
        self.assertEqual(["a.m4a", "--output-root", str(self.COURSE / ".gongbu")], argv)
        argv = cli.build_argv("transcribe-batch", ["a.m4a", "--output-root=out"], self.COURSE)
        self.assertEqual(["a.m4a", "--output-root=out"], argv)

    def test_run_init_gets_state_root_but_other_run_commands_pass_through(self) -> None:
        argv = cli.build_argv("run", ["init", "2주차", "--lecture-id", "w2"], self.COURSE)
        self.assertEqual(["init", "2주차", "--lecture-id", "w2", "--state-root", str(self.COURSE / ".gongbu")], argv)
        argv = cli.build_argv("run", ["next", "state.json"], self.COURSE)
        self.assertEqual(["next", "state.json"], argv)
        argv = cli.build_argv("run", ["init", "x", "--root", "r"], self.COURSE)
        self.assertNotIn("--state-root", argv)

    def test_validate_picks_script_by_target(self) -> None:
        self.assertEqual(("validate_note_output.py", ["note.md"]), cli.resolve_script("validate", ["note", "note.md"]))
        self.assertEqual(("build_study_note_pdf.py", ["a.md", "--output", "a.pdf"]), cli.resolve_script("build", ["a.md", "--output", "a.pdf"]))
        with self.assertRaises(ValueError):
            cli.resolve_script("validate", ["nope"])
        with self.assertRaises(ValueError):
            cli.resolve_script("dance", [])


class SubprocessTests(unittest.TestCase):
    def test_prepare_sources_runs_in_course_folder_with_two_role_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            course = Path(temporary).resolve()
            inputs = course / "lesson"
            inputs.mkdir()
            (inputs / "lecture.txt").write_text("정의와 예시를 모두 보존한다.\n", encoding="utf-8")
            result = run_gongbu("run", "init", str(inputs), "--lecture-id", "lesson",
                                "--runtime", "codex", "--note-mode", "faithful", cwd=course)
            state = Path(result.stdout.strip())
            prepared = json.loads(run_gongbu("prepare-sources", str(state), "--output-dir",
                                             str(state.parent / "prepared"), cwd=course).stdout)
            self.assertEqual(1, prepared["source_files"])
            self.assertFalse(prepared["semantic_reviewed"])
            self.assertTrue(Path(prepared["source_map"]).is_file())
            next_payload = json.loads(run_gongbu("run", "next", str(state), "--brief", cwd=course).stdout)
            self.assertEqual("deterministic", next_payload["preprocessing"])
            self.assertNotIn("runtime_model_table", next_payload)

    def test_paths_reports_course_folder_as_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_gongbu("paths", cwd=Path(temporary))
            info = json.loads(result.stdout)
            self.assertEqual(str(Path(temporary).resolve() / ".gongbu"), info["state_root"])
            self.assertEqual(str(REPO_ROOT), info["engine_root"])

    def test_version_and_help(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(__version__, run_gongbu("version", cwd=Path(temporary)).stdout.strip())
            self.assertIn("gongbu", run_gongbu("--help", cwd=Path(temporary)).stdout)
            run_gongbu(cwd=Path(temporary), expected=2)

    def test_setup_creates_state_output_and_gitignore_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            course = Path(temporary)
            (course / ".gitignore").write_text("*.wav\n", encoding="utf-8")
            first = json.loads(run_gongbu("setup", cwd=course).stdout)
            self.assertTrue((course / ".gongbu").is_dir())
            self.assertTrue((course / "output").is_dir())
            self.assertIn("/.gongbu/", first["gitignore_added"])
            self.assertNotIn("*.wav", first["gitignore_added"])
            second = json.loads(run_gongbu("setup", cwd=course).stdout)
            self.assertEqual([], second["created"])
            self.assertEqual([], second["gitignore_added"])
            text = (course / ".gitignore").read_text(encoding="utf-8")
            self.assertEqual(1, text.count("/.gongbu/"))

    def test_run_init_in_course_folder_keeps_state_out_of_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            # CI의 Windows TEMP는 8.3 단축 경로(RUNNER~1)라서, CLI가 돌려주는 resolve()된 경로와 맞추려면 먼저 푼다.
            course = Path(temporary).resolve()
            lecture = course / "2026-03-10_과목A"
            lecture.mkdir()
            (lecture / "교안.pdf").write_bytes(b"%PDF-1.4 handout")
            (lecture / ".gongbu").mkdir()
            (lecture / ".gongbu" / "stale.json").write_text("{}", encoding="utf-8")
            (lecture / "output").mkdir()
            (lecture / "output" / "old_note.md").write_text("# old", encoding="utf-8")
            (lecture / ".gitignore").write_text("*.wav\n", encoding="utf-8")
            result = run_gongbu(
                "run", "init", str(lecture), "--lecture-id", "2026-03-10_과목A",
                "--runtime", "codex", "--note-mode", "faithful",
                cwd=course,
            )
            state_file = course / ".gongbu" / "2026-03-10_과목A" / "run_state.json"
            self.assertEqual(str(state_file), result.stdout.strip())
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(["교안.pdf"], [item["path"] for item in state["inputs"]])
            self.assertEqual(str(course.resolve() / ".gongbu"), state["state_root"])
            self.assertEqual(str(REPO_ROOT), state["engine_root"])
            payload = json.loads(run_gongbu("run", "next", str(state_file), cwd=course).stdout)
            self.assertEqual(str(REPO_ROOT / "agent_prompts"), payload["prompt_root"])

    def test_setup_agents_installs_user_scope_declarations_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            (home / ".codex").mkdir(parents=True)
            (home / ".codex" / "config.toml").write_text('model = "gpt-5.6-luna"\n', encoding="utf-8")
            first = json.loads(run_gongbu("setup-agents", "--home", str(home), cwd=Path(temporary)).stdout)
            self.assertEqual(9, len(first["written"]))
            self.assertTrue((home / ".claude" / "agents" / "study-note-worker.md").is_file())
            self.assertTrue((home / ".codex" / "agents" / "deep-note-reviewer.toml").is_file())
            config = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
            self.assertTrue(config.startswith('model = "gpt-5.6-luna"\n'))
            self.assertIn("[agents]", config)
            second = json.loads(run_gongbu("setup-agents", "--home", str(home), cwd=Path(temporary)).stdout)
            self.assertEqual([], second["written"])
            self.assertEqual(1, len(second["notices"]))
            self.assertEqual(1, config.count("[agents]"))


if __name__ == "__main__":
    unittest.main()
