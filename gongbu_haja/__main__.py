"""`python -m gongbu_haja` 진입점 — 설치 전 저장소 안에서도 같은 명령을 쓸 수 있다."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
