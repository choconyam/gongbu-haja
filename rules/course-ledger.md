# 과목 원장

과목 폴더의 `.gongbu/course.json`(과목 원장)과 학기 목록을 다룰 때 읽는다. 노트·노션 학기 요청이 원장 없는 과목에 닿았을 때, 자료·노트를 등록할 때, 진도·현황을 답할 때가 해당한다. 쓰기는 `../scripts/course_ledger.py`의 `gongbu course`·`gongbu semester` 명령으로만 하고 JSON을 직접 고치지 않는다.

## 원장의 성격

- 원장은 과목의 기본 모드·재료 조건, 강의 목록, 강의별 교안·녹음·전사와 노트 원고·최종본을 적은 색인이다. 실행 상태가 아니므로 관리형 실행으로 바꿀 이유가 되지 않고, 등록은 자체 점검·독립 검수 통과의 근거가 아니다.
- 노트의 `progress`는 에이전트의 진술이다. 수업이 그 강의를 끝냈고 노트가 그 범위를 모두 다룰 때만 `done`, 그 밖에는 `in_progress`다.
- 원장은 과목 폴더에만 둔다. 엔진 저장소 안의 `input/` 작업에는 만들지 않는다. 학기 목록은 과목 폴더 밖의 사용자 설정 폴더에 있으며 비밀값을 담지 않는다.
- `course note`·`course material add`의 `<ID>`와 계획의 `id`는 원장의 강의 ID다. 영문·숫자로 시작하고 영문·숫자·`_.-`만 쓴 41자 이내다(예: `ch05`, `w01`). 한글을 쓸 수 있는 녹음·전사 폴더 이름(`.gongbu/<강의ID>/`)과는 따로 정한다.

## 처음 쓰는 과목

노트 제작이나 노션 학기 요청이 원장 없는 과목에 닿으면 제작 전에 한 번만 진행한다. 진도 질문이면 사용자가 지금 하자고 할 때만 진행한다(아래 '현황과 현황판').

1. 한 메시지로 함께 묻는다: 기본 모드(`note-production-modes.md` 1절 — 자료를 보고 추천할 수 있지만 과목명으로 정하지 않는다), 재료 조건(교안만 / 교안+녹음), 표시할 과목 이름 확인.
2. `gongbu course init --name <이름> --mode faithful|deep --materials handout|handout+recording`을 실행한다.
3. 현재 학기가 있으면(`gongbu semester show`) `gongbu semester add-course`를 실행한다. 없으면 학기 이름·첫 주 월요일·주 수를 한 번 묻고 `gongbu semester init <ID> --title <이름> --start YYYY-MM-DD --weeks N` 뒤 `add-course`를 실행한다.
4. 강의·자료·기존 노트·무시할 파일 패턴·파일 이름 관례는 계획 JSON 하나로 `gongbu course import <plan.json>`에 넘긴다. 계획 파일은 `.gongbu/`나 임시 폴더에 쓰고 과목 폴더 최상위에 두지 않는다. 먼저 `--dry-run`의 강의별 요약과 무시 패턴을 확인한다.
   - 모양: `{"course": {"name", "aliases", "mode", "materials", "expect", "planned", "ignore", "conventions"}, "lectures": [{"id", "title", "weeks", "expect", "materials": [{"kind", "path", "part", "from"}], "note": {"source", "progress", "outputs", "covers", "mode"}}]}`. 강의는 `id`만, 자료는 `kind`·`path`만, 노트는 `source`·`progress`만 반드시 쓴다.
   - `weeks`는 그 강의의 학기 주차 번호 목록(예: `[7]`)이며, 교안·녹음 `받기` 할 일은 이 주차가 이미 지난 강의에만 뜨므로 알면 넣는다.
   - 등록하는 녹음으로 만든 전사본에는 `"from": "<녹음 경로>"`를 붙인다. 빠지면 그 녹음이 전사 안 된 것으로 남는다.

## 등록할 때

- 원장에 없는 강의(새 주차 등)는 `material add`·`course note` 전에 `{"lectures":[{"id":"w07","title":"7주차","weeks":[7]}]}` 같은 계획으로 `gongbu course import`해 먼저 더한다.
- 새 교안·녹음·전사는 `gongbu course material add <ID> <경로> --kind handout|recording|transcript [--part <범위>] [--from <녹음>] [--force]`로 그 강의에 붙인다. 최근 5분 안에 바뀐 파일은 아직 녹음·다운로드 중일 수 있어 거부된다(`course import`도 같다). 받는 중이면 끝난 뒤 등록하고, gongbu가 방금 만든 파일(전사 직후의 전사본, `gongbu record`가 막 끝낸 녹음)은 `--force`로 등록한다. 전사가 끝나면 노트 작성용 전사본을 `--kind transcript --from <녹음> --force`로 등록한다.
- 노트를 만들거나 고쳤으면 완료를 보고하기 전에 `gongbu course note <ID> --source <기준 원고> --progress done|in_progress [--output <최종본>]... [--covers <범위>]`를 실행한다. 요청 모드가 과목 기본값과 다르면 `--mode`도 준다. 이어 쓰기·수정마다 다시 실행한다. 노션 변환이 안 된다는 경고가 나오면 원고를 고친 뒤 다시 등록한다.
- 기준 원고의 경로를 바꿨으면 `gongbu notion rekey <예전> <새>`로 같은 노션 페이지를 이어 쓴다. 파일을 옮겼으면 `gongbu course relink`로 경로를 맞춘다.
- 기본 모드·재료 조건은 사용자가 앞으로 바꾸라고 할 때만 `gongbu course set`으로 바꾼다. 현재 내용은 `gongbu course show`로 본다.

## 현황과 현황판

- `gongbu status`(현재 과목)와 `gongbu status --all`(현재 학기 전체)은 네트워크·토큰 없이 읽기만 한다. 진도·"어디까지 했어?" 질문은 이것으로 답하고 제작 경로를 시작하지 않는다. 녹음·다운로드 중인 파일은 새 자료로 세지 않는다. `--json`의 `todo`는 사용자가 할 일, `ready`는 도구로 할 수 있는 다음 작업이다.
- 원장이 없는 과목이면 `status`는 종료 코드 3으로 알리고 아무것도 만들지 않는다. 진도 질문이면 처음 설정이 필요하다고 답하고 지금 할지 묻는다. 사용자가 하자고 할 때만 '처음 쓰는 과목' 1–4단계(기존 노트의 `course import` 포함)를 진행한 뒤 `status`로 답한다. 그 밖의 처음 설정은 제작·노션 학기 요청 때 한다.
- `status`에 등록 안 된 노트 원고가 보이면 새 노트를 쓰기 전에 어느 강의 것인지 확인해 등록한다.
- 원장을 바꿨고 그 학기에 노션 페이지가 있으면(`gongbu semester show`에 보인다) 확인 없이 `gongbu notion dashboard`를 실행한다. 현황판 규칙은 `notion-output-contract.md`의 '학기 페이지와 현황판' 절이다.
