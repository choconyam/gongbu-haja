#!/usr/bin/env python3
"""여러 스크립트가 함께 사용하는 입력 파일 형식 정의."""

from __future__ import annotations


# 전사 실행기, 실행 관리자, 전사 검증기가 반드시 같은 목록을 사용해야 한다.
# 한쪽에만 형식이 추가되면 녹화가 조용히 누락될 수 있으므로 여기서 단일 관리한다.
AUDIO_SUFFIXES = frozenset(
    {
        ".wav",
        ".mp3",
        ".m4a",
        ".aac",
        ".flac",
        ".ogg",
        ".opus",
        ".wma",
        ".mp4",
        ".webm",
    }
)

