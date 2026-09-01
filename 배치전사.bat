@echo off
chcp 65001 >nul
title 통합 학습노트 프로젝트 - 강의 녹음 일괄 전사

if "%~1"=="" (
  echo.
  echo   전사할 강의 녹음 파일 여러 개 또는 녹음이 든 폴더를
  echo   이 배치 파일 위로 끌어다 놓으세요.
  echo   GPU 충돌을 막기 위해 한 번에 하나씩 순서대로 전사합니다.
  echo.
  pause
  exit /b 2
)

python "%~dp0scripts\transcribe_batch.py" %* --interactive
set "BATCH_EXIT=%ERRORLEVEL%"

echo.
if not "%BATCH_EXIT%"=="0" (
  echo ============================================
  echo  일부 파일이 전사되지 않았습니다. 위 요약을 확인하세요.
) else (
  echo ============================================
  echo  모든 파일의 전사가 끝났습니다. workspace 폴더를 확인하세요.
)
echo  창을 닫으려면 아무 키나 누르세요.
pause >nul
exit /b %BATCH_EXIT%
