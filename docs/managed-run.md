# 관리형 에이전트 실행

[← README](../README.md) · [설치](install.md) · [전사·녹음](transcription.md) · [관리형 실행](managed-run.md) · [구조](architecture.md) · [개발·검증](development.md)

## 관리형 에이전트 실행

아래 명령은 기존 실행 상태를 이어가거나 사용자가 관리형 실행을 요청했을 때만 사용한다. 새 직접 제작에는 `init`이나 역할별 `start/complete`가 필요 없다. 명령은 개발하거나 관리형 문제를 확인할 때 참고용이다.

입력 폴더를 준비한 뒤 강의별 상태 파일을 만든다.

```powershell
python scripts/manage_run.py init <입력_폴더> --lecture-id <강의ID> --note-mode faithful   # --output-format 생략 시 faithful=md, deep=pdf
python scripts/manage_run.py next workspace/<강의ID>/run_state.json
python scripts/manage_run.py next workspace/<강의ID>/run_state.json --brief   # 모델표·원장 없이 준비된 역할만
```

파일명만으로 전사본을 알아보지 못하면 관리자가 내용을 확인한 뒤 분류를 지정한다.

```powershell
python scripts/manage_run.py init <입력_폴더> --lecture-id <강의ID> `
  --note-mode faithful --classify "강의내용메모.txt=transcript"
```

관리자 에이전트는 `next`에 표시된 역할만 실행하고, 함께 반환되는 `execution` 값에 따라 Python 또는 제한된 하위 에이전트에 배정한 뒤 시작과 완료를 기록한다. 역할의 산출물이 실제로 존재하지 않으면 통과 처리할 수 없다.

```powershell
python scripts/manage_run.py start workspace/<강의ID>/run_state.json --role source_mapper
python scripts/manage_run.py complete workspace/<강의ID>/run_state.json --role source_mapper --artifact work/source_map.json
```

역할이 실패하면 전체 입력으로 다시 시작할 수 없다. 실패한 위치를 지정한 국소 재검수 한 번만 허용한다. 최종 검수가 내용 결함을 반려했을 때는 검수가 직접 고친 파일을 `complete --patched`로 다시 기록하거나, `repair --reopen writer`로 집필 이후를 다시 열어 새 review cycle에서 한 번 더 검수한다.

```powershell
python scripts/manage_run.py fail workspace/<강의ID>/run_state.json `
  --role source_mapper --reason "교안 8쪽 대응 근거 부족"
python scripts/manage_run.py start workspace/<강의ID>/run_state.json `
  --role source_mapper --repair-scope "교안 8쪽과 연결된 전사 구간만" `
  --repair-packet "workspace/<강의ID>/review/repair_packet.json"
```

국소 고강도 검수가 필요하면 첫 의미 작업에서 남은 핵심 항목의 개별 패킷만 다음 게이트에 통과시킨다. 한 강의에서 두 번째 요청, 16KiB 초과 파일, `model_input=true`·`kind=*packet`·명시적 target 계약을 지키지 않은 파일, 역할과 맞지 않는 오류 분류는 거부된다. 실제 모델·effort는 현재 모드와 역할에 따라 `quality_high` 또는 `quality_xhigh` 프로필을 런타임 모델표로 해석해 반환된다.

```powershell
python scripts/manage_run.py escalate workspace/<강의ID>/run_state.json `
  --role transcript_auditor `
  --packet "workspace/<강의ID>/transcript/<강의ID>_packets/packet_0001.json" `
  --category proper_noun --reason "교안과 전사의 고유명사 충돌"
```

관리형 최종 검수는 모든 source unit의 처리 상태를 별도 JSON으로 남긴다. 시작할 때 현재 `review_cycle`의 유일한 검수 호출을 예약한다. Python 게이트가 ID 누락·중복, 빈 source map, 이유 없는 제외, 표시 위치 없는 미해결 항목을 거부하며, 통과한 source map과 coverage report를 실행 상태에 함께 묶는다.

```powershell
python scripts/validate_source_coverage.py work/source_map.json work/source_coverage.json
python scripts/manage_run.py complete workspace/<강의ID>/run_state.json `
  --role final_reviewer --artifact work/final_review.md `
  --source-map work/source_map.json --coverage-report work/source_coverage.json
```

```powershell
python scripts/manage_run.py repair workspace/<강의ID>/run_state.json `
  --reopen writer --reason "2장 도입 발언 누락" --findings work/final_review.md
```

자료 충실형 기본 Markdown은 다음처럼 출력한다. PDF가 필요한 경우에만 출력 확장자를 `.pdf`로 바꾼다. `deep` PDF는 [전용 TeX 경로](architecture.md#deep-pdf-출력)를 따른다. CLI 설치본에서는 `gongbu build`가 같은 스크립트를 부른다.

```powershell
python scripts/build_study_note_pdf.py workspace/<강의ID>/work/note_draft.md `
  --output output/<과목>_<차시>_학습노트.md --course "<과목>" --session "<차시>"
```

수식이나 설명 부족이 뒤늦게 발견되면 해당 선택 역할만 활성화한다. 입력이 바뀌지 않은 재실행에서는 통과한 중간 산출물을 재사용하고, 검수 실패 시 관련 역할과 그 하위 단계만 다시 실행한다. 자세한 기준은 `rules/orchestration.md`에 있다.

진행 중 교안이나 전사본이 추가·변경되면 상태 JSON을 삭제하지 않고 입력을 갱신한다. 변경이 없으면 기존 통과 상태를 유지하고, 변경이 있으면 영향받는 단계부터 다시 연다.

```powershell
python scripts/manage_run.py refresh-inputs workspace/<강의ID>/run_state.json
```

제작 모드를 바꾸면 전사와 자료 매핑은 유지하고 집필 이후 단계만 다시 실행한다.

```powershell
python scripts/manage_run.py set-mode workspace/<강의ID>/run_state.json `
  --note-mode deep --reason "수식 유도와 배경지식 보강 필요"
```

자동으로 활성화된 조건부 역할이 자료 확인 결과 불필요하면 이유를 남겨 비활성화한다.

```powershell
python scripts/manage_run.py deactivate workspace/<강의ID>/run_state.json `
  --role transcriber --reason "사용자 제공 전사본 사용"
```

## 진도별 제작과 이어 쓰기

교안 한 묶음을 여러 수업에 나눠 배우거나 현재 진도까지만 노트가 필요할 때 쓴다. 두 모드 공통이다. 이번 수업 범위를 `scope` JSON(파일별 `pages`·`lines`·`segments` 또는 `all`, 1부터 시작하고 양 끝 포함)으로 기록하고, 다음 수업은 새 진도만 가진 별도 실행을 `--continue-from`으로 앞 실행에 연결한다. 이미 검수가 끝난 앞부분은 다시 열지 않으며, 범위 밖 자료는 제외가 아니라 **미진행**으로 남는다.

```powershell
python scripts/manage_run.py init <과목폴더> --lecture-id L01_part01 --note-mode deep --scope <첫범위.json>
python scripts/prepare_source_map.py workspace/L01_part01/run_state.json --output-dir workspace/L01_part01/sources
python scripts/manage_run.py init <과목폴더> --lecture-id L01_part02 --note-mode deep --scope <새범위.json> `
  --continue-from workspace/L01_part01/run_state.json
python scripts/manage_run.py compose workspace/L01_part02/run_state.json --output <누적_원고.md>
```

`prepare_source_map.py`(CLI 설치본에서는 `gongbu prepare-sources`)는 모델을 부르지 않고 대상 범위의 원문·페이지·구간·해시를 무손실 근거 묶음으로 만든다. 요약·용어 교정·중요도 판정은 하지 않는다. `compose`는 검수가 끝난 진도별 원고를 모델 호출 없이 누적 원고로 잇는다. 범위 JSON 형식과 경계 규칙은 [진도별 제작 규칙](../rules/incremental-notes.md), 자료 충실형 관리형 경로의 전처리 순서는 [자료 충실형 경로](../rules/faithful-cost-path.md)에 있다.

## 여러 강의 병렬 처리

실행 상태가 강의(`lecture_id`) 단위로 완전히 분리되어 있어, 서로 다른 강의의 학습노트는 세션을 나눠 병렬로 만들 수 있다. 다음 세 가지 규칙만 지킨다.

1. **강의마다 입력 하위폴더를 분리한다.** `init`과 `refresh-inputs`는 지정한 폴더 전체를 해시로 기록하므로, 여러 강의 자료를 한 폴더에 섞으면 다른 강의 파일 추가가 입력 변경으로 오인된다.

   ```text
   input/
   ├─ 2026-03-10_과목A_본강의/
   ├─ 2026-03-11_과목B_본강의/
   └─ 2026-03-12_과목C_본강의/
   ```

2. **같은 강의에는 세션 하나만 둔다.** 락 파일(`run_state.json.lock`)은 명령 하나가 도는 짧은 순간의 충돌만 막아 준다. 락 오류가 없다고 다른 세션이 없다는 보장은 아니니, 같은 강의는 한 세션에서만 다룬다. 중단된 실행이 남긴 락이 확실할 때만 직접 지운다.

3. **전사는 한 번에 하나만 실행한다.** 전사 모델 하나가 GPU 메모리를 사실상 독점하므로 동시 전사는 메모리 부족으로 오히려 느려진다. 녹음이 여러 개면 배치 큐가 순서대로 처리한다.

   ```powershell
   python scripts/transcribe_batch.py "C:\자료\녹음폴더"
   ```

   탐색기에서는 녹음 파일들이나 폴더를 `배치전사.bat` 위로 끌어다 놓으면 된다. 전사가 끝난 강의부터 노트 제작(LLM 작업)을 시작하면 다음 강의의 전사(GPU 작업)와 자연스럽게 겹쳐 진행된다.
