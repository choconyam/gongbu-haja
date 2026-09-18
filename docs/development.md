# 개발과 검증

[← README](../README.md) · [설치](install.md) · [전사·녹음](transcription.md) · [관리형 실행](managed-run.md) · [구조](architecture.md) · [개발·검증](development.md)

## 업데이트 개발 시 검증

로컬에서는 [유지보수 지침](../rules/repository-maintenance.md)의 변경 범위별 검사만 수행한다. 모델 설정만 바꿨다면 모델 배정과 상태 검사를 하고, PDF 빌드·패키지 설치 검사를 매번 반복하지 않는다. 실제 강의의 비교용 집필이나 추가 의미 검수는 업데이트 검증에 포함하지 않는다.

전체 단위 테스트와 Windows·Linux/Python 버전별 설치 호환성 검사는 기존 [GitHub Actions](../.github/workflows/validate.yml)가 push와 pull request마다 실행한다. 테스트는 Python 작업이며 AI 모델을 호출하지 않는다. 로컬 검증과 CI 결과는 구분해서 보고하고, CI 실패는 해당 실패 범위부터 확인한다.

## 학습노트 산출물 검증

전사 패키지:

```powershell
python scripts/validate_transcript_package.py <전사본> `
  --audio <녹음> `
  --manifest <메타데이터_JSON> `
  --require-timestamps
```

최종 노트:

```powershell
python scripts/validate_note_output.py <학습노트_파일>
```

Python 검증은 구조와 추적 가능성까지만 본다. 음성이 제대로 받아 적혔는지, 교수 설명이 중요한지, 내용이 맞는지는 역할별 에이전트 검수가 판단한다.

교안이나 코드의 `TODO`는 자동으로 최종 노트에 복사하지 않는다. 학습·과제 목표이면 의도적인 실습 과제로 정리하고, 관련 없는 템플릿 표지는 제외한다. 코드 블록 안의 `TODO`는 검증기가 오류로 보지 않으며, 일반 본문에 남은 `TODO`는 최종 검수자가 의미를 판단하도록 경고한다. `FIXME`, `TBD`, `<placeholder>`처럼 명백한 미완성 표지는 계속 오류다.
