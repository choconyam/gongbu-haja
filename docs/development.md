# 개발과 검증

[← README](../README.md) · [설치](install.md) · [전사·녹음](transcription.md) · [관리형 실행](managed-run.md) · [구조](architecture.md) · [개발·검증](development.md)

## 업데이트 개발 시 검증

로컬에서는 [유지보수 지침](../rules/repository-maintenance.md)의 변경 범위별 검사만 수행한다. 모델 설정만 바꿨다면 모델 배정과 상태 검사를 하고, PDF 빌드·패키지 설치 검사를 매번 반복하지 않는다. 실제 강의의 비교용 집필이나 추가 의미 검수는 업데이트 검증에 포함하지 않는다.

전체 단위 테스트와 Windows·Linux/Python 버전별 설치 호환성 검사는 기존 [GitHub Actions](../.github/workflows/validate.yml)가 push와 pull request마다 실행한다. 테스트는 Python 작업이며 AI 모델을 호출하지 않는다. 로컬 검증과 CI 결과는 구분해서 보고하고, CI 실패는 해당 실패 범위부터 확인한다.

## 비밀값 보호

저장소를 처음 받았다면 커밋 훅을 한 번 켠다.

```bash
git config core.hooksPath .githooks
```

- **커밋·푸시 검사:** `pre-commit`은 커밋하려는 파일을, `pre-push`는 올리려는 모든 커밋을 [`check_secrets.py`](../scripts/check_secrets.py)로 검사한다. API 키·토큰 모양의 값, 비밀값 이름의 변수에 실제 값처럼 보이는 문자열을 넣은 줄, 환경 파일·인증서·쿠키 같은 인증 파일, 강의 자료·녹음·실행 상태 폴더가 있으면 멈추고, 위치와 종류만 값을 가린 채 알린다. 커밋 훅을 건너뛴 커밋도 푸시 단계에서 다시 걸러지고, CI가 `--all`로 저장소 전체를 한 번 더 검사한다. GitHub의 비밀값 푸시 보호도 켜져 있다.
- **Claude Code 실행 전 검사:** [`.claude/settings.json`](../.claude/settings.json)이 셸·파일 도구를 쓰기 직전마다 [`guard_secrets.py`](../scripts/guard_secrets.py)를 실행한다. 인증 파일 읽기, 환경 변수 전체 출력, 비밀값 변수·OS 자격 증명 보관소 접근, 명령이나 파일 내용에 든 토큰 모양의 값, `--no-verify`·`core.hooksPath` 변경 같은 검사 우회를 막는다. 커밋 훅·검사기·Claude 설정을 고치려 하면 사용자 확인을 받는다. 커밋 메시지에 비밀값 변수 이름이 들어가야 하면 메시지를 파일로 만들어 `git commit -F <파일>`로 넘긴다.
- 흔한 실수와 우회를 막는 장치이지 완전한 격리는 아니다. 키가 한 번이라도 공개 저장소·채팅·로그에 나갔다면 그 서비스에서 바로 재발급한다.
- 오탐이면 검사를 끄지 말고 값을 `<토큰>` 같은 자리표시자로 바꾼다. 테스트용 가짜 토큰은 문자열을 실행 중에 조립한다.

## 학습노트 산출물 검증

전사 패키지:

```powershell
python scripts/validate_transcript_package.py <전사본> `
  --audio <녹음> `
  --manifest <메타데이터_JSON>
```

간결형 Markdown은 시간표시가 없는 것이 정상이다. 타임스탬프 자체를 검사할 때는 `<전사본>`에 원시 SRT를 넣거나 명시적으로 `--require-timestamps`를 사용한다.

최종 노트:

```powershell
python scripts/validate_note_output.py <학습노트_파일>
```

Python 검증은 구조와 추적 가능성까지만 본다. 음성이 제대로 받아 적혔는지, 교수 설명이 중요한지, 내용이 맞는지는 역할별 에이전트 검수가 판단한다.

교안이나 코드의 `TODO`는 자동으로 최종 노트에 복사하지 않는다. 학습·과제 목표이면 의도적인 실습 과제로 정리하고, 관련 없는 템플릿 표지는 제외한다. 코드 블록 안의 `TODO`는 검증기가 오류로 보지 않으며, 일반 본문에 남은 `TODO`는 최종 검수자가 의미를 판단하도록 경고한다. `FIXME`, `TBD`, `<placeholder>`처럼 명백한 미완성 표지는 계속 오류다.
