"""Claude Code 보안 훅이 막을 도구 호출과 통과시킬 호출을 확인한다.

가짜 토큰은 실행 중에 조립한다. 문자열 그대로 적으면 이 파일 자체가 커밋 검사에 걸린다.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from scripts import guard_secrets as guard
from scripts.test_check_secrets import fake

SCRIPT = Path(__file__).with_name("guard_secrets.py")

DENIED_COMMANDS = (
    # 비밀값·인증 파일
    "cat .env",
    "type .env.local",
    "Get-Content -Path C:\\proj\\.env",
    "python -c \"print(open('.env').read())\"",
    "cat ~/.ssh/id_rsa",
    "cat ~/.aws/credentials",
    # 환경 변수 전체·명령 기록 출력
    "env",
    "env | sort",
    "printenv",
    "export",
    "set",
    "Get-ChildItem env:",
    "gci Env:\\",
    "[Environment]::GetEnvironmentVariables()",
    "cmd /c set",
    "python -c \"import os; print(os.environ)\"",
    "node -e \"console.log(process.env)\"",
    "cat /proc/self/environ",
    "reg query HKCU\\Environment",
    "Get-Content (Get-PSReadLineOption).HistorySavePath",
    # 비밀값 변수
    "echo $NOTION_TOKEN",
    "echo ${GITHUB_TOKEN}",
    "Write-Output $env:ANTHROPIC_API_KEY",
    "echo %OPENAI_API_KEY%",
    "printenv GONGBU_NOTION_TOKEN",
    "setx NOTION_TOKEN placeholder",
    "python -c \"import os; print(os.environ['NOTION_TOKEN'])\"",
    "[Environment]::GetEnvironmentVariable('GONGBU_NOTION_TOKEN', 'User')",
    # 자격 증명 보관소·로그인 토큰
    "python -m keyring get gongbu-haja notion",
    "python -c \"import keyring; print(keyring.get_password('a', 'b'))\"",
    "cmdkey /list",
    "gh auth token",
    "gh auth status --show-token",
    "git credential fill",
    "security find-generic-password -w -s gongbu",
    "secret-tool lookup service gongbu",
    # 커밋·푸시 검사와 훅 우회
    "git commit --no-verify -m x",
    "git commit -n -m x",
    "git commit -anm x",
    "git -C repo commit -n",
    "git push --no-verify origin main",
    "git config core.hooksPath /dev/null",
    "git config --unset core.hooksPath",
    "git config --global core.hooksPath .githooks",
    "git -c core.hooksPath=/dev/null commit -m x",
    "claude --settings '{\"disableAllHooks\": true}'",
)

ALLOWED_COMMANDS = (
    "git status",
    "git diff -- scripts/check_secrets.py",
    "git add .githooks/pre-commit scripts/check_secrets.py",
    "git commit -F msg.txt",
    "git commit -m \"fix: handle -n flag\"",
    "git commit -am \"update docs\"",
    "git log --format=%H -n 3",
    "git config core.hooksPath .githooks",
    "git config --get core.hooksPath",
    "grep -rn core.hooksPath docs",
    "python scripts/check_secrets.py --all",
    "python -m unittest discover -s scripts -p \"test_*.py\"",
    "bash .githooks/pre-commit",
    "cat .env.example",
    "env NODE_ENV=test npm test",
    "printenv PATH",
    "Get-ChildItem env:PATH",
    "echo $CLAUDE_PROJECT_DIR",
    "$env:GONGBU_FONT_DIR = 'C:\\Windows\\Fonts'",
    "set -euo pipefail",
    "python -m venv .venv",
    "python -c \"import os; print(os.environ.get('PATH'))\"",
    "python -c \"print(item.key)\"",
    "gh auth status",
)

ASKED_COMMANDS = (
    "rm .githooks/pre-commit",
    "chmod -x .githooks/pre-push",
    "sed -i 's/a/b/' scripts/guard_secrets.py",
    "echo {} > .claude/settings.json",
    "Set-Content -Path .claude\\settings.local.json -Value x",
    "git checkout -- scripts/check_secrets.py",
    "python -c \"open('.git/config', 'w')\"",
)


class CommandTests(unittest.TestCase):
    def assertDecision(self, command, expected):
        decision = guard.check_command(command)
        self.assertEqual(decision and decision[0], expected, f"{command!r} → {decision}")

    def test_denied_commands(self):
        for command in DENIED_COMMANDS:
            with self.subTest(command=command):
                self.assertDecision(command, guard.DENY)

    def test_allowed_commands(self):
        for command in ALLOWED_COMMANDS:
            with self.subTest(command=command):
                self.assertDecision(command, None)

    def test_protected_file_changes_need_user_confirmation(self):
        for command in ASKED_COMMANDS:
            with self.subTest(command=command):
                self.assertDecision(command, guard.ASK)

    def test_secret_value_in_command_is_denied_without_echoing_it(self):
        token = fake("ntn" + "_", 46)
        action, reason = guard.check_command(f"curl -H 'Authorization: Bearer {token}' https://api.notion.com/v1/users")
        self.assertEqual(action, guard.DENY)
        self.assertNotIn(token[4:], reason)


class FileToolTests(unittest.TestCase):
    def test_secret_paths_are_denied(self):
        for tool, data in (
            ("Read", {"file_path": "C:\\proj\\.env"}),
            ("Read", {"file_path": "/home/u/.ssh/id_ed25519"}),
            ("Read", {"file_path": "C:/Users/u/.aws/credentials"}),
            ("Grep", {"pattern": "x", "path": ".auth/state.json"}),
            ("Grep", {"pattern": "x", "glob": "*.pem"}),
            ("NotebookEdit", {"notebook_path": "secrets/.env.local", "new_source": "x"}),
        ):
            with self.subTest(tool=tool, data=data):
                self.assertEqual(guard.decide(tool, data)[0], guard.DENY)

    def test_secret_values_in_written_content_are_denied(self):
        name, value = "NOTION" + "_TOKEN", fake("", 20)
        for tool, data in (
            ("Write", {"file_path": "notes.md", "content": f"토큰: {fake('gh' + 'p_', 36)}"}),
            ("Edit", {"file_path": "a.py", "old_string": "a", "new_string": f'{name} = "{value}"'}),
            ("MultiEdit", {"file_path": "a.py", "edits": [{"old_string": "a", "new_string": fake("sk-" + "proj-")}]}),
        ):
            with self.subTest(tool=tool):
                self.assertEqual(guard.decide(tool, data)[0], guard.DENY)

    def test_ordinary_files_and_placeholders_pass(self):
        for tool, data in (
            ("Read", {"file_path": ".env.example"}),
            ("Read", {"file_path": "scripts/guard_secrets.py"}),
            ("Grep", {"pattern": "x", "glob": "*.py"}),
            ("Edit", {"file_path": "README.md", "old_string": "a", "new_string": "NOTION_TOKEN=<토큰>"}),
        ):
            with self.subTest(tool=tool, data=data):
                self.assertIsNone(guard.decide(tool, data))

    def test_protected_files_need_user_confirmation(self):
        for tool, path in (
            ("Edit", ".claude/settings.json"),
            ("Edit", "C:\\Users\\u\\.claude\\settings.json"),
            ("Write", "C:\\proj\\scripts\\guard_secrets.py"),
            ("Write", ".githooks/pre-commit"),
        ):
            with self.subTest(path=path):
                self.assertEqual(guard.decide(tool, {"file_path": path, "content": "x", "new_string": "x"})[0], guard.ASK)


class HookProcessTests(unittest.TestCase):
    def run_hook(self, payload):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return subprocess.run([sys.executable, str(SCRIPT)], input=data, capture_output=True)

    def test_deny_exits_2_with_reason_on_stderr(self):
        token = fake("ntn" + "_", 46)
        result = self.run_hook({"tool_name": "Bash", "tool_input": {"command": f"echo {token}"}})
        self.assertEqual(result.returncode, 2)
        stderr = result.stderr.decode("utf-8")
        self.assertIn("보안 훅", stderr)
        self.assertNotIn(token[4:], stderr)

    def test_ask_returns_permission_decision_json(self):
        result = self.run_hook({"tool_name": "Edit", "tool_input": {"file_path": ".githooks/pre-commit", "new_string": "x"}})
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual((output["hookEventName"], output["permissionDecision"]), ("PreToolUse", "ask"))

    def test_allowed_call_is_silent(self):
        result = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "git status"}})
        self.assertEqual((result.returncode, result.stdout), (0, b""))

    def test_broken_input_fails_closed(self):
        self.assertEqual(self.run_hook(b"{").returncode, 2)


if __name__ == "__main__":
    unittest.main()
