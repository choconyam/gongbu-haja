"""gongbu-haja 전역 명령 패키지.

`pipx install gongbu-haja` 뒤 어느 과목 폴더에서나 `gongbu <명령>`으로 엔진의
Python 스크립트를 부른다. 규칙·역할 프롬프트·스크립트 본체는 저장소 루트
(`scripts/`, `agent_prompts/`, `rules/`)에 그대로 있고, 배포 시 이 패키지 안의
`engine/`로 복사된다. 이 패키지는 위치를 찾고 인자를 과목 폴더 기준으로
보정하는 얇은 껍데기다.
"""

__version__ = "1.5.0"
