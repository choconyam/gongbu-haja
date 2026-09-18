# 녹음과 전사

[← README](../README.md) · [설치](install.md) · [전사·녹음](transcription.md) · [관리형 실행](managed-run.md) · [구조](architecture.md) · [개발·검증](development.md)

온라인 강의 녹음(Windows), 로컬 Whisper 전사, 모델 자동 선택, 전사 검수 도구와 산출물을 다룬다.

## 온라인 강의 녹음 실행(Windows만)

이 기능은 사용자가 녹음을 명시적으로 요청하고 수강 권한과 학교·교수자의 녹음 허용 범위를 확인한 **온라인 강의 재생음**에만 사용한다. 대면 수업이나 마이크 입력은 지원하지 않으며 로그인·2단계 인증·CAPTCHA와 강의 재생 시작은 필요할 때 사용자가 직접 처리한다. 접근 제어나 DRM은 우회하지 않는다.

강의 사이트 주소와 로그인 정보는 설정 파일이나 명령줄 인자로 받지 않는다. 사용자가 실행할 때 브라우저에서 직접 사이트를 열고 인증하며, 특정 학교명·사이트 URL·계정 식별자·비밀번호·쿠키·세션·브라우저 프로필은 프로젝트 파일이나 로그에 기록하지 않고 Git/GitHub에도 올리지 않는다.

처음 한 번 녹음 패키지를 설치한다.

```powershell
python -m pip install -r requirements-recording.txt
```

사용 가능한 Windows 출력 루프백 장치를 확인하고 30초 시험 녹음을 만든다.

```powershell
python scripts/record_lecture.py --list-devices
python scripts/record_lecture.py --lecture-id "2026-03-10_과목A_본강의" --duration 30
```

시험 파일을 재생해 음량을 확인한 뒤 본 녹음을 시작한다. `--duration`을 생략하면 `Ctrl+C`를 누를 때까지 녹음한다. 재생 배속은 기본 **1.75배**다(`--playback-rate`, 최대 2). 플레이어가 배속을 지원하면 1.75배로 틀어 녹음 시간을 줄이고, 사이트가 배속을 막으면 `--playback-rate 1`로 1배 재생한다. 배속은 녹음 옆 `.recording.json`과 전사 manifest에 남고, 전사 타임스탬프는 녹음 시간 기준이다.

```powershell
python scripts/record_lecture.py --lecture-id "2026-03-10_과목A_본강의"
```

터미널 명령 대신 탐색기에서 `강의녹음.bat`를 실행해 강의 식별자와 선택 항목을 입력해도 된다.

출력은 `input/<lecture_id>/` 아래의 충돌 없는 새 WAV 파일이다. 녹음 중에는 `.part.wav`로 쓰고 정상 종료나 `Ctrl+C` 후 완성 이름으로 바꾸므로 기존 녹음을 덮어쓰지 않는다. 이미 해당 강의의 실행 상태를 만든 뒤 녹음했다면 `manage_run.py refresh-inputs`로 새 입력을 반영한다.

## 전사 의존성

```powershell
python -m pip install -r requirements-transcription.txt
```

온라인 전사 서비스 업로드는 기본 경로가 아니다. 로컬 전사를 우선하며 외부 업로드가 필요하면 사용자 승인을 먼저 받는다.

## 전사 실행

실제 전사 전에 이름과 출력 위치만 확인:

```powershell
python scripts/transcribe_lecture.py "C:\자료\음성 260310_과목A.m4a" --dry-run
```

기본 전사:

```powershell
python scripts/transcribe_lecture.py "C:\자료\음성 260310_과목A.m4a"
```

검수해 확정한 전문용어 목록을 인식 힌트로 제공:

```powershell
python scripts/transcribe_lecture.py "C:\자료\음성 260310_과목A.m4a" `
  --glossary "C:\자료\전문용어.txt"
```

### 컴퓨터 사양은 신경 쓰지 않아도 된다

기본값 `--model auto`가 GPU 메모리를 감지해 그 사양에서 가장 정확한 모델을 고르고, 선택 근거와 예상 소요 시간을 출력한다.

```text
GPU 10GB 이상  → 그냥 실행하세요 (large-v3, 최고 정확도)
GPU 있음       → 그냥 실행하세요 (메모리에 맞는 모델 자동 선택)
GPU 없음       → 실행 가능하나 1시간 강의당 약 20~40분 소요 (small, CPU)
```

| 감지된 GPU 메모리 | 자동 선택 | 비고 |
|---|---|---|
| 10GB 이상 | `large-v3` float16 | 최고 정확도 |
| 6–10GB | `large-v3` int8 | 정확도 거의 동일, 8GB 카드(RTX 3060/4060 등) 대상 |
| 3–6GB | `medium` int8 | |
| 2–3GB | `small` int8 (GPU) | |
| GPU 없음·감지 실패 | `small` int8 (CPU) | 느리지만 동작 |

`--model large-v3`처럼 이름을 직접 지정하면 자동 선택을 건너뛴다. 언어 기본값은 한국어이며 GPU 추론 중 오류가 나면 CPU로 처음부터 다시 시도한다. 실제 사용한 모델과 선택 방식은 전사 manifest의 `model`, `model_selection`, `model_tier`에 기록되어 전사 검수 담당이 표본 범위를 정할 때 활용한다.

기존 동일 산출물은 자동으로 덮어쓰지 않는다. 의도적으로 교체하는 경우에만 `--force`를 사용한다.

전사가 끝난 뒤 Python으로 용어 후보와 검수할 구간만 추린다. 이 명령은 전사를 고치지 않으며, PDF에서 발견한 표현도 최종 전문용어로 확정하지 않는다.

```powershell
python scripts/prepare_transcript_review.py `
  --segments "workspace/<lecture_id>/transcript/<lecture_id>_segments.json" `
  --handout "C:\자료\교안.pdf" `
  --output-dir "workspace/<lecture_id>/transcript" `
  --prefix "<lecture_id>"
```

`*_term_candidates.json`은 전체 용어 후보 캐시이고 `*_review_packets.json`은 요약·경로만 담은 로컬 색인이다. manifest를 포함한 세 파일은 `model_input=false`라서 모델에 전달하지 않는다. 아래 selector가 고른 `*_packets/packet_NNNN.json`만 합계 16KiB 이하로 전사 검수 하위 에이전트에 전달한다. 각 개별 패킷은 `model_input=true`이며 관련 용어 후보를 최대 6개만 포함한다.

manifest도 모델이 읽지 않는다. 다음 로컬 selector가 실제 ASR 이상을 단순 숫자·평가조건 후보보다 먼저 고르고, 선택 결과 총합을 기본 16KiB 아래로 제한한다. 필요하면 `--reason`이나 `--segment-id`를 반복해 정확한 후보만 고른다. 출력 경로는 manifest 폴더 기준 상대 경로다.

```powershell
python scripts/select_review_packets.py `
  "workspace/<lecture_id>/transcript/<lecture_id>_review_packet_manifest.json" `
  --max-total-bytes 16384
```

검수 에이전트는 자동 치환을 직접 하지 않고 `source_segments_sha256`, `segment_id`, 정확한 `original`, `action`, `replacement`, `verification`, `rationale`를 담은 결정 JSON을 만든다. Python은 해시와 현재 원문이 모두 일치할 때만 파생 검수본에 적용한다.

```powershell
python scripts/apply_transcript_corrections.py `
  "workspace/<lecture_id>/transcript/<lecture_id>_segments.json" `
  "workspace/<lecture_id>/transcript/<lecture_id>_correction_decisions.json" `
  --output-dir "workspace/<lecture_id>/transcript" `
  --prefix "<lecture_id>"
```

## 강의 이름 처리

원본 녹음 파일은 이름을 바꾸거나 덮어쓰지 않는다. 파일명에서 날짜와 과목을 확정할 수 있으면 다음 형식의 `lecture_id`를 자동 생성한다.

```text
2026-03-10_과목A_본강의
```

자동 판정이 불가능하면 관리자가 사용자에게 확인한 뒤 명시적으로 전달한다.

```powershell
python scripts/transcribe_lecture.py "C:\자료\녹음001.m4a" `
  --lecture-id "2026-03-10_과목A_본강의"
```

드래그앤드롭 실행에서는 `강의전사.bat`가 자동 판정을 먼저 시도하고, 필요한 경우에만 강의 식별자를 질문한다.

## 전사 산출물

```text
workspace/<lecture_id>/transcript/
├─ <lecture_id>_transcript_raw.srt
├─ <lecture_id>_transcript_raw.txt
├─ <lecture_id>_transcript_draft.md
├─ <lecture_id>_segments.json
└─ <lecture_id>_transcript_manifest.json
```

SRT는 타임스탬프 기준 원시 전사, TXT는 검색용 원문, Markdown은 검수 작업본이다. `segments.json`에는 구간별 신뢰도 관련 값이 들어가고 manifest에는 원본 파일 해시, 모델, 장치, 언어, 강의 식별 정보가 기록된다.
