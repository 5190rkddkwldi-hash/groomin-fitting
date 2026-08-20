@echo off
chcp 65001 >nul
rem 개발 환경 준비 + 실행 (Windows)
rem   dev.bat        가상환경 만들고 서버 실행
rem   dev.bat test   테스트만 실행
cd /d "%~dp0"

if not exist .venv (
  echo 가상환경을 만듭니다...
  python -m venv .venv
)
call .venv\Scripts\activate.bat

echo 필요한 패키지를 설치합니다...
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt -r requirements-dev.txt

if /i "%1"=="test" (
  python -m pytest
  goto :eof
)

echo.
echo   http://127.0.0.1:5000  에서 열립니다. ^(Ctrl+C 로 종료^)
echo   추천인 코드: grooming2026
echo.
python app.py
