#!/usr/bin/env python3
"""커밋·푸시에 비밀값(API 키·토큰·인증 파일)과 개인 강의 자료가 들어가지 않게 막는다.

    python scripts/check_secrets.py              # 스테이징한 파일 검사(pre-commit 훅)
    python scripts/check_secrets.py --pre-push   # 올리려는 모든 커밋 검사(pre-push 훅, 표준 입력 사용)
    python scripts/check_secrets.py --all        # 추적 중인 모든 파일 검사(CI)

찾으면 위치와 종류만 알리고 값은 가린 채 종료 코드 1을 돌려준다. 표준 라이브러리만 쓰며,
Claude Code 실행 전 검사(guard_secrets.py)도 같은 판정 함수를 쓴다.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:
    from .project_types import AUDIO_SUFFIXES
except ImportError:  # `python scripts/check_secrets.py`로 직접 실행할 때
    from project_types import AUDIO_SUFFIXES

# 값의 생김새만으로 알아보는 비밀값. 한 줄에서는 처음 맞은 종류 하나만 보고한다.
TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("노션 연동 토큰", re.compile(r"\b(?:ntn|secret)_[A-Za-z0-9]{40,}")),
    ("Anthropic API 키", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI API 키", re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}")),
    ("GitHub 토큰", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("GitLab 토큰", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("Google API 키", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("Google OAuth 비밀", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}")),
    ("AWS 액세스 키", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Hugging Face 토큰", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("Slack 토큰", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Stripe 키", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("npm·PyPI 토큰", re.compile(r"\b(?:npm_[A-Za-z0-9]{36}|pypi-[A-Za-z0-9_-]{50,})")),
    ("JWT·세션 토큰", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("비공개 키", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
)

# 이름이 비밀값인 변수·키에 실제 값처럼 보이는 문자열을 넣는 줄
QUOTED_ASSIGNMENT_RE = re.compile(
    r"""(?P<name>[A-Za-z_][\w.-]*)["']?\s*(?::=|=|:)\s*(?P<quote>["'])(?P<value>[^"'\s]{16,})(?P=quote)"""
)
BARE_ASSIGNMENT_RE = re.compile(
    r"""^\s*(?:export\s+)?(?P<name>[A-Za-z_][\w.-]*)\s*(?:=|:)\s*(?P<value>[^\s"'#]{16,})\s*(?:\#.*)?$"""
)
SECRET_NAME_WORDS = {"TOKEN", "SECRET", "PASSWORD", "PASSWD", "APIKEY", "CREDENTIAL", "CREDENTIALS", "PAT"}
SECRET_NAME_PAIRS = {("API", "KEY"), ("ACCESS", "KEY"), ("PRIVATE", "KEY"), ("SECRET", "KEY"), ("CLIENT", "SECRET")}
PLACEHOLDER_RE = re.compile(r"(?i)example|sample|dummy|placeholder|changeme|your|test|fake|xxxx|\.\.\.|…|[<>{}$%]")

# 파일 자체가 비밀값인 경로. .gitignore를 `git add -f`로 우회한 경우까지 막는다.
SECRET_FILE_RE = re.compile(
    r"(?i)(?:^|/)(?:\.env(?:\.(?!example$)[^/]+)?|[^/]+\.env|\.envrc"
    r"|[^/]+\.(?:pem|key|p12|pfx|jks|keystore)|id_(?:rsa|dsa|ecdsa|ed25519)"
    r"|cookies[^/]*\.json|storage-state[^/]*\.json|credentials\.json"
    r"|\.netrc|\.git-credentials|\.pypirc|\.npmrc)$"
)
PRIVATE_DIRS = ("browser-profile", ".auth", ".gongbu", "input", "output")
ALLOWED_PATHS = {"workspace/README.md"}
MAX_SCAN_BYTES = 2 * 1024 * 1024
ZERO_SHA_RE = re.compile(r"^0+$")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    preview: str


def secret_name(name: str) -> bool:
    """`NOTION_TOKEN`, `apiKey`처럼 이름의 마지막 낱말이 비밀값을 뜻하는지 본다."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    words = [word for word in re.split(r"[_.\-]+", spaced.upper()) if word]
    if not words:
        return False
    return words[-1] in SECRET_NAME_WORDS or (len(words) > 1 and (words[-2], words[-1]) in SECRET_NAME_PAIRS)


def secret_value(value: str) -> bool:
    """자리표시자·경로·주소가 아니라 실제 비밀값처럼 보이는 값인지 본다."""
    if PLACEHOLDER_RE.search(value) or ("://" in value and "@" not in value):
        return False
    return any(ch.isdigit() for ch in value) and any(ch.isalpha() for ch in value)


def scan_line(line: str) -> tuple[str, str] | None:
    """한 줄에서 비밀값을 찾으면 (종류, 가린 미리보기)를 돌려준다. 값 자체는 돌려주지 않는다."""
    for kind, pattern in TOKEN_PATTERNS:
        match = pattern.search(line)
        if match:
            return kind, f"{match.group(0)[:4]}…"
    for regex in (QUOTED_ASSIGNMENT_RE, BARE_ASSIGNMENT_RE):
        for match in regex.finditer(line):
            if secret_name(match.group("name")) and secret_value(match.group("value")):
                return "비밀값 대입", f"{match.group('name')}=…"
    return None


def scan_text(path: str, text: str) -> list[Finding]:
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        hit = scan_line(line)
        if hit:
            findings.append(Finding(path, number, *hit))
    return findings


def secret_file(path: str) -> bool:
    return bool(SECRET_FILE_RE.search(path.replace("\\", "/")))


def blocked_path(path: str) -> str | None:
    """커밋하면 안 되는 경로면 그 이유를 돌려준다."""
    normalized = path.replace("\\", "/")
    if normalized in ALLOWED_PATHS:
        return None
    if secret_file(normalized):
        return "비밀값·인증 파일"
    parts = PurePosixPath(normalized).parts
    if any(part in PRIVATE_DIRS for part in parts[:-1]):
        return "강의 자료·실행 상태·브라우저 인증 폴더"
    if normalized.startswith("workspace/"):
        return "강의별 작업 산출물"
    if PurePosixPath(normalized).suffix.lower() in AUDIO_SUFFIXES:
        return "녹음·녹화 파일"
    return None


def git(args: list[str], root: Path, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=root, input=data, capture_output=True, check=True).stdout


def git_paths(output: bytes) -> list[str]:
    paths = output.decode("utf-8", "surrogateescape").split("\0")
    return list(dict.fromkeys(path for path in paths if path and "\n" not in path))


def read_blobs(root: Path, specs: list[str]) -> dict[str, bytes]:
    """`<리비전>:<경로>` 목록을 git 호출 두 번으로 읽는다. 없거나 너무 큰 객체는 건너뛴다."""
    if not specs:
        return {}
    request = "".join(f"{spec}\n" for spec in specs).encode("utf-8", "surrogateescape")
    infos = git(["cat-file", "--batch-check=%(objecttype) %(objectsize)"], root, request).decode().splitlines()
    wanted = [
        spec for spec, info in zip(specs, infos)
        if info.startswith("blob ") and int(info.split()[1]) <= MAX_SCAN_BYTES
    ]
    if not wanted:
        return {}
    request = "".join(f"{spec}\n" for spec in wanted).encode("utf-8", "surrogateescape")
    output = git(["cat-file", "--batch=%(objectsize)"], root, request)
    blobs, position = {}, 0
    for spec in wanted:
        header_end = output.index(b"\n", position)
        size = int(output[position:header_end])
        blobs[spec] = output[header_end + 1:header_end + 1 + size]
        position = header_end + 1 + size + 1
    return blobs


def scan_specs(root: Path, specs: dict[str, str]) -> list[Finding]:
    """{표시 이름: `<리비전>:<경로>`}를 경로 규칙과 내용으로 검사한다."""
    findings: list[Finding] = []
    readable = {}
    for label, spec in specs.items():
        reason = blocked_path(spec.split(":", 1)[1])
        if reason:
            findings.append(Finding(label, 0, reason, ""))
        else:
            readable[label] = spec
    blobs = read_blobs(root, list(readable.values()))
    for label, spec in readable.items():
        data = blobs.get(spec)
        if data is None or b"\0" in data[:8192]:
            continue
        findings.extend(scan_text(label, data.decode("utf-8", "replace")))
    return findings


def pushed_commits(root: Path, updates: str) -> list[str]:
    """pre-push 표준 입력(로컬 ref, 로컬 SHA, 원격 ref, 원격 SHA)에서 새로 올라갈 커밋을 고른다."""
    commits: list[str] = []
    for line in updates.splitlines():
        fields = line.split()
        if len(fields) != 4 or ZERO_SHA_RE.match(fields[1]):
            continue  # 형식이 다르거나 원격 브랜치 삭제
        local_sha, remote_sha = fields[1], fields[3]
        new_only = [local_sha, "--not", "--remotes"]
        try:
            listing = git(["rev-list", *(new_only if ZERO_SHA_RE.match(remote_sha) else [f"{remote_sha}..{local_sha}"])], root)
        except subprocess.CalledProcessError:  # 원격 커밋을 아직 받지 않은 경우
            listing = git(["rev-list", *new_only], root)
        commits.extend(sha for sha in listing.decode().split() if sha not in commits)
    return commits


def check(root: Path, mode: str, updates: str = "") -> list[Finding]:
    changed = ["--name-only", "--diff-filter=ACMRT", "-z"]
    if mode == "all":
        return scan_specs(root, {path: f":{path}" for path in git_paths(git(["ls-files", "-z"], root))})
    if mode == "staged":
        return scan_specs(root, {path: f":{path}" for path in git_paths(git(["diff", "--cached", *changed], root))})
    specs = {}
    for sha in pushed_commits(root, updates):
        listing = git(["diff-tree", "-r", "-m", "--root", "--no-commit-id", *changed, sha], root)
        specs.update({f"{sha[:8]} {path}": f"{sha}:{path}" for path in git_paths(listing)})
    return scan_specs(root, specs)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="비밀값과 개인 강의 자료가 커밋·푸시되지 않게 검사합니다.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="추적 중인 모든 파일을 검사합니다(CI용).")
    group.add_argument("--pre-push", action="store_true", help="표준 입력으로 받은 푸시 대상 커밋을 모두 검사합니다.")
    parser.add_argument("--root", type=Path, default=None, help="검사할 Git 저장소(기본: 현재 저장소)")
    args = parser.parse_args(argv)
    mode = "all" if args.all else "push" if args.pre_push else "staged"
    try:
        root = args.root or Path(git(["rev-parse", "--show-toplevel"], Path.cwd()).decode("utf-8").strip())
        findings = check(root.resolve(), mode, sys.stdin.read() if mode == "push" else "")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"[gongbu-haja] 비밀값 검사를 실행하지 못해 멈춥니다: {exc}", file=sys.stderr)
        return 1
    if not findings:
        return 0
    for item in findings:
        where = f"{item.path}:{item.line}" if item.line else item.path
        value = f" ({item.preview})" if item.preview else ""
        print(f"[막음] {where} — {item.kind}{value}", file=sys.stderr)
    action = {"staged": "커밋을", "push": "푸시를", "all": "검사를"}[mode]
    print(
        f"[gongbu-haja] 비밀값이나 개인 자료로 보이는 항목 {len(findings)}개 때문에 {action} 멈췄습니다. "
        "해당 값·파일을 빼고 다시 시도하십시오. 실제 키가 공개된 적이 있다면 그 서비스에서 바로 재발급하십시오.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
