# 기여 안내

읽어주셔서 고맙습니다. 이 문서는 **처음 오신 분이 30분 안에 코드를 고치고
검증까지 할 수 있게** 하는 것이 목표입니다.

특히 [프롬프트를 고칠 때](#프롬프트를-고칠-때) 항목은 꼭 읽어주세요.
여기 적힌 규칙은 대부분 **실제로 결과물이 망가진 뒤에 알아낸 것들**입니다.

---

## 1. 개발 환경 만들기

파이썬 3.11 이상이면 됩니다.

```bash
git clone https://github.com/5190rkddkwldi-hash/groomin-fitting.git
cd groomin-fitting

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt -r requirements-dev.txt
```

Windows는 `dev.bat`, macOS/Linux는 `./dev.sh` 를 실행하면 위 과정을 한 번에 합니다.

## 2. 실행

```bash
python app.py          # http://127.0.0.1:5000
```

첫 화면에서 **추천인 코드**(기본 `grooming2026`)와 상호명을 넣으면 들어갑니다.
실제로 이미지를 만들려면 [Google AI Studio](https://aistudio.google.com/apikey)
API 키가 필요합니다. 키는 화면에서 입력하며 **서버에 저장되지 않습니다.**

설정을 바꾸려면 `.env.example` 을 복사해서 쓰세요.

```bash
cp .env.example .env
```

## 3. 테스트 — API 키 없이 전부 돌아갑니다

```bash
pytest
```

Gemini 호출은 `tests/conftest.py` 의 `fake_gemini` 픽스처가 가로챕니다.
**네트워크도, API 키도, 요금도 들지 않습니다.**

| 파일 | 무엇을 지키는가 |
| --- | --- |
| `tests/test_prompts.py` | 프롬프트·프리셋이 지켜야 할 규칙 (과거에 사고 났던 지점들) |
| `tests/test_routes.py` | 로그인 게이트, 업로드 검사, 모델 폴백, 부분 실패 처리 |

PR 전에 `pytest` 가 초록불인지 확인해 주세요. GitHub Actions 에서도 자동으로 돕니다.

프롬프트를 손봤다면 **테스트만으로는 부족합니다.** 실제로 몇 컷 뽑아서 눈으로
확인한 뒤, PR 에 before/after 이미지를 붙여주세요.

---

## 4. 저장소 구조

```
app.py                  전부 여기에 있습니다 (약 1,400줄)
  ├─ 설정·상수          모델 후보, 업로드 한도, 로그인 코드
  ├─ 프롬프트 재료      FACE_RULE, GARMENT_LOCK_RULE, BACKGROUNDS, POSES ...
  ├─ 프롬프트 템플릿    PROMPT_NEW_SCENE / PROMPT_SAME_SCENE
  ├─ 기획(planner)      PLAN_PROMPT, PLAN_STRATEGIES, PLAN_TONES
  └─ 라우트             /login /  /planner  /api/process  /api/plan
templates/              index(생성) · planner(기획) · login
tests/                  네트워크 없는 검증
render.yaml             Render 배포 설정
```

자세한 흐름은 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 를 보세요.

---

## 5. 프롬프트를 고칠 때

이 프로젝트에서 가장 자주 망가지는 곳입니다. 아래는 전부 **실제 사고 기록**입니다.

### 5-1. 부정문으로 막지 마세요. 원하는 것을 긍정문으로 쓰세요

이미지 모델은 "~하지 마라"에 나온 단어를 오히려 그립니다.

| 하지 말 것 | 대신 |
| --- | --- |
| `never an empty white void` | `freshly painted clean white walls` |
| `completely free of form-tie holes` | `smooth, evenly finished, seamless concrete` |
| `surfaces are mostly plain` (억제) | 원하는 분위기를 직접 묘사 |

> `never an empty white void` 를 넣었더니 모델이 **흰 벽 자체를 피해서**
> 갤러리 배경이 빈티지 폐건물로 튀었습니다.
> `surfaces are mostly plain` 을 전역 규칙으로 넣었더니 **모든 배경이
> 휑한 빈 공간으로 수렴**했습니다.

### 5-2. 어떤 단어는 그 자체로 아티팩트를 부릅니다

- `raw concrete` / `poured concrete` → 거푸집 O자 구멍이 규칙적인 점 무늬로 깔립니다
- `form-tie holes` → 금지문으로 써도 그려집니다
- `tactile paving` (점자블록) → 노란 점 패턴이 화면 전체로 번집니다

`tests/test_prompts.py` 의 `BANNED` 목록이 이 단어들을 막습니다.
새로 발견하면 그 목록에 추가해 주세요.

### 5-3. 소품 개수를 지시하지 마세요

`소품 1~2개를 넣어라` 라고 했더니 모델 옆에 화분과 스툴이 뜬금없이 놓였습니다.
**"편집샵 룩북처럼 연출된 코너"** 로 묘사하고, 가구는 원래 있을 자리에
통합되게 두세요. 휑함의 반대는 소품이 아니라 **온기(따뜻한 톤 + 자연광)** 입니다.

### 5-4. 같은 장면 모드(`PROMPT_SAME_SCENE`)에 구도·스타일링 지시를 섞지 마세요

12포즈 모드는 "보낸 사진 그대로, 포즈만 바꾸기"입니다.
여기에 새 장면용 구도(`{framing}`, 예: "벽을 사선으로")나 장신구 지시가 들어가면
**모델이 배경을 새로 만들어 버립니다.** 실제로 났던 버그입니다.

이 규칙은 `test_같은_장면_모드에는_구도_지시를_넣지_않는다` 가 지킵니다.

### 5-5. 절대 바꾸지 말아야 할 것

- **얼굴 노출 금지** (`FACE_RULE`) — 목 아래 크롭은 한국 쇼핑몰 착용컷의 관행입니다
- **상품 잠금** (`GARMENT_LOCK_RULE`) — 판매 상품의 색·프린트·핏이 컷마다 달라지면 안 됩니다
- **화질 우선** — 혼잡하다고 `lite` 같은 하위 모델로 몰래 내려가지 마세요.
  차라리 "잠시 후 다시 시도" 라는 정직한 오류를 보여줍니다

### 5-6. 배경 취향

이 프로젝트가 지향하는 배경은 **소품이 자연스럽게 놓인 꾸며진 공간**입니다.

- 좋아하는 것 — 스튜디오 선반+램프+화분, 우드 사이드보드+액자+매거진, 쇼룸 코너
- 싫어하는 것 — 텅 빈 흰 방, 거울샷, 생활감 있는 침실, 황량한 옥상, 네온·간판이 복잡한 거리
- 랜덤 풀(`RANDOM_POOL`)은 **한 브랜드가 찍은 한 세트**처럼 무드가 통일돼야 합니다.
  뜬금없는 장소(예: 차도)는 `BACKGROUNDS` 에는 두되 랜덤 풀에서는 빼세요

### 5-7. 배경 프리셋을 새로 추가하려면

1. `BACKGROUNDS` 에 `키: "영문 묘사"` 추가 — 장소·표면·빛·모델의 위치까지 씁니다
2. `BACKGROUND_GROUPS` 의 알맞은 그룹에 `(키, "한글 라벨")` 추가
   — **여기 안 넣으면 화면에서 고를 수 없습니다** (테스트가 잡아줍니다)
3. 랜덤에도 넣을지 판단해 `RANDOM_POOL` 에 추가
4. `pytest` 로 조립 검사 → 실제로 3~4컷 뽑아 눈으로 확인

---

## 6. 자주 밟는 함정

**폼 필드 500KB 제한**
12포즈 모드는 고른 컷을 파일이 아니라 data URL **텍스트 필드**로 보냅니다.
Werkzeug 3.1부터 파일이 아닌 폼 필드 합계가 기본 500KB로 제한돼 항상 413이 납니다.
`app.config["MAX_FORM_MEMORY_SIZE"]` 를 업로드 한도와 같게 유지하세요.

**응답에 이미지가 없을 수 있습니다**
안전필터에 걸리면 `response.parts` 가 `None` 입니다. 그냥 순회하면 500이 납니다.
`response.parts or []` 로 방어하고, 한 번은 재시도하세요.

**모델은 은퇴합니다**
텍스트·이미지 모델 모두 후보 목록(`PLAN_MODELS`, `IMAGE_MODELS`)을 두고
404/`no longer available` 이면 다음 후보로 넘어갑니다. 새 모델이 나오면
목록 앞에 추가하세요.

---

## 7. 배포

`master` 에 머지되면 [Render](https://render.com) 가 자동 배포합니다.

> ⚠️ **2026-08-20 확인:** 자동 배포가 조용히 멈춰 있던 적이 있습니다.
> 설정은 `autoDeploy: yes` 인데 GitHub 이벤트가 Render 에 들어오지 않아
> 커밋 9개가 한 달 가까이 반영되지 않았습니다.
> **머지 후에는 실제 사이트가 바뀌었는지 꼭 확인하세요.**

배포 확인 방법 — 로그인한 뒤 첫 화면 HTML 의 `const RANDOM_POOL = [...]` 이
로컬 `app.RANDOM_POOL` 과 같은지 비교하면 됩니다.

---

## 8. PR 체크리스트

- [ ] `pytest` 초록불
- [ ] 프롬프트를 고쳤다면 실제 생성 컷 before/after 첨부
- [ ] 배경 프리셋을 추가했다면 `BACKGROUND_GROUPS` 에도 넣었는가
- [ ] 새로 알아낸 "이 단어는 이런 아티팩트를 부른다"가 있다면
      `BANNED` 목록과 이 문서에 남겼는가
- [ ] 커밋 메시지는 무엇을 왜 바꿨는지 한 줄로

## 9. 코드 스타일

- 들여쓰기 4칸, 한 줄 100자 이내
- **주석은 한국어로, "무엇"이 아니라 "왜"를 씁니다.**
  이 저장소의 주석 상당수가 과거 버그의 재발 방지 메모입니다. 지우지 마세요
- 새 의존성은 되도록 늘리지 않습니다 (Render 무료 플랜 빌드 시간)
