# Agent Orchestration and Token Control

이 문서는 역할 프롬프트와 실제 에이전트 실행을 구분하고, 필요한 역할만 제한된 문맥으로 실행하는 방법을 정의한다.

## 1. 용어

- **역할 프롬프트**: `agent_prompts/*.md`에 기록된 직무와 품질 기준이다. 파일 자체는 실행 중인 에이전트가 아니다.
- **관리자 에이전트**: 전체 작업 동안 유지되며 범위, 실행 계획, 상태 전이, 반환 경로를 관리하는 실제 모델 프로세스다.
- **담당 에이전트**: 관리자가 특정 역할 프롬프트와 입력 묶음, 출력 계약을 배정해 실행한 별도 모델 프로세스다.
- **Python 게이트**: 입력 해시, 파일 구조, 선후관계, 산출물 존재, 문법과 빌드를 결정적으로 검사하는 코드다. 의미 판단을 대신하지 않는다.
- **문맥 묶음**: 한 역할이 현재 단원을 처리하는 데 필요한 원문 구간과 선행 산출물만 모은 입력이다.
- **실행 프로필**: 역할을 `local_python`, `economy_high`, `economy_max`, `quality_high`, `quality_xhigh` 중 어디에 배정할지 나타내는 비용·품질 계약이다. Codex에서는 각각 Python, Luna `high`, Luna `max`, Sol `high`, Sol `xhigh`에 대응하며 다른 런타임은 같은 의도로 매핑한다.

실제 모델 프로세스가 배정되지 않았다면 “해당 에이전트를 실행했다”고 기록하지 않는다. 다만 실행 상태에서 `executor=python`인 역할은 별도 에이전트가 아니라 기록된 Python 명령과 산출물로 실행한다.

## 2. 관리자와 Python의 경계

관리자 에이전트가 담당하는 일:

- 사용자의 현재 요청과 학습 목적 해석;
- 자료 묶음과 강의 식별 확정;
- 조건부 역할 활성화;
- 담당 에이전트 실행과 결과 전달;
- 의미 검수 실패를 적절한 역할로 반환;
- 최종 완료 판정.

Python이 담당하는 일:

- 사용자가 온라인 강의 녹음을 명시적으로 요청한 경우 Windows 시스템 오디오를 충돌 없는 새 입력 파일로 저장;
- 입력 파일 목록, 유형, 크기, SHA-256 해시 기록;
- 역할의 선행 조건과 상태 전이 검사;
- 이미 통과한 동일 입력 산출물의 재사용;
- 필수 산출물의 존재와 경로 기록;
- 전사 패키지, 노트 구조, LaTeX 빌드 같은 결정적 검사.
- 선택된 `faithful` 또는 `deep` 제작 모드를 실행 상태에 기록하고 모드 변경 시 집필 이후만 무효화.

Python은 교수 설명의 중요성, 요약의 정확성, 학습 난이도, 환각 여부를 독자적으로 확정하지 않는다. PDF에서 뽑은 반복어·괄호 병기·영문 표기는 어디까지나 **용어 후보**다. Python이 전공 용어라고 확정하거나 발음 유사어를 임의 치환하지 않는다.

### 결정적 처리 우선 원칙

모델을 호출하기 전에 아래 순서를 지킨다.

| 단계 | Python 책임 | 서브에이전트 책임 |
|---|---|---|
| 1. 원자료 추출 | PDF·문서 텍스트, 페이지·구간 포인터, 해시 추출 | 스캔 실패나 자료 의미가 애매할 때만 판정 |
| 2. 전사 후보 탐지 | ASR 신뢰도, 무음 확률, 압축률, 반복, 시간 이상, 숫자·평가조건 후보 탐지 | 후보가 실제 오류인지 판정 |
| 3. 용어 처리 | 빈도·괄호 병기·영문 표기로 후보와 근거 위치 생성 | 실제 용어, 약어·동의어, 올바른 표기 확정 |
| 4. 문맥 패킷 | 의심 구간과 앞뒤 최대 2개 구간, 관련 교안 발췌만 묶고 해시 기록 | 이 작은 패킷만 읽고 교정 결정을 구조화해 반환 |
| 5. 교정 적용 | 에이전트가 승인한 `구간 ID + 정확한 원문 + 대체문`만 검증해 적용하고 변경 로그 보존 | 숫자·고유명사·평가조건처럼 중요한 항목의 근거와 불확실성 판정 |
| 6. 검증·재사용 | 구조 검사, 빌드, 산출물·입력 해시, 캐시 일치 여부 확인 | 자동 검사로 판단할 수 없는 내용·시각 품질의 제한 표본 검수 |

Python이 만든 후보를 확정 사실로 승격하지 않는다. 자동 치환은 승인된 매핑이 현재 구간의 원문과 정확히 일치할 때만 수행한다. 철자 거리나 발음 유사도만으로 전사를 자동 변경하지 않는다.

### 비용 우선 서브에이전트 정책

- 전사 후보 판정, 자료 대응, 조판 표본처럼 범위가 제한된 반복 의미 작업은 `economy_high`(`gpt-5.6-luna`, `high`)로 실행한다.
- `faithful` 집필은 `economy_high`, 모든 source unit의 누락·왜곡·중복을 독립 대조하는 최종 검수는 `economy_max`(`gpt-5.6-luna`, `max`)로 실행한다. 이 검수는 외부 배경지식을 만들지 않는다.
- `deep` 집필·교수 설명 통합·교육 보강·수식 의미 검수는 `quality_high`(`gpt-5.6-sol`, `high`)로 실행한다. 완성본 전체의 논리 순서, 선행개념, 중간 사고, 유도와 적용 조건을 보는 독립 최종 검수 1회는 `quality_xhigh`(`gpt-5.6-sol`, `xhigh`)로 실행한다.
- 모드별 최종 Luna `max` 또는 Sol `xhigh` 완성본 검수는 `run_state.json`의 현재 `review_cycle`에 시작 전에 예약하고 한 번만 실행한다. source map과 조판 산출물의 SHA-256 지문이 이전 호출과 같으면 cycle 번호가 달라도 거부한다. 입력·제작 모드·명시적 사용자 편집 계약이 바뀌면 새 cycle을 열되, 실패 복구나 같은 완성본 재독을 위해 cycle을 늘리지 않는다.
- Terra는 실행 프로필에 두지 않는다. Sol `max`와 모든 역할의 일괄 Sol 실행도 기본 경로에 두지 않는다.
- 숫자·고유명사·수식·평가조건·근거 충돌·논리 또는 유도 공백이 남으면 **작고 중요한 미해결 패킷 하나만** `manage_run.py escalate`에 통과시킨다. `faithful`의 국소 충돌은 `quality_high`, `deep`의 집필 단계 국소 충돌은 `quality_xhigh`로 강의당 한 번만 재검수한다. `deep` 최종 검수는 이미 `quality_xhigh`이므로 다시 승격하지 않는다.
- 한 역할의 전체 재시도 기본 횟수는 0회다. 실패한 절·구간만 고친 뒤 해당 범위와 하위 산출물만 다시 검사한다.
- 관리자 에이전트는 범위·상태·충돌·최종 완료만 관리한다. 담당 역할을 대신해 전체 전사나 전체 노트를 반복 집필하지 않는다.
- 런타임이 서브에이전트 모델 선택을 지원하면 전체 대화 이력을 상속하지 않고 역할 프롬프트와 제한된 근거 묶음만 전달한다. 지원하지 않으면 같은 입력 경계를 유지해 순차 실행한다.

역할별 기본 실행 방식은 다음과 같다. 실제 값은 `manage_run.py next`의 `execution` 필드를 따른다.

| 역할 | 기본 실행 | 의미 작업 프로필 | 제한적 승격 |
|---|---|---|---|
| `transcriber` | Python | 없음 | 없음 |
| `transcript_auditor` | Python 후보 생성 + 서브에이전트 | 두 모드 `economy_high` | 중요 미해결 개별 패킷만 `quality_high` |
| `source_mapper` | Python 인벤토리·안정 ID + 서브에이전트 | 두 모드 `economy_high` | 근거 충돌 핵심 패킷만 `quality_high` |
| `writer` | 서브에이전트 | `faithful=economy_high`, `deep=quality_high` | 모드별 `quality_high` 또는 `quality_xhigh` |
| `instructor_integrator` | 서브에이전트 | `faithful=economy_high`, `deep=quality_high` | 왜곡 위험 핵심 패킷만 모드별 고강도 프로필 |
| `formula_code_checker` | Python 계산·실행 + 서브에이전트 | `faithful=economy_high`, `deep=quality_high` | 핵심 수식 충돌만 모드별 고강도 프로필 |
| `pedagogy_editor` | 서브에이전트 | `faithful=economy_high`, `deep=quality_high` | 핵심 개념·유도 공백만 모드별 고강도 프로필 |
| `layout_builder` | Python 빌드·렌더 + 서브에이전트 표본 검수 | 두 모드 `economy_high` | 없음; 렌더·구조 오류는 Python 또는 같은 프로필 국소 수정 |
| `final_reviewer` | 서브에이전트 | `faithful=economy_max`, `deep=quality_xhigh` | `faithful`의 의미 충돌만 `quality_high`; `deep` 추가 승격 없음 |
| `maintainer` | Python | 없음 | 없음 |

## 3. 최소 실행 경로

항상 실행하는 실제 역할은 다음으로 제한한다.

1. `source_mapper`
2. `writer`
3. `layout_builder`
4. `final_reviewer`

`faithful`의 최소 경로는 위 네 역할이다. `deep`에서는 `pedagogy_editor`를 기본 활성화해 배경지식과 중간 사고의 누락을 점검한다. `maintainer`는 최종 전달 파일을 정리할 때만 짧게 실행한다. 다음 역할은 조건부다.

| 역할 | 활성화 조건 |
|---|---|
| `transcriber` | 녹음은 있고 사용할 전사본은 없음 |
| `transcript_auditor` | 녹음 또는 전사본이 있음 |
| `instructor_integrator` | 전사에서 교수 고유 설명을 노트에 반영해야 함 |
| `formula_code_checker` | 수식, 수치, 그래프, 코드, 실험 결과의 검증 대상이 발견됨 |
| `pedagogy_editor` | `deep`에서는 기본 활성, `faithful`에서는 작성 또는 검수 결과가 명백한 연결 부족으로 판정될 때만 제한적으로 활성 |

파일 확장자만으로 수식이 없다고 확정하지 않는다. 자료 대응표에서 수식·코드 검증 대상이 발견되면 관리자가 `formula_code_checker`를 활성화한다.

## 4. 역할 실행 계약

각 담당 에이전트를 실행할 때 관리자는 다음을 명시한다.

- 역할 이름과 읽을 역할 프롬프트;
- 현재 처리할 단원 또는 자료 범위;
- 허용된 입력 파일과 원문 구간;
- 읽어야 할 선행 산출물;
- 생성할 산출물의 정확한 경로;
- 통과 조건과 자동 검사 명령;
- 미해결 항목을 기록할 형식.
- 실행 상태의 `note_mode`, 사용자 표시명, 외부 보강 허용 범위.

담당 에이전트는 배정되지 않은 전체 자료를 임의로 다시 읽지 않는다. 추가 문맥이 필요하면 필요한 페이지, 타임스탬프, 절을 특정해 관리자에게 요청한다.

## 5. 문맥과 토큰 통제

- 오디오 전사와 PDF 텍스트 추출은 로컬 도구로 한 번 수행하고 결과를 저장한다.
- 전사 검수 전에 `../scripts/prepare_transcript_review.py`로 용어 후보와 의심 구간 패킷을 만든다. 전체 후보와 전체 패킷 색인은 `model_input=false`인 로컬 캐시다. manifest도 모델에 보내지 않고 `../scripts/select_review_packets.py`로 조회한다. 담당 에이전트에는 selector가 총 16KiB 안에서 고른 `model_input=true` 개별 패킷만 전달한다.
- 원자료는 페이지, 타임스탬프, 문제, 코드 셀 또는 주제 블록으로 색인한다.
- 작성 담당은 전체 원본 대신 `자료 대응표 + 현재 단원의 근거 구간`을 받는다.
- 교수 설명 담당은 `현재 초안 + 대응 전사 구간 + 정렬표`만 받는다.
- 수식·코드 담당은 검증 대상이 있는 절과 직접 근거만 받는다.
- `faithful` 최종 검수 담당은 source map의 모든 단위를 대응 노트 위치와 대조한다. `deep` 최종 검수 담당은 완성본 전체를 한 번 읽고 논리·유도·설명 연결을 검사한다. 두 경우 모두 전체 원시 전사·녹음·교안을 반복 입력하지 않고 문제 source unit의 근거만 확장한다.
- 최종 검수는 `study_note_source_coverage` JSON을 만들고 `../scripts/validate_source_coverage.py`를 통과해야 한다. `included`·`merged`는 노트 위치, `excluded`는 이유, `unresolved`는 이유와 학생용 노트의 표시 위치가 필요하다.
- 같은 원자료를 여러 역할에 전달해야 하면 재추출하거나 재요약하지 않고 저장된 색인과 문맥 묶음을 재사용한다.
- 수정 실행은 입력 해시와 선행 산출물 해시를 비교해 영향을 받은 단계만 무효화한다.
- 역할 에이전트를 새로 시작할 때 런타임이 지원하면 전체 대화 이력을 상속하지 않는다. 역할 프롬프트와 명시된 파일 경로만 전달한다.
- 같은 역할이 실패해도 전체 입력으로 다시 시작하지 않는다. 실패한 범위의 `model_input=true`, `kind=*packet`, 명시적 target이 있는 16KiB 이하 JSON에만 직접 근거를 추가해 최대 한 번 재검수한다. 첫 시도 뒤에는 동일 프로필 국소 repair 또는 `manage_run.py escalate`가 반환한 모드별 고강도 검수 중 하나만 허용한다. `final_reviewer`는 예외로 전체 검수 1회 안에서 국소 패치와 해당 위치 재확인까지 끝내며 두 번째 전체 호출을 하지 않는다.
- 단일 강의의 검증된 전사·정렬표·대응표가 있으면 작성 이후 역할에 원본 녹음이나 전체 원시 전사를 다시 전달하지 않는다.
- 학생용 최종본에는 사용자가 요청하지 않은 쪽수·타임스탬프를 넣지 않는다. 추적 정보는 내부 대응표와 검수 보고서에만 유지한다.
- 단일 최종 PDF는 `maintainer`를 생략한다. 복수 파일 패키징, 경로 이동, 전달 목록 생성이 실제로 필요할 때만 활성화한다.
- `faithful`은 페이지별 대응표와 해당 근거 구간만 전달하고 외부 보강 검색·장문 유도를 생략한다.
- `deep`도 전체 전사나 전체 대화를 전달하지 않는다. 현재 단원에 필요한 근거와 선행개념만 추가한다.

첫 요약문만 재사용해 원래 근거가 사라지게 하지 않는다. 압축된 문맥에는 항상 원본 페이지나 타임스탬프 포인터가 남아야 한다.

### 단일 강의 빠른 경로

한 강의에 교안과 검증된 전사·정렬표가 있고 미해결 항목이 없으면 다음 경로를 쓴다.

1. 저장된 `source_map`을 재사용하거나 변경 페이지에 한해 갱신한다.
2. `writer`가 교안별 설명과 교수 고유 설명을 한 번에 통합한다.
3. `layout_builder`가 최종 형식으로 만들고 전 페이지를 한 번 렌더 검수한다.
4. `faithful`의 `final_reviewer`는 모든 source unit의 누락·왜곡을 Luna `max`로 대조하고, `deep`의 `final_reviewer`는 완성본 전체의 논리·유도를 Sol `xhigh`로 한 번 검수한다.
5. coverage report를 Python 게이트로 검증하고 source map과 함께 실행 상태에 기록한다.

입력이 그대로인데 디자인이나 출력 순서만 바뀌면 다음처럼 조판 이후만 다시 연다.

```powershell
python scripts/manage_run.py rerun workspace/<강의ID>/run_state.json --role layout_builder --change-kind output_contract --reason "사용자 출력 형식 변경"
```

국소 고강도 재검수는 다음처럼 상태 게이트를 먼저 통과해야 한다. 명령이 반환한 `quality_high` 또는 `quality_xhigh` 실행 계약만 사용하며, 패킷 크기와 강의당 1회 제한을 우회하지 않는다.

```powershell
python scripts/manage_run.py escalate workspace/<강의ID>/run_state.json --role <역할명> --packet <개별_패킷_JSON> --category <분류> --reason <승격_이유>
```

최종 검수는 source map과 coverage report를 결정적으로 대조한 뒤 두 파일을 상태에 함께 묶는다.

```powershell
python scripts/validate_source_coverage.py workspace/<강의ID>/<source_map>.json workspace/<강의ID>/<coverage>.json
python scripts/manage_run.py complete workspace/<강의ID>/run_state.json --role final_reviewer --artifact <검수_결과> --source-map <source_map_JSON> --coverage-report <coverage_JSON>
```

제작 모드가 바뀌면 다음처럼 전사·정렬·자료 매핑은 유지하고 집필 이후만 다시 연다.

```powershell
python scripts/manage_run.py set-mode workspace/<강의ID>/run_state.json --note-mode deep --reason "중간 유도와 배경 설명 필요"
```

## 6. 상태 전이

역할 상태는 다음 중 하나다.

- `skipped`: 현재 실행 계획에서 비활성;
- `blocked`: 필수 선행 역할이 아직 통과하지 않음;
- `ready`: 실행 가능한 상태;
- `running`: 실제 담당 에이전트 또는 도구가 작업 중;
- `passed`: 산출물과 역할별 검수가 완료됨;
- `failed`: 오류 또는 의미 검수 실패가 기록됨.

허용되는 기본 전이는 `skipped → blocked/ready`, `blocked → ready`, `ready → running`, `running → passed/failed`, `failed → running`(재시작)이다. 선행 역할이 통과하지 않은 상태에서 후속 역할을 완료 처리하지 않는다.

조건부 역할을 나중에 활성화해도 정식 선행 관계를 복원한다. `pedagogy_editor`와 `formula_code_checker`는 `writer` 통과 전 실행할 수 없고, `instructor_integrator`는 `writer`와 `transcript_auditor` 통과 전 실행할 수 없다.

## 7. 입력 변경과 수동 라우팅

입력이 바뀌었을 때 `run_state.json`을 삭제하거나 직접 편집하지 않는다.

```powershell
python scripts/manage_run.py refresh-inputs workspace/<강의ID>/run_state.json
```

입력 해시와 파일 목록이 같으면 기존 통과 상태를 유지한다. 변경이 있으면 전사·교안 정렬·자료 매핑 중 영향을 받는 가장 이른 단계와 그 하위 단계만 무효화한다.

파일명만으로 전사본임을 알 수 없는 경우 관리자가 의미를 확인한 뒤 분류를 명시한다.

```powershell
python scripts/manage_run.py refresh-inputs workspace/<강의ID>/run_state.json --classify "강의내용메모.txt=transcript"
```

자동으로 활성화된 조건부 역할이 실제로 불필요하면 이유를 기록해 비활성화한다.

```powershell
python scripts/manage_run.py deactivate workspace/<강의ID>/run_state.json --role transcriber --reason "사용자 제공 전사본 사용"
```

## 8. 강의 간 병렬 실행

실행 상태와 산출물이 `lecture_id` 단위로 분리되어 있으므로, 서로 다른 강의는 병렬로 진행할 수 있다. 다음 경계를 지킨다.

- 강의마다 입력 하위폴더를 분리한다(`input/<강의ID>/`). 입력 해시는 폴더 전체 기준이므로 여러 강의 자료를 한 폴더에 섞으면 다른 강의 파일의 추가·변경이 이 강의의 입력 변경으로 기록된다.
- 같은 강의 상태 파일에는 세션 하나만 접근한다. 상태를 수정하는 모든 명령은 `run_state.json.lock` 락으로 보호되며, 락 충돌 오류는 동시 접근 사고의 신호다. 중단된 실행이 남긴 락이 확실할 때만 수동으로 삭제한다.
- 관리자 문맥도 강의 단위로 분리한다. 한 관리자 세션이 여러 강의를 오가며 자료를 섞지 않는다. 여러 강의를 병렬로 처리할 때는 강의별 관리자 세션을 권장한다.
- 시스템 오디오 녹음은 현재 PC의 기본 출력 장치를 공유하므로 동시에 하나만 실행한다. 재생 장치를 바꾸거나 다른 소리를 재생하면 같은 녹음에 섞일 수 있다.
- 전사는 GPU 메모리를 독점하므로 동시에 하나만 실행한다. 녹음이 여러 개면 `../scripts/transcribe_batch.py`가 순서대로 처리한다. 전사가 끝난 강의의 노트 제작(모델 작업)과 다음 강의의 전사(GPU 작업)는 자원이 겹치지 않으므로 병행해도 된다.

## 9. 실패 반환

검수 실패는 전체 파이프라인을 처음부터 반복하지 않는다.

- 전사 오류 → `transcriber` 또는 `transcript_auditor`;
- 원자료 누락 → `source_mapper`;
- 설명 누락·왜곡 → `writer` 또는 `instructor_integrator`;
- 수식·수치·코드 오류 → `formula_code_checker` 이후 `writer`;
- 이해 흐름 부족 → `pedagogy_editor`;
- 조판·글꼴·깨진 참조 → `layout_builder`.

반환 시 위치, 근거, 필요한 수정, 다시 검사할 명령을 함께 전달한다.

## 10. 완료 게이트

완료로 판정하려면 다음이 모두 참이어야 한다.

- 실행 계획에서 활성화된 필수 역할이 모두 `passed`다.
- 각 통과 역할에 존재하는 산출물 경로가 기록되어 있다.
- 입력 파일이 실행 시작 때의 해시와 일치하거나 변경 영향 단계가 다시 실행되었다.
- 전사와 최종 노트의 Python 검증이 통과했다.
- 최종 검수 담당이 의미 정확성·근거 충실성·학습 품질을 통과시켰다.
- 미해결 항목이 결과물과 전달 보고에서 숨겨지지 않는다.
