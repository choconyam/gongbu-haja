# Claude Code 진입 지침

이 저장소의 실행 지침은 `AGENTS.md`에서 시작한다. Claude Code로 이 프로젝트를 열었다면 `AGENTS.md`와 `rules/orchestration.md`를 읽고 결정적 작업 우선, 제한된 역할 입력, 최대 2개 하위 에이전트, 국소 재검수 원칙을 그대로 따른다.

실행 프로필(`economy_high`, `review_high`, `quality_high`, `quality_xhigh`)의 Claude 모델·effort는 `scripts/execution_profiles.py`의 표가 정하고(현재 economy→`claude-sonnet-5`, quality→`claude-opus-5`, 별칭이 아닌 전체 ID로 고정), `.claude/agents/`의 서브 에이전트 선언 4개는 그 표에서 생성된다. 관리자는 `manage_run.py next`가 런타임에 맞게 해석해 준 모델·effort를 그대로 쓰고 임의로 바꾸지 않는다. `faithful`은 저비용 집필과 독립 누락 검수, `deep`은 고품질 집필과 완성본 전체 논리 검수를 구분하며, 모든 역할을 최고가 모델로 일괄 실행하지 않는다. 국소 승격도 `manage_run.py escalate`가 허용한 16KiB 이하 핵심 패킷 하나에만 강의당 한 번 사용한다. 완성본 고비용 검수는 현재 `review_cycle`에서 한 번만 예약한다.
