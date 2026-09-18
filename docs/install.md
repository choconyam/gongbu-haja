# 설치와 첫 사용

[← README](../README.md) · [설치](install.md) · [전사·녹음](transcription.md) · [관리형 실행](managed-run.md) · [구조](architecture.md) · [개발·검증](development.md)

README의 3단계로 부족할 때 보는 문서다. 처음 쓰는 사람을 위한 단계별 안내, 업데이트, 전역 CLI·플러그인·스킬 같은 선택 설치를 다룬다.

## 처음이라면 — 하나씩 따라 하기

AI 에이전트를 써 본 적이 없어도 된다. 아래 순서대로 하면 된다.

### 0. 필요한 것

| 준비물 | 설명 |
|---|---|
| AI 코딩 도구 하나 | **Codex**, **Claude Code** 또는 **Cursor**. 이 프로젝트에 일을 시키는 창구다. 사용하는 도구에 따라 구독료나 모델 사용료가 들 수 있다. |
| Python 3.10 이상 | 입력 해시·실행 상태·산출물 검증에 필요. 녹음 전사를 사용할 때는 전사 패키지도 추가로 설치한다. [python.org](https://www.python.org/downloads/)에서 설치 |
| Windows 시스템 오디오 녹음(선택) | 온라인 강의를 이 PC에서 직접 녹음할 때만 `requirements-recording.txt`를 설치한다. 대면 수업·마이크 녹음 용도가 아니다. |
| deep PDF 출력(선택) | XeLaTeX·한글 글꼴과 TeX 패키지가 필요하다. [DEEP PDF 출력](architecture.md#deep-pdf-출력) 참고. Markdown만 만들 때는 필요 없다. |
| GPU | 없어도 된다. 전사가 느려질 뿐이다(1시간 강의 ≈ 20~40분) |

### 1. 프로젝트 받기 (기본 설치)

git을 쓸 줄 알면:

```bash
git clone https://github.com/choconyam/gongbu-haja
cd gongbu-haja
```

git을 모르면: 이 페이지 위쪽의 초록색 **Code** 버튼 → **Download ZIP** → 압축을 풀면 된다.

저장소 폴더를 직접 열어 쓰면 `gongbu` CLI 패키지를 설치할 필요가 없다. Python과 녹음·전사·PDF용 의존성은 사용하는 기능에 따라 준비한다.

### 2. 강의 자료 넣기

받은 폴더 안의 `input/`에 강의별 폴더를 만들고, 교안 PDF와 녹음 파일을 복사해 넣는다.

```text
gongbu-haja/
└─ input/
   └─ 2026-03-10_과목A/        ← 새로 만든 폴더
      ├─ 3주차_교안.pdf
      └─ 수업녹음.m4a
```

폴더 이름은 아무렇게나 지어도 되지만, 날짜와 과목이 들어가면 나중에 찾기 편하다.

### 3. AI 도구로 이 폴더 열기

- **Windows CLI**: 탐색기에서 `gongbu-haja` 폴더를 연 상태로 주소창에 `cmd`를 입력해 터미널을 띄우고, `claude`(Claude Code) 또는 `codex`(Codex)를 입력
- **macOS·Linux CLI**: 터미널에서 `cd`로 폴더에 들어간 뒤 `claude` 또는 `codex`를 입력
- **데스크톱 앱**: Codex, Claude Code 또는 Cursor에서 프로젝트 열기 기능으로 `gongbu-haja` 폴더를 선택

### 4. 한 문장 말하기

```text
input/2026-03-10_과목A 자료로 자료 충실형 학습노트 만들어줘
```

이게 전부다. 에이전트가 필요한 전사·자료 대응·작성·자체 점검·출력을 이어서 진행하고, 스스로 확정할 수 없는 것(강의 이름이 애매하다든지)만 물어본다.

### 5. 결과 받기

끝나면 완성된 노트 파일(PDF, Word 또는 Markdown)의 위치를 알려준다. 중간 작업물은 `workspace/` 폴더에 남는다.

### 자주 묻는 것

- **돈이 드나?** 이 프로젝트 자체는 무료(MIT)다. 다만 AI 도구의 구독료·사용량은 본인 계정에서 나간다.
- **녹음이 인터넷에 올라가나?** 기본 전사는 로컬 Whisper로 실행되므로 원본 녹음을 외부 전사 서비스에 자동 업로드하지 않는다. 다만 학습노트 생성 중 AI 도구가 읽은 텍스트·이미지 등의 처리 방식은 Codex, Claude Code 또는 Cursor의 계정 설정과 서비스 정책을 따른다.
- **온라인 강의도 직접 녹음할 수 있나?** Windows에서는 가능하다. 사용자가 녹음을 명시적으로 요청하고 수강·녹음 권한을 확인한 온라인 강의에 한해, 기본 출력 장치의 재생음을 로컬 WAV로 저장한다. 대면 수업이나 주변 마이크는 녹음하지 않는다.
- **전사만 따로 쓸 수 있나?** 된다. 녹음 파일을 `강의전사.bat`(여러 개면 `배치전사.bat`)에 끌어다 놓으면 전사본만 만들어 준다.

## 업데이트

Git으로 받은 저장소 폴더에서 실행한다.

```bash
git status --short
git pull --ff-only
```

진행 중인 노트 작업이 끝난 뒤 업데이트한다. 직접 수정한 코드·규칙이 있거나 업데이트가 충돌하면 변경을 보존하고 원인을 확인한다. 강제 초기화로 해결하지 않는다. ZIP으로 받았다면 새 버전을 별도 폴더에 풀고 기존 자료·산출물을 보존한다.

코드·규칙 업데이트마다 패키지를 다시 만들거나 전체 강의를 재검수할 필요는 없다. 의존성이나 사용자 범위 에이전트 선언을 바꾼 업데이트만 해당 설치·동기화를 추가로 수행한다. 업데이트 후에는 새 에이전트 작업을 열어 변경된 지침을 읽게 한다. 기존 실행 상태는 저장된 모델표를 유지한다.

## 선택 설치 — 사용하는 도구에 맞게 선택

기본은 위의 Git 설치다. 같은 엔진을 Codex, Claude Code, Cursor에서 사용할 수 있으며, 전역 명령이나 스킬·플러그인이 필요할 때만 아래 방식을 선택한다. 어느 입구를 선택하든 규칙·역할·스크립트는 이 저장소 하나가 기준이다.

### 전역 CLI로 설치해 과목 폴더에서 쓰기 (선택)

저장소를 clone하지 않아도 된다. Python 패키지 하나를 전역에 깔면 `gongbu` 명령이 생기고, 규칙·역할 프롬프트·스크립트가 패키지 안에 함께 들어간다.

```bash
python -m pip install --user pipx && python -m pipx ensurepath
pipx install "gongbu-haja[recording,transcription,pdf] @ git+https://github.com/choconyam/gongbu-haja"
gongbu setup-agents
```

- `[recording]`은 Windows 온라인 강의 녹음, `[transcription]`은 로컬 Whisper 전사, `[pdf]`는 Python PDF 도구용 선택 의존성이다. deep의 XeLaTeX 환경은 별도로 준비한다. 선택 기능이 모두 필요 없으면 `pipx install "gongbu-haja @ git+https://github.com/choconyam/gongbu-haja"`.
- `gongbu setup-agents`는 `~/.claude/agents/`와 `~/.codex/agents/`에 서브 에이전트 선언 4개를 설치한다. `~/.codex/config.toml`은 `[agents]` 절이 없을 때만 끝에 덧붙이고, 이미 있으면 손대지 않고 맞출 값만 알려준다.

그 다음은 과목 폴더에서 한다. 과목 폴더 하나가 자기 자료·녹음·상태·노트를 전부 갖고, 다른 과목과 섞이지 않는다.

```text
C:\강의\과목A\                              ← 과목 폴더 (여기서 gongbu 실행, AI 도구도 여기서 열기)
├─ 2026-03-10_1주차\                         ← 강의별 하위폴더 = 입력
│  ├─ 1주차_교안.pdf
│  └─ 2026-03-10_1주차_20260310_090000.wav    ← gongbu record 결과
├─ output\                                   ← 최종 학습노트
└─ .gongbu\2026-03-10_1주차\                 ← 실행 상태·전사·중간 산출물 (숨김 폴더)
```

```bash
cd C:\강의\과목A
gongbu setup                                        # .gongbu/, output/, .gitignore(녹음·상태 제외) 준비
gongbu record --lecture-id 2026-03-10_1주차          # 온라인 강의 녹음 (Windows) → 2026-03-10_1주차\ 아래 WAV
gongbu transcribe 2026-03-10_1주차\녹음.wav          # 로컬 전사 → .gongbu\2026-03-10_1주차\transcript\
```

이후 AI 코딩 도구(Codex, Claude Code, Cursor)에서 그 과목 폴더를 열고 "2026-03-10_1주차 자료로 자료 충실형 학습노트 만들어줘"라고 요청하면 된다. 스킬은 `gongbu paths`로 엔진 위치를 찾는다. 직접 제작은 별도 실행 상태가 필요 없고, 기존 상태를 이어가거나 관리형 실행을 요청했을 때만 `.gongbu/`의 상태와 `gongbu run ...`을 사용한다. 전체 명령은 `gongbu --help`에서 볼 수 있다.

CLI 설치본은 Git 작업 폴더와 별개다. CLI 설치를 갱신할 때는 `pipx upgrade gongbu-haja`를 사용하고, 사용자 범위 에이전트 선언이 바뀌었다면 `gongbu setup-agents`로 동기화한다. 저장소의 `git pull`만으로 설치본까지 바뀌지는 않는다.

| 방식 | 대상 | 설치·실행 |
|---|---|---|
| **프로젝트로 직접 열기 (기본)** | Codex, Claude Code, Cursor | `git clone` 후 저장소 폴더를 열고 요청. 업데이트는 `git pull --ff-only` |
| **전역 CLI (선택)** | Codex, Claude Code, Cursor | 위 절. `pipx install` 뒤 과목 폴더에서 `gongbu` |
| **Codex 스킬 설치** | Codex CLI·데스크톱 앱 | Codex에 “`$skill-installer`로 [`skills/gongbu-haja/`](https://github.com/choconyam/gongbu-haja/tree/main/skills/gongbu-haja)를 설치해줘”라고 요청 |
| **Claude Code 플러그인** | Claude Code | `/plugin marketplace add choconyam/gongbu-haja` → `/plugin install gongbu-haja@gongbu-haja` |
| **Claude Code 스킬 수동 설치** | Claude Code | 저장소의 `skills/gongbu-haja/` 폴더를 `~/.claude/skills/`에 복사 |

Codex CLI 자체가 아직 없다면 운영체제에 맞는 방법 하나로 먼저 설치한다([Codex CLI 공식 설치 안내](https://learn.chatgpt.com/docs/codex/cli)).

```powershell
# Windows
powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"

# Node.js가 설치되어 있다면 Windows·macOS·Linux 공통
npm install -g @openai/codex
```

macOS·Linux에서는 독립 설치 스크립트도 사용할 수 있다.

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Claude Code CLI 자체가 아직 없다면 운영체제에 맞는 방법 하나로 설치한다([Claude Code 공식 설치 안내](https://code.claude.com/docs/en/installation)).

```powershell
# Windows: WinGet
winget install Anthropic.ClaudeCode

# Node.js 22 이상이 설치되어 있다면 Windows·macOS·Linux 공통
npm install -g @anthropic-ai/claude-code
```

macOS·Linux에서는 독립 설치 스크립트도 사용할 수 있다.

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

설치 후 프로젝트나 강의 자료 폴더에서 Codex는 `codex`, Claude Code는 `claude`를 실행하고 각 서비스 계정으로 로그인하면 된다.

플러그인·스킬로 설치하면 **아무 폴더에서나** 사용할 수 있다. 과목 폴더(교안·녹음을 모아둔 폴더)에서 "이 자료로 학습노트 만들어줘"라고 하면, 스킬이 엔진 저장소를 찾아 연결하고 현재 폴더를 입력 자료로 사용한다. 엔진이 없으면 승인을 받아 `~/gongbu-haja`에 받아온다.

사용자가 Python 명령을 하나하나 칠 필요는 없다. 현재 세션의 에이전트가 필요한 로컬 도구를 실행한다. 이 문서의 명령 예시는 개발하거나 문제를 파헤칠 때 쓰는 참고용이다.
