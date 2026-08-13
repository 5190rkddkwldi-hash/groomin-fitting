# groomin-fitting — AI 쇼핑몰 피팅컷 생성기

모델 착용 사진 한 장으로, 얼굴 없는(목 아래 크롭) 한국 쇼핑몰 스타일 피팅컷을
여러 장 만들어주는 웹앱입니다. Google Gemini 이미지 모델(나노바나나)을 사용하며,
상세페이지 기획(스토리보드) 생성기도 함께 들어 있습니다.

## 주요 기능

- **빠른 생성** — 배경 프리셋(스튜디오·콘크리트 벽·공원 잔디밭·모던 카페 앞 등
  20여 종) 또는 랜덤으로 최대 10장을 병렬 생성. 같은 프리셋이라도 컷마다
  "같은 스타일의 다른 장소"가 나오도록 변주가 걸립니다
- **포즈 모음** — 마음에 든 컷 하나를 고르면 배경·모델·의상은 그대로 두고
  서 있는 포즈만 바꾼 12컷을 생성
- **상품 잠금** — 판매 상품의 색·프린트·디테일이 컷마다 달라지지 않게 잠금
- **무보정 폰카 리얼리즘** — 실제 인기 쇼핑몰 컷 분석을 반영해 AI 티를 줄임
- **상세페이지 기획** (/planner) — 상품 정보를 넣으면 섹션별 스토리보드
  (헤드카피·본문·이미지 가이드·CTA·태그)를 생성
- **전체 저장(ZIP)**, 실패 컷 개별 재시도, 완성되는 대로 표시

## 실행 방법 (로컬)

```bash
pip install -r requirements.txt
python app.py
# http://127.0.0.1:5000
```

Windows는 `run.bat` 더블클릭으로 실행할 수 있습니다 (서버가 죽으면 자동 재시작).

## 로그인

첫 화면에서 **추천인 코드**와 **상호명**을 입력하면 입장됩니다.
코드 기본값은 `grooming2026`이며, 환경변수 `REFERRAL_CODE`로 바꿀 수 있습니다.
세션 서명 키는 `SECRET_KEY` 환경변수로 설정하세요 (미설정 시 개발용 기본값).

## API 키

방문자가 자신의 [Google AI Studio](https://aistudio.google.com) API 키를
요청마다 직접 입력합니다. 서버는 키를 저장하지 않고 해당 요청 처리에만 씁니다.

## 배포 (Render)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/5190rkddkwldi-hash/groomin-fitting)


`render.yaml`이 포함되어 있어 [Render](https://render.com)에서 저장소를
연결하면 Blueprint로 바로 배포됩니다. 배포 후 주소는
`https://groomin-fitting.onrender.com` 형태가 됩니다.

## 기여

이슈·PR 환영합니다. 배경 프리셋과 포즈는 `app.py` 상단의
`BACKGROUNDS` / `POSES` / `STANDING_POSES`에 모여 있어서, 프롬프트 문구만
다듬어도 결과가 크게 달라집니다.

## 라이선스

[MIT](LICENSE)
