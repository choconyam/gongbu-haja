#!/usr/bin/env python3
"""Claude Code PreToolUse 훅: 비밀값을 읽거나 출력하거나 커밋 검사를 우회하는 도구 호출을 막는다.

`.claude/settings.json`이 셸·파일 도구를 쓰기 직전마다 실행한다. 막을 때는 이유를 stderr에 쓰고
종료 코드 2로 끝내며, 보호 장치 파일을 셸 명령으로 바꾸려 하면 사용자 확인을 요청한다.
흔한 실수와 우회를 막는 장치이지 격리 장치는 아니다. 커밋·푸시의 최종 검사는 check_secrets.py가 맡는다.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

try:
    from .check_secrets import scan_line, secret_file, secret_name
except ImportError:  # 훅이 `python scripts/guard_secrets.py`로 직접 실행할 때
    from check_secrets import scan_line, secret_file, secret_name

DENY, ASK = "deny", "ask"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# 홈 폴더의 자격 증명·명령 기록과 브라우저 인증 위치
CREDENTIAL_PATH_RE = re.compile(
    r"(?i)(?:^|/)(?:\.ssh|\.aws|\.kube|\.gnupg|\.config/gh|PSReadLine|browser-profile|\.auth)(?:/|$)"
    r"|(?:^|/)(?:\.docker/config\.json|\.claude/\.credentials\.json|\.codex/auth\.json"
    r"|\.bash_history|\.zsh_history|ConsoleHost_history\.txt)$"
)
# 고치려면 사용자 확인이 필요한 보호 장치 파일
PROTECTED_PATH_RE = re.compile(
    r"(?i)(?:^|/)(?:\.claude/settings(?:\.local)?\.json|\.githooks(?:/[^/]+)?"
    r"|scripts/(?:guard|check)_secrets\.py|\.git/(?:config|hooks(?:/[^/]+)?))$"
)
SECRET_GLOB_RE = re.compile(
    r"(?i)\.env(?!\.example)|\.envrc|\.(?:pem|key|p12|pfx)\b|id_(?:rsa|dsa|ecdsa|ed25519)"
    r"|cookies|storage-state|credentials\.json|\.netrc|\.git-credentials"
)
WORD_SPLIT_RE = re.compile(r"[\s\"'`=;|&()<>,{}\[\]]+")

COMMAND_START = r"(?:^|[;&|({\n`]|\$\(|\b(?:then|do|else|sudo|exec|xargs|nohup|time|command|builtin)\s)\s*"
COMMAND_END = r"\s*(?=$|[;&|)>}\n`])"
ENV_DUMP_RES = (
    # env·printenv·export·set처럼 인자 없이 쓰면 환경 변수 전체를 출력하는 명령
    re.compile(COMMAND_START + r"(?:env|printenv|export|set|export\s+-p|declare\s+-[a-zA-Z]*[px][a-zA-Z]*"
               r"|typeset\s+-[a-zA-Z]*x[a-zA-Z]*|env(?:\s+-[-\w]+)+)" + COMMAND_END),
    re.compile(r"(?i)(?<![\w$])env:[\\/]?\*?(?=$|[\s;|)'\"}])"),  # PowerShell env: 드라이브 전체
    re.compile(r"(?i)GetEnvironmentVariables\b"),
    re.compile(r"(?i)\bcmd(?:\.exe)?\s+/[ck]\s+[\"']?set\s*[\"']?(?=$|[\s;&|)])"),
    re.compile(r"(?i)\bHK(?:CU|LM|EY_CURRENT_USER|EY_LOCAL_MACHINE):?\\[^\s;|]*Environment"),
    re.compile(r"\bos\.environ\b(?!\s*(?:\[|\.get\b|\.setdefault\b|\.pop\b))"),
    re.compile(r"\bprocess\.env\b(?!\s*[.\[])"),
    re.compile(r"/proc/[^/\s]+/environ"),
    re.compile(r"(?i)\bHistorySavePath\b|\bGet-History\b"),
)
ENV_REFERENCE_RE = re.compile(
    r"(?i)(?:\$env:|\$\{?|%|\benv:|\benviron(?:\.get)?\s*[\[(]\s*['\"]?|\bgetenv\s*\(\s*['\"]?"
    r"|\bprocess\.env\.|\bprocess\.env\[\s*['\"]?|EnvironmentVariable\s*\(\s*['\"]?"
    r"|\b(?:printenv|setx|unset|export|set)\s+(?:-\w+\s+)*)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)
UPPER_NAME_RE = re.compile(r"(?<!\w)[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?!\w)")
CREDENTIAL_COMMAND_RE = re.compile(
    r"""(?ix)
      \bkeyring(?:\.exe)?\s+(?:get|set|del)\b | -m\s+keyring\b
    | \bkeyring\.(?:get_password|get_credential|set_password|delete_password)\b
    | \b(?:cmdkey|vaultcmd)(?:\.exe)?\b
    | \b(?:Get|Set|New|Remove)-StoredCredential\b | \bCredRead\w*\b | \bPasswordVault\b
    | \bConvertFrom-SecureString\b
    | \bsecurity\s+(?:find|dump|export|delete)-\w+
    | \bsecret-tool\s+(?:lookup|search|store|clear)\b
    | \bgh\s+auth\s+(?:token\b|status\b[^\n;|&]*(?:\s-t\b|--show-token)|login\b[^\n;|&]*--with-token)
    | \bgh\s+secret\s+(?:set|delete|remove)\b
    | \bgit\s+credential(?:-[\w-]+)?\s+(?:fill|get|approve|reject|erase|store)\b
    | \bgit\s+credential-manager\b
    """
)
NO_VERIFY_RE = re.compile(r"(?i)\bgit(?:\.exe)?\b[^\n]*?\s--no-verify\b")
GIT_CALL_RE = re.compile(r"(?i)\bgit(?:\.exe)?\b(?P<args>[^\n;|&]*)")
WRITE_COMMAND_RE = re.compile(
    r"(?ix)"
    r"(?:^|[\s;&|(`{])(?:rm|rmdir|mv|cp|ln|chmod|chown|chattr|truncate|touch|tee|install|unlink|shred|dd"
    r"|del|erase|ren|rename|move|copy|xcopy|robocopy|ri|mi|cpi|rni|ni|sc|ac|clc|attrib|icacls|takeown)(?=\s)"
    r"|\bsed\s+(?:-[a-z]*i|--in-place)|\bperl\s+-[a-z]*i"
    r"|\b(?:Set|Add|Clear)-Content\b|\bOut-File\b|\b(?:Remove|Move|Copy|Rename|New)-Item\b|\bSet-ItemProperty\b"
    r"|\bgit\s+(?:rm|mv|checkout|restore|reset|stash|apply|am|clean|update-index)\b"
    r"|\bopen\s*\([^)]*,\s*['\"][wax]|\b(?:write_text|write_bytes|WriteAllText|WriteAllBytes|rmtree)\b"
    r"|\bos\.(?:remove|replace|rename|unlink)\b|\bshutil\.\w+"
)
REDIRECT_RE = re.compile(r"(?<![<>&\d])>{1,2}\s*([^\s;&|)]+)")
COMMIT_VALUE_SHORT = set("mFCct")
COMMIT_VALUE_LONG = {"--message", "--file", "--reuse-message", "--reedit-message", "--template", "--author",
                     "--date", "--cleanup", "--fixup", "--squash", "--trailer", "--pathspec-from-file"}
GIT_VALUE_GLOBAL = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}


def normalized(path: str) -> str:
    return path.replace("\\", "/")


def sensitive_path(path: str) -> bool:
    path = normalized(path)
    return secret_file(path) or bool(CREDENTIAL_PATH_RE.search(path))


def protected_path(path: str) -> bool:
    return bool(PROTECTED_PATH_RE.search(normalized(path)))


def first_secret(text: str) -> tuple[str, str] | None:
    return next((hit for hit in map(scan_line, text.splitlines()) if hit), None)


def commit_skips_hooks(words: list[str]) -> bool:
    """`git commit -n`처럼 짧은 옵션 묶음 안의 -n(검사 건너뛰기)을 찾는다."""
    index = 0
    while index < len(words) and words[index].startswith("-"):
        index += 2 if words[index] in GIT_VALUE_GLOBAL else 1
    if index >= len(words) or words[index] != "commit":
        return False
    index += 1
    while index < len(words):
        word = words[index]
        if word in {"--", ";", "&&", "||", "|", "&"}:
            return False
        if word.startswith("--"):
            if word.split("=", 1)[0] == "--no-verify":
                return True
            index += 2 if word in COMMIT_VALUE_LONG else 1
            continue
        if word.startswith("-") and len(word) > 1:
            for position, letter in enumerate(word[1:], start=1):
                if letter == "n":
                    return True
                if letter in COMMIT_VALUE_SHORT:
                    index += 1 if position == len(word) - 1 else 0  # 값이 다음 낱말에 있으면 건너뛴다
                    break
        index += 1
    return False


def skips_commit_hooks(command: str) -> bool:
    for line in command.splitlines():
        try:
            words = shlex.split(line)
        except ValueError:
            words = line.split()
        for index, word in enumerate(words):
            if re.fullmatch(r"(?i)(?:.*[/\\])?git(?:\.exe)?", word) and commit_skips_hooks(words[index + 1:]):
                return True
    return False


def changes_hooks_path(command: str) -> bool:
    """`core.hooksPath`를 저장소의 `.githooks`가 아닌 곳으로 바꾸거나 지우는지 본다."""
    for call in GIT_CALL_RE.finditer(command):
        args = call.group("args")
        if not re.search(r"(?i)core\.hookspath", args):
            continue
        if re.search(r"(?i)-c\s*['\"]?core\.hookspath|--config-env", args):
            return True
        if not re.search(r"(?i)\bconfig\b", args):
            continue
        if re.search(r"(?i)--(?:unset|unset-all|remove-section|rename-section|global|system|file|edit|replace-all)\b"
                     r"|\bconfig\s+(?:unset|edit)\b", args):
            return True
        value = re.search(r"(?i)core\.hookspath['\"]?(?:\s*=\s*|\s+)(['\"]?)([^\s'\"]+)\1", args)
        if value and normalized(value.group(2)).rstrip("/") not in {".githooks", "./.githooks"}:
            return True
    return bool(re.search(r"GIT_CONFIG_\w+", command) and re.search(r"(?i)hookspath", command))


def hooks_bypass(command: str) -> str | None:
    if NO_VERIFY_RE.search(command):
        return "--no-verify"
    if skips_commit_hooks(command):
        return "git commit -n"
    if changes_hooks_path(command):
        return "core.hooksPath 변경"
    if re.search(r"(?i)disableAllHooks", command):
        return "disableAllHooks"
    return None


def secret_variable(command: str) -> str | None:
    for match in ENV_REFERENCE_RE.finditer(command):
        if secret_name(match.group("name")):
            return match.group("name")
    return next((match.group(0) for match in UPPER_NAME_RE.finditer(command) if secret_name(match.group(0))), None)


def secret_word(command: str) -> str | None:
    for word in WORD_SPLIT_RE.split(command):
        path = normalized(word)
        if not path or (path.lower().endswith(".key") and "/" not in path):
            continue  # 코드의 `obj.key` 같은 속성 접근은 파일로 보지 않는다
        if sensitive_path(path):
            return word
    return None


def modifies_protected(command: str) -> bool:
    if not any(protected_path(word) for word in WORD_SPLIT_RE.split(command) if word):
        return False
    if any(protected_path(match.group(1).strip("'\"")) for match in REDIRECT_RE.finditer(command)):
        return True
    return bool(WRITE_COMMAND_RE.search(command))


def check_command(command: str) -> tuple[str, str] | None:
    hit = first_secret(command)
    if hit:
        return DENY, (f"명령에 {hit[0]}로 보이는 값({hit[1]})이 들어 있어 실행하지 않습니다. 비밀값은 명령·파일·채팅에 "
                      "쓰지 말고 사용자가 자기 터미널에서 직접 입력하게 하십시오.")
    bypass = hooks_bypass(command)
    if bypass:
        return DENY, (f"커밋·푸시 검사나 보안 훅을 건너뛰거나 끄는 명령({bypass})은 실행하지 않습니다. "
                      "검사가 막은 항목은 빼고, 오탐이면 사용자에게 알리십시오.")
    if any(pattern.search(command) for pattern in ENV_DUMP_RES):
        return DENY, "환경 변수 전체나 명령 기록을 출력하는 명령은 실행하지 않습니다. 필요한 변수 하나만 이름으로 확인하십시오."
    name = secret_variable(command)
    if name:
        return DENY, (f"비밀값 변수 `{name}`를 읽거나 바꾸는 명령은 실행하지 않습니다. 값이 필요하면 사용자가 직접 확인하게 "
                      "하십시오. 커밋 메시지에 이 이름이 들어가야 하면 메시지를 파일로 만들어 `git commit -F <파일>`을 쓰십시오.")
    if CREDENTIAL_COMMAND_RE.search(command):
        return DENY, "OS 자격 증명 보관소나 로그인 토큰에 접근하는 명령은 실행하지 않습니다. 로그인·토큰 입력은 사용자가 직접 하게 하십시오."
    word = secret_word(command)
    if word:
        return DENY, (f"비밀값·인증 파일(`{word}`)에 접근하는 명령은 실행하지 않습니다. 커밋 메시지에 이 이름이 들어가야 하면 "
                      "`git commit -F <파일>`을 쓰십시오.")
    if modifies_protected(command):
        return ASK, "보호 장치 파일(커밋 훅·비밀값 검사기·Claude 설정·Git 설정)을 바꾸는 명령이라 사용자 확인이 필요합니다."
    return None


def check_file_tool(tool: str, data: dict) -> tuple[str, str] | None:
    paths = [value for key in ("file_path", "notebook_path", "path") if isinstance(value := data.get(key), str)]
    for path in paths:
        if sensitive_path(path):
            return DENY, f"비밀값·인증 파일(`{path}`)은 읽거나 고치지 않습니다."
    if tool == "Grep" and isinstance(data.get("glob"), str) and SECRET_GLOB_RE.search(data["glob"]):
        return DENY, f"비밀값·인증 파일을 고르는 검색(`{data['glob']}`)은 하지 않습니다."
    if tool not in EDIT_TOOLS:
        return None
    texts = [data.get("content"), data.get("new_string"), data.get("new_source")]
    texts += [edit.get("new_string") for edit in data.get("edits") or [] if isinstance(edit, dict)]
    for text in texts:
        hit = first_secret(text) if isinstance(text, str) else None
        if hit:
            return DENY, (f"쓰려는 내용에 {hit[0]}로 보이는 값({hit[1]})이 있습니다. 실제 비밀값은 파일에 넣지 말고, 예시는 "
                          "`<토큰>` 같은 자리표시자로, 테스트용 가짜 값은 실행 중에 조립하십시오.")
    if any(protected_path(path) for path in paths):
        return ASK, "보호 장치 파일(커밋 훅·비밀값 검사기·Claude 설정·Git 설정)을 고치려면 사용자 확인이 필요합니다."
    return None


def decide(tool: str, data: dict) -> tuple[str, str] | None:
    if tool in {"Bash", "PowerShell"}:
        return check_command(str(data.get("command", "")))
    return check_file_tool(tool, data)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        decision = decide(str(event.get("tool_name", "")), event.get("tool_input") or {})
    except Exception as exc:  # 검사기가 고장 나면 통과시키지 않는다.
        print(f"[gongbu-haja 보안 훅] 검사 중 오류가 나서 이 도구 호출을 막았습니다: {exc}", file=sys.stderr)
        return 2
    if decision is None:
        return 0
    action, reason = decision
    if action == ASK:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": reason}}))
        return 0
    print(f"[gongbu-haja 보안 훅] {reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
