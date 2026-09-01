@echo off
chcp 65001 >nul
title 통합 학습노트 프로젝트 - 강의 녹음 전사

if "%~1"=="" (
  echo.
  echo   전사할 강의 녹음 파일을 이 배치 파일 위로 끌어다 놓으세요.
  echo   파일명에서 과목명이나 날짜를 알 수 없을 때만 강의 식별자를 질문합니다.
  echo.
  pause
  exit /b 2
)

python "%~dp0scripts\transcribe_lecture.py" "%~1" --interactive
set "TRANSCRIBE_EXIT=%ERRORLEVEL%"

echo.
if not "%TRANSCRIBE_EXIT%"=="0" (
  echo ============================================
  echo  전사가 완료되지 않았습니다. 위 오류를 확인하세요.
) else (
  echo ============================================
  echo  전사가 완료되었습니다. workspace 폴더를 확인하세요.
)
echo  창을 닫으려면 아무 키나 누르세요.
pause >nul
exit /b %TRANSCRIBE_EXIT%
