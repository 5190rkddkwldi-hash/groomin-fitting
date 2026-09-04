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
| 포즈 목록 | `POSES` (0번 = `BASE_POSE`) | `STANDING_POSES` (0번 = `BASE_POSE`) |
| 최대 장수 | `QUICK_MAX` (10) | `POSESET_MAX` (13 = 기본 포즈 1 + 변주 12) |
| 사진 전달 | 파일 업로드 | 앞서 만든 컷의 data URL |

### AI 자동 코디 (`/api/coordinate`)

코디 스타일에서 **★ AI 자동 코디**를 고르면, 화면은 컷을 만들기 **전에 이 엔드포인트를
딱 한 번** 부릅니다. 텍스트(비전) 모델이 피팅컷·누끼컷·인스타 스크린샷을 읽고
`styling_en`(영어 명사구)을 지어내면, 화면이 그 문장을 모든 컷 요청에 `styling_desc`로
똑같이 실어 보냅니다. 컷마다 물어보면 10장이 전부 다른 옷을 입기 때문입니다.

참고 스크린샷은 `ref_use`로 쓰임새를 고릅니다 — `codi`(코디만) / `background`(배경만) /
`both`(둘 다, 기본). 배경까지 참고하면 응답에 `scene_en`이 실리고, 화면이 그걸
`scene_desc`로 넘겨 **배경 프리셋을 밀어냅니다**(`BACKGROUND_RULE_TEMPLATE`의
`{setting}` 자리에 그대로 꽂힙니다).

여러 장을 뽑을 때 컷마다 달라지는 축은 셋입니다 — 포즈(`POSES[index]`),
배경 디테일(`SCENE_VARIETY[index]`), 구도(`SHOT_VARIETY[index]`, AI 코디에서만).

주의: 코디를 새로 짤 때는 누끼 규칙이 `DETAIL_RULE`이 아니라
`DETAIL_RULE_RESTYLE`로 바뀝니다. `DETAIL_RULE`은 "나머지 착장도 첫 사진 그대로"라고
못박기 때문에 코디 지시와 정면으로 충돌합니다.

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

## 출력 비율 (1:1)

모든 컷은 정사각으로 나옵니다. 이건 프롬프트로 부탁해서 되는 일이 아니라
API 파라미터로 못박아야 합니다 — `_image_gen_config()` 가
`image_config=ImageConfig(aspect_ratio=IMAGE_ASPECT_RATIO)` 를 붙입니다.
아무 말도 안 하면 이미지 모델은 **받은 참고 사진의 비율을 그대로 따라가서**
폰 사진(3:4)을 넣으면 세로 컷만 나옵니다.

구도 지시(`SQUARE_FRAME_RULE`)도 새 장면 프롬프트에만 함께 들어갑니다 —
비율만 바꾸고 구도를 안 알려주면 상품이 프레임 밖으로 밀립니다.
장면 유지(12포즈) 프롬프트에는 넣지 않습니다(구도 지시가 섞이면 배경이 바뀜).

모델이 `image_config` 자체를 거부하는 400 이면(`_is_ratio_option_error`)
같은 모델로 비율 없이 한 번만 더 시도합니다. 안전필터 같은 다른 400 은
그대로 다음 후보 모델로 넘깁니다.

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
