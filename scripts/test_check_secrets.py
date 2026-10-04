"""비밀값·개인 자료 커밋 검사를 실제 임시 Git 저장소로 확인한다.

가짜 토큰은 실행 중에 조립한다. 문자열 그대로 적으면 이 파일 자체가 커밋 검사에 걸린다.
"""

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_secrets as secrets


def fake(prefix, length=40):
    """실제 키처럼 생긴 가짜 값을 만든다."""
    return prefix + ("a1B2c3D4e5" * (length // 10 + 1))[:length]


def git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=root, capture_output=True, check=True, text=True,
    ).stdout.strip()


class ScanTests(unittest.TestCase):
    def test_known_token_shapes_are_found_and_masked(self):
        samples = {
            "노션 연동 토큰": fake("ntn" + "_", 46),
            "Anthropic API 키": fake("sk-" + "ant-api03-"),
            "OpenAI API 키": fake("sk-" + "proj-"),
            "GitHub 토큰": fake("gh" + "p_", 36),
            "Google API 키": fake("AI" + "za", 35),
            "AWS 액세스 키": "AK" + "IA" + "ABCDEFGH12345678",
            "비공개 키": "-----BEGIN " + "RSA PRIVATE KEY-----",
        }
        for kind, value in samples.items():
            with self.subTest(kind=kind):
                self.assertEqual(secrets.scan_line(f"value: {value}"), (kind, value[:4] + "…"))

    def test_secret_assignment_needs_secret_name_and_real_looking_value(self):
        name, value = "NOTION" + "_TOKEN", fake("", 20)
        self.assertEqual(secrets.scan_line(f'{name} = "{value}"'), ("비밀값 대입", f"{name}=…"))
        self.assertEqual(secrets.scan_line(f"{name}={value}")[0], "비밀값 대입")
        for line in (
            "token = keyring.get_password(SERVICE, ACCOUNT)",
            "NOTION_TOKEN=<토큰>",
            'KEYRING_SERVICE = "gongbu-haja-notion-2026"',
            'token_url = "https://api.notion.com/v1/oauth/token"',
            'max_tokens = "12345678901234567890"',
            'api_key = "your-api-key-goes-here-123"',
        ):
            with self.subTest(line=line):
                self.assertIsNone(secrets.scan_line(line))

    def test_secret_name(self):
        for name in ("NOTION_TOKEN", "apiKey", "accessToken", "AWS_SECRET_ACCESS_KEY", "GITHUB_PAT", "client_secret", "password"):
            with self.subTest(name=name):
                self.assertTrue(secrets.secret_name(name))
        for name in ("MAX_TOKENS", "TOKEN_SERVICE", "tokenizer", "KEYRING_SERVICE", "token_url", "PATH"):
            with self.subTest(name=name):
                self.assertFalse(secrets.secret_name(name))

    def test_blocked_paths(self):
        blocked = {
            ".env": "비밀값·인증 파일",
            "config/.env.local": "비밀값·인증 파일",
            "certs/server.pem": "비밀값·인증 파일",
            "cookies.json": "비밀값·인증 파일",
            "input/lecture.pdf": "강의 자료·실행 상태·브라우저 인증 폴더",
            "physics/.gongbu/w1/state.json": "강의 자료·실행 상태·브라우저 인증 폴더",
            "workspace/w1/note.md": "강의별 작업 산출물",
            "recordings/w1.m4a": "녹음·녹화 파일",
        }
        for path, reason in blocked.items():
            with self.subTest(path=path):
                self.assertEqual(secrets.blocked_path(path), reason)
        for path in (".env.example", "workspace/README.md", "scripts/check_secrets.py", "docs/output-format.md"):
            with self.subTest(path=path):
                self.assertIsNone(secrets.blocked_path(path))


class GitCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "core.autocrlf", "false")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")

    def commit(self, message):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def check(self, *args, stdin=""):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), patch("sys.stdin", io.StringIO(stdin)):
            code = secrets.main(["--root", str(self.root), *args])
        return code, errors.getvalue()

    def test_staged_token_stops_commit_without_printing_it(self):
        token = fake("ntn" + "_", 46)
        self.write("notes.md", "깨끗한 노트\n")
        git(self.root, "add", "notes.md")
        self.assertEqual(self.check()[0], 0)
        self.write("notes.md", f"연동 키: {token}\n")
        git(self.root, "add", "notes.md")
        code, errors = self.check()
        self.assertEqual(code, 1)
        self.assertIn("notes.md:1 — 노션 연동 토큰", errors)
        self.assertNotIn(token[4:], errors)

    def test_force_added_secret_file_is_blocked(self):
        self.write(".env", "LOCAL=1\n")
        git(self.root, "add", "-f", ".env")
        code, errors = self.check()
        self.assertEqual(code, 1)
        self.assertIn(".env — 비밀값·인증 파일", errors)

    def test_all_mode_scans_every_tracked_file(self):
        self.write("a.md", "괜찮음\n")
        self.commit("clean")
        self.assertEqual(self.check("--all")[0], 0)
        self.write("b.py", f'GITHUB = "{fake("gh" + "p_", 36)}"\n')
        git(self.root, "add", "b.py")
        code, errors = self.check("--all")
        self.assertEqual(code, 1)
        self.assertIn("b.py:1 — GitHub 토큰", errors)

    def test_pre_push_checks_history_even_if_secret_was_removed_later(self):
        self.write("a.md", f"{fake('sk-' + 'ant-api03-')}\n")
        leaked = self.commit("leak")
        self.write("a.md", "지움\n")
        head = self.commit("remove")
        zero = "0" * 40
        code, errors = self.check("--pre-push", stdin=f"refs/heads/main {head} refs/heads/main {zero}\n")
        self.assertEqual(code, 1)
        self.assertIn(f"{leaked[:8]} a.md:1 — Anthropic API 키", errors)
        self.write("b.md", "새 내용\n")
        newer = self.commit("clean")
        self.assertEqual(self.check("--pre-push", stdin=f"refs/heads/main {newer} refs/heads/main {head}\n")[0], 0)


if __name__ == "__main__":
    unittest.main()
