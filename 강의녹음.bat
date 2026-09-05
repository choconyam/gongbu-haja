@echo off
chcp 65001 >nul
title 통합 학습노트 프로젝트 - 온라인 강의 시스템 오디오 녹음

echo.
echo   이 기능은 Windows에서 재생되는 온라인 강의 소리만 녹음합니다.
echo   대면 수업이나 마이크 입력은 녹음하지 않습니다.
echo   수강 권한과 학교·교수자의 녹음 허용 여부를 먼저 확인하세요.
echo.

python "%~dp0scripts\record_lecture.py" --list-devices
set "DEVICE_LIST_EXIT=%ERRORLEVEL%"
if not "%DEVICE_LIST_EXIT%"=="0" (
  echo.
  echo [오류] 녹음 장치를 확인하지 못했습니다. 위 설치 또는 장치 오류를 해결하세요.
  pause
  exit /b %DEVICE_LIST_EXIT%
)

set "LECTURE_ID="
set /p "LECTURE_ID=강의 식별자(예: 2026-03-10_과목A_본강의): "
if not defined LECTURE_ID (
  echo [오류] 강의 식별자가 필요합니다.
  pause
  exit /b 2
)

set "DEVICE_INDEX="
set /p "DEVICE_INDEX=출력 loopback 장치 index(기본 장치는 빈칸): "

set "PLAYBACK_RATE="
set /p "PLAYBACK_RATE=강의 재생 배속(기본 1.75, 사이트가 배속을 막으면 1): "
if not defined PLAYBACK_RATE set "PLAYBACK_RATE=1.75"

echo.
echo   먼저 30초 시험 녹음을 합니다. 온라인 강의를 시작 위치에서 일시정지하세요.
echo   아무 키나 누른 뒤 강의를 재생하고, 30초가 지나면 다시 일시정지하세요.
pause >nul

set "TEST_OUTPUT=%TEMP%\gongbu-haja_online_test_%RANDOM%_%RANDOM%.wav"
if defined DEVICE_INDEX (
  python "%~dp0scripts\record_lecture.py" --lecture-id "%LECTURE_ID%" --duration 30 --device-index "%DEVICE_INDEX%" --output "%TEST_OUTPUT%"
) else (
  python "%~dp0scripts\record_lecture.py" --lecture-id "%LECTURE_ID%" --duration 30 --output "%TEST_OUTPUT%"
)
set "TEST_EXIT=%ERRORLEVEL%"
if not "%TEST_EXIT%"=="0" (
  echo.
  echo [오류] 30초 시험 녹음에 실패했습니다. 본 녹음을 시작하지 않습니다.
  pause
  exit /b %TEST_EXIT%
)

echo.
echo   시험 파일을 재생합니다: %TEST_OUTPUT%
start "" "%TEST_OUTPUT%"
set "TEST_OK="
set /p "TEST_OK=강의 소리와 음량이 정상입니까? 정상일 때만 Y 입력: "
if /I not "%TEST_OK%"=="Y" (
  echo [중단] 본 녹음을 시작하지 않았습니다. 시험 파일은 위 경로에 남아 있습니다.
  pause
  exit /b 3
)

del /q "%TEST_OUTPUT%" 2>nul
set "RECORD_DURATION="
set /p "RECORD_DURATION=본 녹음 시간(초, Ctrl+C로 끝낼 경우 빈칸): "
echo.
echo   온라인 강의를 시작 위치로 되돌려 일시정지하세요.
echo   아무 키나 누르면 본 녹음을 시작합니다. 시작 메시지가 나오면 강의를 %PLAYBACK_RATE%배속으로 재생하세요.
pause >nul

if defined RECORD_DURATION (
  if defined DEVICE_INDEX (
    python "%~dp0scripts\record_lecture.py" --lecture-id "%LECTURE_ID%" --duration "%RECORD_DURATION%" --device-index "%DEVICE_INDEX%" --playback-rate "%PLAYBACK_RATE%"
  ) else (
    python "%~dp0scripts\record_lecture.py" --lecture-id "%LECTURE_ID%" --duration "%RECORD_DURATION%" --playback-rate "%PLAYBACK_RATE%"
  )
) else if defined DEVICE_INDEX (
  python "%~dp0scripts\record_lecture.py" --lecture-id "%LECTURE_ID%" --device-index "%DEVICE_INDEX%" --playback-rate "%PLAYBACK_RATE%"
) else (
  python "%~dp0scripts\record_lecture.py" --lecture-id "%LECTURE_ID%" --playback-rate "%PLAYBACK_RATE%"
)

set "RECORD_EXIT=%ERRORLEVEL%"
echo.
if not "%RECORD_EXIT%"=="0" (
  echo ============================================
  echo  녹음이 완료되지 않았습니다. 위 오류를 확인하세요.
) else (
  echo ============================================
  echo  녹음이 저장되었습니다. input 폴더를 확인하세요.
)
echo  창을 닫으려면 아무 키나 누르세요.
pause >nul
exit /b %RECORD_EXIT%
