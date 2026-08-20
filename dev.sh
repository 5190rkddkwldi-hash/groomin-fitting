#!/usr/bin/env bash
# 개발 환경 준비 + 실행 (macOS / Linux)
#   ./dev.sh          가상환경 만들고 서버 실행
#   ./dev.sh test     테스트만 실행
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "가상환경을 만듭니다..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "필요한 패키지를 설치합니다..."
pip install -q --upgrade pip
pip install -q -r requirements.txt -r requirements-dev.txt

if [ "$1" = "test" ]; then
  exec pytest
fi

echo
echo "  http://127.0.0.1:5000  에서 열립니다. (Ctrl+C 로 종료)"
echo "  추천인 코드: ${REFERRAL_CODE:-grooming2026}"
echo
exec python app.py
