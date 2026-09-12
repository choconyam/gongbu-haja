# 진도별 제작과 이어 쓰기

교안 한 묶음을 여러 수업에 나누어 배우거나, 과제 때문에 현재 진도까지만 노트가 필요할 때 적용한다. `faithful`과 `deep` 공통이다. 이 경우 아래의 범위·재사용 계약이 일반 규칙의 ‘전체 자료’, ‘완성본 전체 검수’, ‘입력 추가 시 refresh-inputs’보다 우선한다. 모델 등급과 독립 내용 검수 게이트는 유지한다.

## 범위 확정

- 교안 전체 분량과 실제 수업 진도를 구분한다. 사용자 지정과 전사·교안 대응으로 이번 마지막 페이지·발언을 확정하고, 경계가 불명확할 때만 질문한다. 과제에 필요하다는 이유로 아직 배우지 않은 뒷부분까지 임의 집필하지 않는다.
- 첫 부분부터 `scope`를 기록하고, 이어 쓸 때는 새 진도만 가진 별도 실행과 `--continue-from`으로 연결한다. 기존 완료 실행에 새 강의를 추가해 전체를 다시 열지 않는다.
- `scope` JSON은 내부 상태 폴더에 둔다. `sources`의 키는 입력 폴더 기준 `/` 상대경로이며, 파일마다 하나의 포함 범위를 지정한다. 페이지는 PDF의 물리적 페이지, 줄과 JSON 발언은 파일 안 순번이다. 모두 **1부터 시작하며 양 끝을 포함**한다. 발언의 원래 `id` 값이나 초 단위 타임스탬프가 아니다.

```json
{
  "label": "교안 1~12쪽, 첫 수업 진도",
  "sources": {
    "handout.pdf": {"pages": [1, 12]},
    "lecture_01.json": {"segments": [1, 53]},
    "assignment.txt": {"all": true}
  }
}
```

- 텍스트에는 `lines`, 별도 파일 전체에는 `all: true`를 쓴다. 녹음은 보존된 파생 전사의 `segments` 또는 `lines`로 지정하고 `prepare_source_map.py --transcript 원본=전사_JSON`을 연결한다. 추출본과 원본의 위치 체계가 다르면 몰래 치환하지 말고 원본 위치를 유지한 추출본을 준비한다.
- 한 파일의 새 범위는 모든 앞부분의 끝보다 뒤여야 한다. 같은 페이지를 나누어 배우는 경우 이 페이지 단위 자동 이어 쓰기로 처리하지 않는다. 기존 설명에 새 발언을 합치는 국소 수정으로 분류해 영향 범위만 고치고 재검수한다.
- 범위 사이의 간격은 명령이 자동으로 채우지 않는다. 수업에서 건너뛴 것인지, 범위 누락인지 관리자가 확인하고 이유를 내부 기록에 남긴다. 범위 밖 자료는 이번 coverage의 `excluded`가 아니라 **미진행**이다.

## 실행과 모델 입력

저장소에서 직접 실행하는 예시다. 과목 폴더에서는 `gongbu run`을 쓰고 상태 위치는 `<과목>/.gongbu/<부분ID>/`를 따른다.

```powershell
python scripts/manage_run.py init <과목폴더> --lecture-id L01_part01 --note-mode deep --scope <첫범위.json>
python scripts/prepare_source_map.py workspace/L01_part01/run_state.json --output-dir workspace/L01_part01/sources
# 기존 start/complete와 실제 역할 수행, 독립 검수·coverage·조판 게이트를 완료한다.
python scripts/manage_run.py init <과목폴더> --lecture-id L01_part02 --note-mode deep --scope <새범위.json> --continue-from workspace/L01_part01/run_state.json
python scripts/manage_run.py next workspace/L01_part02/run_state.json --brief
python scripts/prepare_source_map.py workspace/L01_part02/run_state.json --output-dir workspace/L01_part02/sources
```

- 유형 자동 분류가 틀리면 `--classify "lecture_01.json=transcript"`처럼 명시한다. PDF·전사 추출과 해시 확인은 로컬 작업이다. 범위 밖 본문을 모델 입력으로 보내지 않는다.
- scoped `deep`에서도 `prepare_source_map.py`로 선택된 범위의 무손실 근거와 ID를 먼저 만든다. 이는 의미 매핑의 대체가 아니다. `semantic` 실행은 지정된 담당이 이 근거에 의미 대응을 추가하며 `scope`, 파일 목록, 원문 위치와 안정 ID를 보존한다. `faithful`의 deterministic 경로는 그대로다.
- 모든 담당에게 이번 `scope`, 새 범위 source map, 필요한 선행 산출물만 전달한다. 원본 교안의 시각 확인·전사 검수 패킷도 이번 범위로 제한한다. 자동 후보 생성에 전체 파일이 필요해도 전체 결과를 모델에 넘기지 않는다.
- 이전 원고는 `next`의 `reused_parts`로 찾는다. 새 내용에 필요한 직전 절·정의·기호·참조만 읽고, 이전 본문 전체를 다시 요약하거나 집필하지 않는다. 새 부분의 기호와 참조는 앞부분에 맞춘다.
- 독립 최종 검수의 ‘전체’는 **이번 부분 전체와 앞부분과의 연결**이다. 새 범위의 모든 source unit과 조건·예시를 대조하고, 선행 정의·기호·논리 경계를 확인한다. 이전 부분 전체의 내용 검수를 다시 호출하지 않는다. 연결 검수의 실제 범위와 결과는 이번 검수 보고에 남긴다.

## 승인 원고 재사용과 누적 출력

- 각 부분의 `writer`에는 기준 본문을 정확히 하나 등록한다(`deep` PDF: `.tex`, 그 외 기존 Markdown 경로: `.md`). 부분마다 기준 원고가 하나이며 누적 파일은 파생 조합물이다. 표지나 독립 문서 preamble을 부분 본문에 넣지 않는다.
- 승인 원고, 원자료, source map, coverage, 역할 산출물과 실행 기록의 해시가 유효해야 재사용한다. source map의 원본 `sha256`와 절대 `evidence_path`·`evidence_sha256`도 보존하며 파생 근거 파일의 변경·삭제를 검사한다. 원고에서 사용하는 파생 그림 등은 해당 역할의 파일 또는 자산 폴더 산출물로 등록한다. 앞부분이 미완료이거나 변경되면 이어 쓰기를 중단한다. 누락된 검수 기록을 임의로 만들지 않는다.
- 다음 수업 전사는 새 파일로 저장한다. 선택된 **파일 전체 해시**를 사용하므로 기존 전사 파일에 내용을 덧붙이거나 원본 교안을 수정하면 이전 범위도 재사용 불가로 판정한다. 부분 내용 해시로 변경을 무시하는 기능은 없다. 반면 scope에 없는 새 파일 추가는 앞부분을 무효화하지 않는다.
- 완료된 앞부분을 다음 진도 추가 목적으로 `refresh-inputs`하지 않는다. 완료 기록도 재사용 근거라 변경하면 연결이 끊긴다. 기존 내용의 정정은 영향 범위를 먼저 확인하고 정상 수정·검수 절차를 거친 뒤 후속 연결을 다시 구성한다. 해시나 검수 원장을 직접 고쳐 통과시키지 않는다.
- scope 없는 과거 노트는 자동 이어 쓰기 대상으로 간주하지 않는다. 범위·기준 원고·검수 근거를 확인해야 하며, 이를 갖췄다고 추정해서 완료 상태를 만들어서는 안 된다.

```powershell
python scripts/manage_run.py verify workspace/L01_part02/run_state.json --check-inputs
python scripts/manage_run.py compose workspace/L01_part02/run_state.json --output <누적본문.tex>
python scripts/build_study_note_pdf.py <누적본문.tex> --note-mode deep --output <누적최종.pdf> --course <과목> --session <차시> --summary <현재까지의내용한줄>
python scripts/validate_note_output.py <누적최종.pdf>
```

- `compose`는 검수 완료된 부분들을 순서대로 연결한다. TeX는 본문을 복사하지 않는 `\input` 목록, Markdown은 기존 본문을 로컬에서 결합한 파생 파일이다. 기존 파일을 교체할 때만 `--force`를 쓰며 승인 원고·입력·상태는 덮어쓸 수 없다. 원고 경로에 TeX 특수문자가 있으면 명령이 거부하므로 안전한 경로를 사용한다.
- TeX 그림은 각 부분 원고의 폴더를 기준으로 찾는다. 이름 충돌 방지를 위해 누적 원고 폴더에 같은 이름의 그림을 별도로 두지 않는다. 본문 안의 추가 `\input`, 공유 매크로와 상호 참조는 경로·이름 충돌을 별도 확인한다. 기본 부분 원고는 자체 포함형으로 작성하고 새로운 매크로가 다른 부분에 누출되지 않게 한다. Markdown 결합은 링크를 자동 변환하지 않으므로 첫 집필부터 누적 전달 위치에서도 유효한 자산 경로와 부분별 고유 참조 ID를 사용하고, 조합 뒤 링크·이미지도 확인한다.
- 부분 사이에는 페이지를 나누며 표지는 누적 빌드에서 한 번만 만든다. 조합 원고를 직접 집필·수정하지 않는다. 이미 승인된 앞부분의 문장·수식·교안 배치를 재작성하지 않는다.
- `compose` 성공이나 부분별 `verify` 통과만으로 **누적 PDF 완료**를 선언하지 않는다. 누적 빌드·무결성 검사 뒤 모든 쪽을 렌더해 순서, 중복·누락, 경계, 그림·수식, 여백을 확인하고 기존 QA 기록에 누적 파일 해시와 검사 결과를 남긴다. 문제가 발견되면 영향 부분만 수정·재검수한다.
- 전체 PDF 재컴파일은 로컬 계산이며 모델 재집필과 구분한다. 누적 시각 확인과 새 범위의 내용 검수 비용까지 0이라고 주장하지 않는다. 사용자가 부분 PDF만 원하면 누적 조합은 생략한다.
