# 구조 설명

`app.py` 한 파일에 다 들어 있습니다. 파일을 쪼개지 않은 이유는, 이 프로젝트에서
정말 중요한 건 **프롬프트 문구들**이고 그것들이 한눈에 보이는 편이 낫기 때문입니다.

## 큰 그림

```
 브라우저                          Flask (app.py)                 Google Gemini
 ────────                          ──────────────                 ─────────────
 사진 + 옵션  ──POST /api/process──▶ 폼 검사
                                    ↓
                                   프롬프트 조립
                                    ↓
                                   1536px 로 축소  ──generate_content──▶ 이미지 모델
                                    ↓                                     ↓
 컷 1장 표시  ◀───data URL(PNG)──── 응답에서 이미지 꺼내기 ◀──────────────┘
```

화면은 컷을 **한 장씩 따로** 요청합니다 (`count=1` + `index`). 최대 4개를 동시에
보내고 완성되는 대로 화면에 붙입니다. 그래서 한 장이 실패해도 나머지는 살고,
실패한 컷만 다시 시도할 수 있습니다.

## 두 가지 모드

| | 빠른 생성 (`quick`) | 포즈 모음 (`poseset`) |
| --- | --- | --- |
| 무엇을 하나 | 배경을 새로 만들어 착용컷 생성 | 보낸 사진 그대로, 포즈만 변경 |
| 템플릿 | `PROMPT_NEW_SCENE` | `PROMPT_SAME_SCENE` |
| 포즈 목록 | `POSES` | `STANDING_POSES` |
| 최대 장수 | `QUICK_MAX` (10) | `POSESET_MAX` (12) |
| 사진 전달 | 파일 업로드 | 앞서 만든 컷의 data URL |

`reference` 필드에 data URL 이 오면 `mode` 와 상관없이 **자동으로 포즈 모음**이
됩니다. 이미 만든 컷을 이어받는 경우이기 때문입니다.

## 프롬프트가 조립되는 순서

`PROMPT_NEW_SCENE` 의 자리표시자에 아래 조각들이 채워집니다.

```
{detail_rule}    누끼컷을 같이 올렸을 때만 — 상품 디테일의 기준
{focus}          상품 종류별 초점 (PRODUCTS)
{garment_lock}   판매 상품 잠금 — 색·프린트·핏 고정
{model_rule}     표준 모델 (180cm/79kg 한국 남성)
{framing}        상품 종류별 구도
{face_rule}      목 아래 크롭 (얼굴 노출 금지)
{scene_block}    배경 — BACKGROUNDS + 변주 규칙 (아래 참고)
{mood_rule}      상의 + 스튜디오 계열일 때만 얹는 무드
{pose}           포즈 한 줄
{pose_style}     포즈 공통 스타일
{styling_rule}   코디 지시, '그대로'면 OUTFIT_KEEP_RULE
{accessory_rule} 사용자가 적은 장신구
{realism_rule}   무보정 폰카 리얼리즘
```

`{scene_block}` 은 이렇게 만들어집니다.

```
BACKGROUND_RULE_TEMPLATE(setting = BACKGROUNDS[키])
  + GARMENT_AWARE_RULE      (랜덤 배정일 때)
  + LOCATION_RULE_VARY
  + SCENE_VARIETY_RULE(n=컷번호) + SCENE_VARIETY[컷번호]   (같은 프리셋 반복 방지)
```

같은 프리셋으로 여러 장을 뽑으면 죄다 비슷한 장면이 나오던 문제 때문에,
컷 번호마다 **변주 축**(재질/색, 빛/시간대, 카메라 관계, 부속 요소)을 돌려 씁니다.

## 이미지 모델 폴백

`_generate_image_with_fallback()` 이 `IMAGE_MODELS` 를 앞에서부터 시도합니다.

- 401/403(키 문제)이면 **폴백하지 않고** 즉시 알립니다 — 다른 모델로도 안 되니까요
- 그 외 오류·타임아웃이면 다음 후보로 넘어갑니다
- 한 번 성공한 모델은 `_image_model_pick` 에 기억해, 죽은 모델의 타임아웃을
  컷마다 다시 기다리지 않습니다
- 전부 실패하면 `ImageModelUnavailable` → 502 와 함께 "혼잡하니 잠시 후" 안내

> 화질이 우선이라 `lite` 계열은 후보에 넣지 않습니다.

## 부분 실패를 대하는 방식

컷을 여러 장 만들다 중간에 끊겨도, **이미 만든 컷은 버리지 않고** `warning` 과
함께 돌려줍니다. 사용자가 다 날리고 처음부터 하는 일이 없게 하기 위해서입니다.

응답에 이미지가 없으면(안전필터 등) 같은 프롬프트로 **한 번만** 즉시 재시도합니다.

## 로그인 게이트

`require_login()` 이 모든 요청 앞에 섭니다.

- `/login`, `/static`, `/favicon.ico` 는 통과
- 세션에 `shop` 이 있으면 통과
- `/api/*` 는 리다이렉트 대신 **401 JSON** — 화면이 요청 중일 때 로그인 페이지
  HTML 을 받아버리는 사고를 막습니다
- 그 외는 `/login` 으로 리다이렉트

코드는 `REFERRAL_CODE`, 세션 서명 키는 `SECRET_KEY` 환경변수로 바꿉니다.

## 상세페이지 기획 (`/planner`)

이미지와 무관한 별도 기능입니다. 상품 정보를 받아 `PLAN_PROMPT` 로 텍스트 모델을
호출하고, `response_mime_type=json` 으로 스키마를 강제해 섹션별 스토리보드를
받습니다. 모델 후보는 `PLAN_MODELS` 이며, 전부 실패하면 계정에서 실제로 쓸 수 있는
flash 계열을 조회해 이어서 시도합니다 (`_plan_model_candidates`).

## 업로드 한도

```python
MAX_CONTENT_LENGTH   = 24MB   # 참고컷 + 누끼컷
MAX_FORM_MEMORY_SIZE = 24MB   # ★ 12포즈 모드의 data URL 텍스트 필드용
```

두 번째 값을 안 올리면 12포즈 모드가 **항상** 413 으로 막힙니다.
Werkzeug 3.1부터 파일이 아닌 폼 필드 합계가 기본 500KB로 제한되기 때문입니다.

## 이미지 축소

폰 원본을 그대로 보내면 컷당 몇 초씩 낭비됩니다. 긴 변 **1536px** 이면 생성
품질에 충분하므로 그 이상은 줄여서 보냅니다 (`_load_shrunk`).
