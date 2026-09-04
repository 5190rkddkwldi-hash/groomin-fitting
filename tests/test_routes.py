# -*- coding: utf-8 -*-
"""웹 요청 흐름 검증 — 네트워크 없이 가짜 Gemini 로 돌린다.

`conftest.py` 의 `fake_gemini` 픽스처가 `genai.Client` 를 가로채므로
API 키 없이 로그인 게이트 · 업로드 검사 · 폴백 · 부분 실패까지 전부 확인된다.
"""
import inspect
import io
import json
import time

import pytest
from PIL import Image

import app as srv
from conftest import (FakeResponse, client_error, make_data_url, make_png,
                      server_error)


def upload(**over):
    """기본 폼 데이터. 필요한 것만 덮어쓴다."""
    data = {
        "api_key": "테스트키",
        "mode": "quick",
        "count": "1",
        "product_type": "top",
        "background": "studio",
        "image": (io.BytesIO(make_png()), "cut.png", "image/png"),
    }
    data.update(over)
    return data


# ---------------------------------------------------------------- 로그인 게이트

def test_로그인_안_하면_첫_화면으로_보낸다(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_로그인_안_한_API는_401(client):
    r = client.post("/api/process", data=upload(),
                    content_type="multipart/form-data")
    assert r.status_code == 401
    assert "로그인" in r.get_json()["error"]


def test_틀린_코드는_입장_불가(client):
    r = client.post("/login", data={"code": "wrong", "shop": "가게"})
    assert r.status_code == 200
    assert "/" not in r.headers.get("Location", "")


def test_맞는_코드로_입장(client):
    r = client.post("/login", data={"code": srv.REFERRAL_CODE, "shop": "가게"})
    assert r.status_code == 302
    assert client.get("/").status_code == 200


def test_로그인하면_생성_화면이_열린다(logged_in):
    assert logged_in.get("/").status_code == 200


def test_없어진_기획_페이지는_404(logged_in):
    assert logged_in.get("/planner").status_code == 404
    assert logged_in.post("/api/plan", json={"name": "반팔"}).status_code == 404


def test_로그아웃(logged_in):
    logged_in.get("/logout")
    assert logged_in.get("/").status_code == 302


def test_로그인_화면과_정적_파일은_게이트를_통과한다(client):
    assert client.get("/login").status_code == 200


# ---------------------------------------------------------------- 입력 검사

def test_키_없으면_거부(logged_in, fake_gemini):
    r = logged_in.post("/api/process", data=upload(api_key=""),
                       content_type="multipart/form-data")
    assert r.status_code == 400
    assert "키" in r.get_json()["error"]


def test_사진_없으면_거부(logged_in, fake_gemini):
    data = upload()
    data.pop("image")
    r = logged_in.post("/api/process", data=data, content_type="multipart/form-data")
    assert r.status_code == 400


def test_지원하지_않는_형식_거부(logged_in, fake_gemini):
    r = logged_in.post("/api/process",
                       data=upload(image=(io.BytesIO(b"GIF89a"), "a.gif", "image/gif")),
                       content_type="multipart/form-data")
    assert r.status_code == 400
    assert "PNG" in r.get_json()["error"]


def test_깨진_이미지_거부(logged_in, fake_gemini):
    r = logged_in.post("/api/process",
                       data=upload(image=(io.BytesIO(b"not an image"), "a.png", "image/png")),
                       content_type="multipart/form-data")
    assert r.status_code == 400


def test_장수는_한도로_잘린다(logged_in, fake_gemini):
    logged_in.post("/api/process", data=upload(count="99"),
                   content_type="multipart/form-data")
    assert len(fake_gemini["prompts"]) == srv.QUICK_MAX


def test_이상한_장수는_기본값으로(logged_in, fake_gemini):
    r = logged_in.post("/api/process", data=upload(count="abc"),
                       content_type="multipart/form-data")
    assert r.status_code == 200


# ---------------------------------------------------------------- 생성 흐름

def test_한_장_생성(logged_in, fake_gemini):
    r = logged_in.post("/api/process", data=upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["results"]) == 1
    assert body["results"][0]["image"].startswith("data:image/png;base64,")


def test_빠른_생성_프롬프트에_핵심_규칙이_들어간다(logged_in, fake_gemini):
    logged_in.post("/api/process", data=upload(), content_type="multipart/form-data")
    prompt = fake_gemini["prompts"][0]
    assert srv.FACE_RULE[:40] in prompt, "얼굴 크롭 규칙이 빠졌다"
    assert "phone" in prompt.lower(), "폰카 스냅 리얼리즘이 빠졌다"


def test_알_수_없는_배경은_스튜디오로_대체(logged_in, fake_gemini):
    logged_in.post("/api/process", data=upload(background="없는배경"),
                   content_type="multipart/form-data")
    assert srv.BACKGROUNDS["studio"][:40] in fake_gemini["prompts"][0]


def test_랜덤_배경은_컷마다_다른_장소(logged_in, fake_gemini):
    logged_in.post("/api/process", data=upload(background="auto", count="4"),
                   content_type="multipart/form-data")
    assert len(fake_gemini["prompts"]) == 4
    assert len(set(fake_gemini["prompts"])) == 4, "컷마다 장면이 달라야 한다"


def test_직접_고른_프리셋도_컷마다_변주가_붙는다(logged_in, fake_gemini):
    seen = set()
    for i in range(3):
        fake_gemini["prompts"].clear()
        logged_in.post("/api/process",
                       data=upload(background="studio", count="1", index=str(i)),
                       content_type="multipart/form-data")
        seen.add(fake_gemini["prompts"][0])
    assert len(seen) == 3, "컷 번호가 다르면 변주 축도 달라져야 한다"


def test_형식이_깨진_키는_혼잡_안내가_아니라_키_안내(logged_in, fake_gemini):
    """구글은 잘못된 키에 401 이 아니라 400 API_KEY_INVALID 를 준다.
    이걸 폴백에 태우면 '서버가 혼잡합니다'라는 엉뚱한 안내가 나간다."""
    fake_gemini["behavior"] = lambda m, p: client_error(400, "API key not valid")
    r = logged_in.post("/api/process", data=upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 400
    assert "API 키" in r.get_json()["error"]
    assert "혼잡" not in r.get_json()["error"]
    assert len(fake_gemini["calls"]) == 1, "키가 틀렸는데 다른 모델까지 불렀다"


# ------------------------------------------------------------------ 1:1 비율

def test_모든_컷은_정사각으로_요청된다(logged_in, fake_gemini):
    """비율은 프롬프트가 아니라 API 파라미터로 못박아야 지켜진다.
    이게 빠지면 모델이 올린 사진(폰 3:4)의 비율을 그대로 따라간다."""
    logged_in.post("/api/process", data=upload(mode="quick", count="3"),
                   content_type="multipart/form-data")
    assert fake_gemini["calls"], "이미지 요청이 한 번도 안 나갔다"
    for call in fake_gemini["calls"]:
        assert call["config"].image_config.aspect_ratio == "1:1"


def test_포즈_모음도_정사각으로_요청된다(logged_in, fake_gemini):
    logged_in.post("/api/process",
                   data=upload(mode="poseset", reference=make_data_url(),
                               count="2"),
                   content_type="multipart/form-data")
    for call in fake_gemini["calls"]:
        assert call["config"].image_config.aspect_ratio == "1:1"


def test_AI_자동_코디_컷도_정사각으로_요청된다(logged_in, fake_gemini):
    """사용자가 실제로 겪은 경로 — 코디 문장을 실어 보내는 컷."""
    logged_in.post("/api/process",
                   data=upload(styling="auto", styling_desc="white tee and "
                               "charcoal wide trousers"),
                   content_type="multipart/form-data")
    call = fake_gemini["calls"][0]
    assert call["config"].image_config.aspect_ratio == "1:1"
    assert "SQUARE" in fake_gemini["prompts"][0], "구도 지시에도 정사각을 알려야 한다"


def test_비율_옵션을_모르는_모델이면_빼고_다시_시도한다(logged_in, fake_gemini):
    """옛 모델이 image_config 를 거부해도 컷은 나와야 한다."""
    calls = fake_gemini["calls"]

    def behavior(model, prompt):
        if calls and calls[-1]["config"].image_config is not None:
            return client_error(400, "Unknown name \"aspect_ratio\"")
        return "ok"

    fake_gemini["behavior"] = behavior
    r = logged_in.post("/api/process", data=upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    assert len(r.get_json()["results"]) == 1
    assert calls[-1]["config"].image_config is None
    assert calls[-1]["model"] == calls[0]["model"], "같은 모델로 다시 시도해야 한다"


def test_비율과_무관한_400은_비율을_빼고_삼키지_않는다(logged_in, fake_gemini):
    """안전필터 같은 400 까지 '비율 문제'로 오해하면 세로 컷이 조용히 돌아온다."""
    fake_gemini["behavior"] = lambda m, p: client_error(400, "안전 정책 위반")
    r = logged_in.post("/api/process", data=upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 502  # 후보를 다 돌고도 실패 → '혼잡' 안내
    assert len(fake_gemini["calls"]) == len(srv.IMAGE_MODELS), "비율만 빼고 더 불렀다"
    for call in fake_gemini["calls"]:
        assert call["config"].image_config is not None


def test_포즈_모음은_장면_유지_템플릿을_쓴다(logged_in, fake_gemini):
    logged_in.post("/api/process", data=upload(mode="poseset", count="2"),
                   content_type="multipart/form-data")
    for prompt in fake_gemini["prompts"]:
        assert "FINAL CHECK" in prompt, "장면 유지 확인 문장이 빠졌다"
        assert "Edit the FIRST supplied photo" in prompt


def test_포즈_모음은_올린_사진에_맞춰_얼굴을_처리한다(logged_in, fake_gemini):
    """얼굴 있는 사진이면 같은 얼굴 유지, 없는 사진이면 얼굴이 안 나오게 —
    두 경우를 모두 프롬프트가 담고 있어야 한다."""
    logged_in.post("/api/process", data=upload(mode="poseset", count="1"),
                   content_type="multipart/form-data")
    prompt = fake_gemini["prompts"][0]
    assert srv.FACE_ADAPTIVE_RULE[:40] in prompt
    assert "CASE A" in prompt and "CASE B" in prompt
    assert srv.FACE_RULE[:40] not in prompt, "포즈 모음에 무조건 크롭 규칙이 남아있다"


def test_빠른_생성은_언제나_목_아래_크롭(logged_in, fake_gemini):
    logged_in.post("/api/process", data=upload(mode="quick", count="1"),
                   content_type="multipart/form-data")
    prompt = fake_gemini["prompts"][0]
    assert srv.FACE_RULE[:40] in prompt
    assert "CASE A" not in prompt, "새 장면 모드에 얼굴 유지 분기가 새어 들어갔다"


def test_고른_컷을_이어받으면_자동으로_포즈_모음(logged_in, fake_gemini):
    """data URL 을 보내면 mode 와 무관하게 장면을 유지해야 한다."""
    data = upload(mode="quick", reference=make_data_url())
    data.pop("image")
    logged_in.post("/api/process", data=data, content_type="multipart/form-data")
    assert "FINAL CHECK" in fake_gemini["prompts"][0]


def test_이어받기_data_url이_깨졌으면_거부(logged_in, fake_gemini):
    data = upload(reference="data:image/png;base64,!!!깨짐!!!")
    data.pop("image")
    r = logged_in.post("/api/process", data=data, content_type="multipart/form-data")
    assert r.status_code == 400


def test_누끼컷을_넣으면_디테일_규칙이_붙는다(logged_in, fake_gemini):
    data = upload(detail_image=(io.BytesIO(make_png(40, 40)), "d.png", "image/png"))
    logged_in.post("/api/process", data=data, content_type="multipart/form-data")
    assert srv.DETAIL_RULE[:40] in fake_gemini["prompts"][0]


def test_누끼컷도_형식을_검사한다(logged_in, fake_gemini):
    data = upload(detail_image=(io.BytesIO(b"x"), "d.gif", "image/gif"))
    r = logged_in.post("/api/process", data=data, content_type="multipart/form-data")
    assert r.status_code == 400
    assert "누끼" in r.get_json()["error"]


def test_그대로_두기는_착장_잠금_문구를_보낸다(logged_in, fake_gemini):
    logged_in.post("/api/process", data=upload(styling="keep"),
                   content_type="multipart/form-data")
    assert srv.OUTFIT_KEEP_RULE[:40] in fake_gemini["prompts"][0]


def test_장신구_요청이_프롬프트에_실린다(logged_in, fake_gemini):
    logged_in.post("/api/process", data=upload(accessories="검정 볼캡"),
                   content_type="multipart/form-data")
    assert "검정 볼캡" in fake_gemini["prompts"][0]


# ---------------------------------------------------------------- 모델 폴백

def test_첫_모델이_죽으면_다음_모델로(logged_in, fake_gemini):
    def behavior(model, prompt):
        if model == srv.IMAGE_MODELS[0]:
            return server_error(503, "high demand")
        return "ok"

    fake_gemini["behavior"] = behavior
    r = logged_in.post("/api/process", data=upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    tried = [c["model"] for c in fake_gemini["calls"]]
    assert tried[0] == srv.IMAGE_MODELS[0]
    assert tried[1] == srv.IMAGE_MODELS[1]


def test_전부_죽으면_502로_안내(logged_in, fake_gemini):
    fake_gemini["behavior"] = lambda model, prompt: server_error(503, "high demand")
    r = logged_in.post("/api/process", data=upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 502
    assert "혼잡" in r.get_json()["error"]


def test_키_오류는_폴백하지_않고_바로_알린다(logged_in, fake_gemini):
    fake_gemini["behavior"] = lambda model, prompt: client_error(401, "API key not valid")
    r = logged_in.post("/api/process", data=upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 401
    assert len(fake_gemini["calls"]) == 1, "키 문제는 폴백해도 소용없다"


def test_성공한_모델을_기억해_다시_안_헤맨다(logged_in, fake_gemini):
    def behavior(model, prompt):
        if model == srv.IMAGE_MODELS[0]:
            return server_error(503, "high demand")
        return "ok"

    fake_gemini["behavior"] = behavior
    logged_in.post("/api/process", data=upload(count="3"),
                   content_type="multipart/form-data")
    tried = [c["model"] for c in fake_gemini["calls"]]
    # 첫 컷만 1번 후보를 시도하고, 이후에는 성공한 모델로 곧장 간다
    assert tried.count(srv.IMAGE_MODELS[0]) == 1


# ---------------------------------------------------------------- 부분 실패

def test_이미지_없이_텍스트만_오면_한_번_재시도(logged_in, fake_gemini):
    state = {"n": 0}

    def behavior(model, prompt):
        state["n"] += 1
        return "text_only" if state["n"] == 1 else "ok"

    fake_gemini["behavior"] = behavior
    r = logged_in.post("/api/process", data=upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    assert len(r.get_json()["results"]) == 1


def test_응답이_비어도_500이_나지_않는다(logged_in, fake_gemini):
    """안전필터에 걸리면 parts 가 None 이라 그냥 돌면 터진다."""
    fake_gemini["behavior"] = "empty"
    r = logged_in.post("/api/process", data=upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 502
    assert "error" in r.get_json()


def test_중간에_끊겨도_만든_컷은_돌려준다(logged_in, fake_gemini):
    state = {"n": 0}

    def behavior(model, prompt):
        state["n"] += 1
        if state["n"] <= 2:
            return "ok"
        return client_error(429, "quota exceeded")

    fake_gemini["behavior"] = behavior
    r = logged_in.post("/api/process", data=upload(count="4"),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["results"]) >= 1
    assert body["warning"]


# ---------------------------------------------------------------- 업로드 한도

def test_너무_큰_요청은_친절한_413(logged_in):
    big = b"0" * (srv.app.config["MAX_CONTENT_LENGTH"] + 1024)
    r = logged_in.post("/api/process",
                       data=upload(image=(io.BytesIO(big), "big.png", "image/png")),
                       content_type="multipart/form-data")
    assert r.status_code == 413
    assert "error" in r.get_json()


# ---------------------------------------------------------------- 기본 포즈

# 2026-09-01 사용자 요청: "기본 포즈(한쪽 주머니에 손)는 기본적으로 나오게".
# 1번 컷(index 0)은 두 모드 모두 무조건 기본 포즈로 나가야 한다.

def _프롬프트(fake_gemini):
    assert fake_gemini["prompts"], "프롬프트가 한 번도 안 나갔다"
    return fake_gemini["prompts"][-1]


def test_빠른생성_첫_컷은_기본_포즈다(logged_in, fake_gemini):
    r = logged_in.post("/api/process", data=upload(index="0"),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    assert srv.BASE_POSE in _프롬프트(fake_gemini)


def test_빠른생성_둘째_컷부터는_다른_포즈다(logged_in, fake_gemini):
    r = logged_in.post("/api/process", data=upload(index="1"),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    assert srv.BASE_POSE not in _프롬프트(fake_gemini)


def test_포즈모음_첫_컷도_기본_포즈다(logged_in, fake_gemini):
    r = logged_in.post("/api/process",
                       data=upload(mode="poseset", index="0",
                                   reference=make_data_url()),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    assert srv.BASE_POSE in _프롬프트(fake_gemini)


# ---------------------------------------------------------------- AI 자동 코디

CODI_JSON = (
    '{"product": "인디고 워시드 데님 셔츠, 오버핏",'
    ' "coordination": "화이트 티셔츠, 차콜 와이드 슬랙스, 화이트 레더 스니커즈",'
    ' "reason": "셔츠 색을 무채색으로 받쳐 색이 셋을 넘지 않게 했습니다.",'
    ' "styling_en": "a plain white heavy-cotton tee, wide charcoal pleated'
    ' trousers, and white leather low-top sneakers"}'
)


def 코디응답(text=CODI_JSON):
    return lambda model, prompt: FakeResponse([], text)


def codi_upload(**over):
    data = {
        "api_key": "테스트키",
        "product_type": "top",
        "image": (io.BytesIO(make_png()), "cut.png", "image/png"),
    }
    data.update(over)
    return data


def test_코디는_키가_있어야_한다(logged_in, fake_gemini):
    r = logged_in.post("/api/coordinate", data=codi_upload(api_key=""),
                       content_type="multipart/form-data")
    assert r.status_code == 400
    assert "키" in r.get_json()["error"]


def test_코디는_피팅컷이_있어야_한다(logged_in, fake_gemini):
    data = codi_upload()
    data.pop("image")
    r = logged_in.post("/api/coordinate", data=data,
                       content_type="multipart/form-data")
    assert r.status_code == 400


def test_코디도_로그인이_필요하다(client, fake_gemini):
    r = client.post("/api/coordinate", data=codi_upload(),
                    content_type="multipart/form-data")
    assert r.status_code == 401


def test_코디_결과를_돌려준다(logged_in, fake_gemini):
    fake_gemini["behavior"] = 코디응답()
    r = logged_in.post("/api/coordinate", data=codi_upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    c = r.get_json()["coordination"]
    assert c["styling_en"].startswith("a plain white")
    assert "차콜" in c["coordination"]
    assert c["product"] and c["reason"]


def test_코디_프롬프트에_인스타_문법이_들어간다(logged_in, fake_gemini):
    fake_gemini["behavior"] = 코디응답()
    logged_in.post("/api/coordinate", data=codi_upload(),
                   content_type="multipart/form-data")
    prompt = fake_gemini["prompts"][-1]
    assert "인기 인스타 피드" in prompt
    assert "색은 3색 이내" in prompt


def test_누끼컷을_주면_색_기준이_누끼라고_알려준다(logged_in, fake_gemini):
    fake_gemini["behavior"] = 코디응답()
    logged_in.post("/api/coordinate",
                   data=codi_upload(detail_image=(io.BytesIO(make_png()),
                                                  "nukki.png", "image/png")),
                   content_type="multipart/form-data")
    prompt = fake_gemini["prompts"][-1]
    assert "두 번째 사진" in prompt and "누끼컷" in prompt
    # 사진도 함께 실려 가야 한다 (프롬프트 1 + 피팅컷 1 + 누끼 1)
    assert len(fake_gemini["calls"][-1]["contents"]) == 3


def test_인스타_스크린샷을_주면_참고하라고_시킨다(logged_in, fake_gemini):
    fake_gemini["behavior"] = 코디응답()
    logged_in.post("/api/coordinate",
                   data=codi_upload(insta_image=(io.BytesIO(make_png()),
                                                 "feed.png", "image/png")),
                   content_type="multipart/form-data")
    prompt = fake_gemini["prompts"][-1]
    assert "인스타그램 피드 스크린샷" in prompt
    assert "그대로 베끼지는 말고" in prompt
    assert len(fake_gemini["calls"][-1]["contents"]) == 3


def test_코디가_JSON이_아니면_친절한_오류(logged_in, fake_gemini):
    fake_gemini["behavior"] = 코디응답("코디를 못 짜겠어요")
    r = logged_in.post("/api/coordinate", data=codi_upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 502
    assert "코디" in r.get_json()["error"]


def test_코디가_비면_친절한_오류(logged_in, fake_gemini):
    fake_gemini["behavior"] = 코디응답('{"product": "셔츠", "styling_en": ""}')
    r = logged_in.post("/api/coordinate", data=codi_upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 502


def test_코디도_은퇴한_모델은_건너뛴다(logged_in, fake_gemini):
    죽은모델 = srv.TEXT_MODELS[0]

    def behavior(model, prompt):
        if model == 죽은모델:
            return client_error(404, "model not found")
        return FakeResponse([], CODI_JSON)

    fake_gemini["behavior"] = behavior
    r = logged_in.post("/api/coordinate", data=codi_upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    assert r.get_json()["model"] != 죽은모델


def test_잘못된_키는_코디도_401(logged_in, fake_gemini):
    fake_gemini["behavior"] = lambda m, p: client_error(401, "API key not valid")
    r = logged_in.post("/api/coordinate", data=codi_upload(),
                       content_type="multipart/form-data")
    assert r.status_code == 401


def test_인스타_레퍼런스도_이미지만_받는다(logged_in, fake_gemini):
    r = logged_in.post("/api/coordinate",
                       data=codi_upload(insta_image=(io.BytesIO(b"x"),
                                                     "a.txt", "text/plain")),
                       content_type="multipart/form-data")
    assert r.status_code == 400


# ---- 짠 코디가 실제 생성 프롬프트에 실리는가

def test_AI코디_문장이_컷_프롬프트에_들어간다(logged_in, fake_gemini):
    desc = "a plain white tee, wide charcoal slacks, and white sneakers"
    r = logged_in.post("/api/process",
                       data=upload(styling="auto", styling_desc=desc),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    prompt = fake_gemini["prompts"][-1]
    assert desc in prompt
    assert srv.OUTFIT_KEEP_RULE not in prompt


def test_AI코디인데_코디문장이_없으면_원래_착장을_지킨다(logged_in, fake_gemini):
    """직접 API를 부르는 등으로 문장이 빠져도 옷을 지어내지 않아야 한다."""
    r = logged_in.post("/api/process", data=upload(styling="auto"),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    assert srv.OUTFIT_KEEP_RULE in fake_gemini["prompts"][-1]


def test_코디를_새로_짤_때는_누끼가_나머지_착장을_잠그지_않는다(logged_in, fake_gemini):
    """DETAIL_RULE 은 '나머지도 첫 사진 그대로'라 코디 지시와 충돌한다."""
    r = logged_in.post(
        "/api/process",
        data=upload(styling="auto", styling_desc="wide black slacks",
                    detail_image=(io.BytesIO(make_png()), "n.png", "image/png")),
        content_type="multipart/form-data")
    assert r.status_code == 200
    prompt = fake_gemini["prompts"][-1]
    assert srv.DETAIL_RULE_RESTYLE in prompt
    assert srv.DETAIL_RULE not in prompt


def test_코디가_그대로면_누끼가_나머지_착장까지_잠근다(logged_in, fake_gemini):
    r = logged_in.post(
        "/api/process",
        data=upload(styling="keep",
                    detail_image=(io.BytesIO(make_png()), "n.png", "image/png")),
        content_type="multipart/form-data")
    assert r.status_code == 200
    prompt = fake_gemini["prompts"][-1]
    assert srv.DETAIL_RULE in prompt
    assert srv.DETAIL_RULE_RESTYLE not in prompt


def test_코디부터_컷까지_한_흐름으로_이어진다(logged_in, fake_gemini):
    """화면이 하는 순서 그대로: 코디를 한 번 짜고 → 그 문장으로 컷 2장."""
    fake_gemini["behavior"] = 코디응답()
    r = logged_in.post("/api/coordinate",
                       data=codi_upload(detail_image=(io.BytesIO(make_png()),
                                                      "n.png", "image/png")),
                       content_type="multipart/form-data")
    desc = r.get_json()["coordination"]["styling_en"]

    fake_gemini["behavior"] = "ok"
    프롬프트들 = []
    for i in (0, 1):
        rr = logged_in.post(
            "/api/process",
            data=upload(styling="auto", styling_desc=desc, index=str(i),
                        detail_image=(io.BytesIO(make_png()), "n.png", "image/png")),
            content_type="multipart/form-data")
        assert rr.status_code == 200
        프롬프트들.append(fake_gemini["prompts"][-1])

    # 두 컷 모두 같은 코디, 포즈만 다름
    assert all(desc in pr for pr in 프롬프트들)
    assert srv.BASE_POSE in 프롬프트들[0]
    assert srv.BASE_POSE not in 프롬프트들[1]
    # 누끼는 상품 디테일만 잠그고 나머지 착장은 풀어준 판본이어야 한다
    assert all(srv.DETAIL_RULE_RESTYLE in pr for pr in 프롬프트들)


# ---------------------------------------------------------------- 어깨선 · 상의 입는 방식

# 2026-09-01 사용자 요청: 어깨 넓히기는 기본이 아니라 버튼으로.
# 기본은 올린 사진의 어깨를 그대로 따라간다.

def test_기본은_사진의_어깨를_그대로_따라간다(logged_in, fake_gemini):
    """코디 스타일과 무관하게, 아무것도 안 고르면 원본을 따른다."""
    for styling in ("keep", "street", "auto"):
        r = logged_in.post("/api/process",
                           data=upload(styling=styling, styling_desc="wide slacks"),
                           content_type="multipart/form-data")
        assert r.status_code == 200
        prompt = fake_gemini["prompts"][-1]
        assert srv.SHOULDERS["keep"]["rule"] in prompt
        assert srv.SHOULDERS["wide"]["rule"] not in prompt


def test_버튼을_켜야_어깨가_넓어진다(logged_in, fake_gemini):
    r = logged_in.post("/api/process", data=upload(shoulder="wide"),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    prompt = fake_gemini["prompts"][-1]
    assert srv.SHOULDERS["wide"]["rule"] in prompt
    assert srv.SHOULDERS["keep"]["rule"] not in prompt


def test_어깨_보정은_AI코디와_상관없이_쓸_수_있다(logged_in, fake_gemini):
    """예전엔 AI 자동 코디를 골라야만 어깨 규칙이 붙었다."""
    logged_in.post("/api/process", data=upload(styling="keep", shoulder="wide"),
                   content_type="multipart/form-data")
    assert srv.SHOULDERS["wide"]["rule"] in fake_gemini["prompts"][-1]


def test_이상한_어깨값은_사진_그대로로_떨어진다(logged_in, fake_gemini):
    r = logged_in.post("/api/process", data=upload(shoulder="아주넓게"),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    assert srv.SHOULDERS["keep"]["rule"] in fake_gemini["prompts"][-1]


def test_어깨_보정은_과하지_않게_지시한다():
    """세게 밀면 보디빌더가 되어 같은 사람으로 안 보인다."""
    rule = srv.SHOULDERS["wide"]["rule"].lower()
    assert "only a little" in rule
    assert "not extra muscle" in rule
    assert "posture only" in rule
    # 옷을 건드리지 않는다는 문구가 있어야 GARMENT_LOCK 과 부딪히지 않는다
    assert "cut, fit and length are unchanged" in rule


def test_사진_그대로는_원본을_따르라고만_말한다():
    rule = srv.SHOULDERS["keep"]["rule"].lower()
    assert "from the reference photo" in rule
    assert "rather than improving it" in rule


def test_어깨를_떨구라는_지시는_어디에도_없다():
    """이 문구가 남아 있으면 넓은 어깨 규칙과 정면으로 충돌한다."""
    조각 = [srv.POSE_STYLE_RULE, srv.BASE_POSE, *srv.POSES, *srv.STANDING_POSES]
    for t in 조각:
        low = t.lower()
        assert "shoulders dropped" not in low
        assert "shoulder dropped" not in low
        assert "slumped" not in low


def test_넣어서_입기와_빼서_입기가_프롬프트에_실린다(logged_in, fake_gemini):
    for key in ("in", "out"):
        r = logged_in.post("/api/process",
                           data=upload(styling="auto", styling_desc="wide slacks",
                                       tuck=key),
                           content_type="multipart/form-data")
        assert r.status_code == 200
        assert srv.TUCKS[key]["rule"] in fake_gemini["prompts"][-1]


def test_사진_그대로면_입는_방식_지시가_없다(logged_in, fake_gemini):
    logged_in.post("/api/process",
                   data=upload(styling="auto", styling_desc="wide slacks", tuck="keep"),
                   content_type="multipart/form-data")
    prompt = fake_gemini["prompts"][-1]
    assert "TUCK —" not in prompt


def test_이상한_입는_방식_값은_그대로로_떨어진다(logged_in, fake_gemini):
    r = logged_in.post("/api/process",
                       data=upload(styling="auto", styling_desc="x", tuck="장난"),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    assert "TUCK —" not in fake_gemini["prompts"][-1]


def test_코디를_짤_때도_입는_방식을_알려준다(logged_in, fake_gemini):
    fake_gemini["behavior"] = 코디응답()
    logged_in.post("/api/coordinate", data=codi_upload(tuck="in"),
                   content_type="multipart/form-data")
    prompt = fake_gemini["prompts"][-1]
    assert "넣어 입는다" in prompt and "벨트" in prompt


def test_포즈모음에는_어깨선도_입는방식도_끼어들지_않는다(logged_in, fake_gemini):
    """같은 장면 유지 모드에 스타일링 지시를 섞으면 사진이 어긋난다."""
    r = logged_in.post("/api/process",
                       data=upload(mode="poseset", reference=make_data_url(),
                                   styling="auto", tuck="in", shoulder="wide"),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    prompt = fake_gemini["prompts"][-1]
    assert srv.SHOULDERS["wide"]["rule"] not in prompt
    assert srv.SHOULDERS["keep"]["rule"] not in prompt
    assert "TUCK —" not in prompt


# ---------------------------------------------------------------- 참고 방식 · 컷별 변주

CODI_SCENE_JSON = (
    '{"product": "인디고 데님 셔츠", "coordination": "화이트 티, 차콜 슬랙스",'
    ' "reason": "무채색으로 받쳤습니다.",'
    ' "styling_en": "a white tee and charcoal slacks",'
    ' "scene_ko": "흰 벽과 원목 선반이 있는 편집샵 코너",'
    ' "scene_en": "in a warm select-shop corner with a white wall and an oak shelf"}'
)


def _인스타첨부(**over):
    data = codi_upload(insta_image=(io.BytesIO(make_png()), "feed.png", "image/png"))
    data.update(over)
    return data


def test_코디만_참고하면_배경은_받아오지_않는다(logged_in, fake_gemini):
    fake_gemini["behavior"] = 코디응답(CODI_SCENE_JSON)
    r = logged_in.post("/api/coordinate", data=_인스타첨부(ref_use="codi"),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    c = r.get_json()["coordination"]
    assert c["scene_en"] == "" and c["scene_ko"] == ""
    prompt = fake_gemini["prompts"][-1]
    assert "코디만" in prompt and "장소·배경은 참고하지 않는다" in prompt
    assert "scene_en" not in prompt          # 장소를 요구하지도 않는다


def test_배경만_참고하면_옷은_참고하지_말라고_시킨다(logged_in, fake_gemini):
    fake_gemini["behavior"] = 코디응답(CODI_SCENE_JSON)
    r = logged_in.post("/api/coordinate", data=_인스타첨부(ref_use="background"),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    c = r.get_json()["coordination"]
    assert c["scene_en"].startswith("in a warm select-shop")
    assert "편집샵" in c["scene_ko"]
    prompt = fake_gemini["prompts"][-1]
    assert "배경(촬영 장소)만" in prompt
    assert "나온 옷은 참고하지 않는다" in prompt
    assert "scene_en" in prompt


def test_둘_다_참고가_기본값이다(logged_in, fake_gemini):
    fake_gemini["behavior"] = 코디응답(CODI_SCENE_JSON)
    r = logged_in.post("/api/coordinate", data=_인스타첨부(),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    assert r.get_json()["coordination"]["scene_en"]
    assert "코디와 배경을 모두" in fake_gemini["prompts"][-1]


def test_참고사진이_없으면_배경도_참고방식도_없다(logged_in, fake_gemini):
    fake_gemini["behavior"] = 코디응답(CODI_SCENE_JSON)
    r = logged_in.post("/api/coordinate", data=codi_upload(ref_use="background"),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    assert r.get_json()["coordination"]["scene_en"] == ""


def test_이상한_참고방식은_둘다로_떨어진다(logged_in, fake_gemini):
    fake_gemini["behavior"] = 코디응답(CODI_SCENE_JSON)
    r = logged_in.post("/api/coordinate", data=_인스타첨부(ref_use="아무거나"),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    assert "코디와 배경을 모두" in fake_gemini["prompts"][-1]


def test_참고배경이_배경프리셋을_밀어낸다(logged_in, fake_gemini):
    scene = "in a warm select-shop corner with a white wall"
    r = logged_in.post("/api/process",
                       data=upload(styling="auto", styling_desc="a white tee",
                                   scene_desc=scene, background="lawn_park"),
                       content_type="multipart/form-data")
    assert r.status_code == 200
    prompt = fake_gemini["prompts"][-1]
    assert scene in prompt
    assert srv.BACKGROUNDS["lawn_park"] not in prompt
    assert srv.REF_SCENE_NOTE in prompt


def test_참고배경으로_여러장_뽑으면_컷마다_달라진다(logged_in, fake_gemini):
    scene = "in a warm select-shop corner with a white wall"
    프롬프트들 = []
    for i in range(4):
        logged_in.post("/api/process",
                       data=upload(styling="auto", styling_desc="a white tee",
                                   scene_desc=scene, index=str(i)),
                       content_type="multipart/form-data")
        프롬프트들.append(fake_gemini["prompts"][-1])
    # 장소는 같지만 배경 디테일·구도·포즈 축이 컷마다 달라야 한다
    assert all(scene in pr for pr in 프롬프트들)
    assert len(set(프롬프트들)) == 4
    for i in range(1, 4):
        assert srv.SCENE_VARIETY[i] in 프롬프트들[i]
        assert srv.SHOT_VARIETY[i] in 프롬프트들[i]
    assert srv.POSES[0] in 프롬프트들[0]
    assert srv.POSES[1] in 프롬프트들[1]


def test_구도_변주는_AI코디에서만_붙는다(logged_in, fake_gemini):
    logged_in.post("/api/process", data=upload(styling="street", index="1"),
                   content_type="multipart/form-data")
    assert srv.SHOT_VARIETY[1] not in fake_gemini["prompts"][-1]


def test_구도_변주는_프레이밍을_뒤엎지_않는다():
    """{framing} 안에서 움직이는 '작은 차이'여야 한다."""
    금지 = ("full body", "close-up of the face", "portrait of the face",
          "wide angle", "aerial", "from behind")
    for v in srv.SHOT_VARIETY[1:]:
        low = v.lower()
        assert v.startswith("For THIS cut,")
        assert not any(w in low for w in 금지), v


def test_참고배경일_때는_상의_무드규칙을_얹지_않는다(logged_in, fake_gemini):
    """장소가 이미 정해졌는데 '사선 벽면' 무드를 덧대면 서로 부딪힌다."""
    logged_in.post("/api/process",
                   data=upload(styling="auto", styling_desc="a white tee",
                               product_type="top",
                               scene_desc="in a select-shop corner"),
                   content_type="multipart/form-data")
    assert srv.TOP_MOOD_RULE not in fake_gemini["prompts"][-1]


# ---------------------------------------------------------------- 업로드 제거 버튼

# 2026-09-01 사용자 신고: 누끼컷의 [✕ 제거]가 눌러도 아무 일도 안 함.
# 원인은 버튼이 없어서가 아니라 wireDrop 에 그 버튼을 안 넘겨서였다
# (버튼은 HTML 에 있고 CSS 로 보이기까지 해서 더 헷갈렸다).
# 앞으로 업로드 칸을 추가할 때 같은 실수를 하지 않도록 렌더된 화면으로 확인한다.

def _화면(logged_in):
    return logged_in.get("/").get_data(as_text=True)


def test_모든_제거버튼이_실제로_연결돼_있다(logged_in):
    import re

    html = _화면(logged_in)
    버튼들 = re.findall(r'class="drop-clear" id="([A-Za-z0-9_]+)"', html)
    assert len(버튼들) >= 3, "제거 버튼을 찾지 못했다: %s" % 버튼들
    연결된것 = re.findall(r'getElementById\("([A-Za-z0-9_]+)"\)\s*\)', html)
    빠진것 = [b for b in 버튼들 if ('getElementById("%s")' % b) not in html]
    assert 빠진것 == [], "wireDrop 에 안 넘긴 제거 버튼: %s" % 빠진것

    # wireDrop 호출마다 인자가 5개인지 (5번째가 제거 버튼)
    호출들 = re.findall(r"wireDrop\((.*?)\);", html, re.S)
    assert len(호출들) >= 3
    for 호출 in 호출들:
        assert "clear" in 호출.lower(), "제거 버튼 없이 부른 wireDrop: %s" % 호출[:80]


def test_사진을_지우면_바뀐_걸_다른_곳에도_알린다(logged_in):
    """input.value 를 코드로 비우면 change 이벤트가 안 난다 —

    그러면 AI 코디 무효화·참고 방식 숨김이 안 돌아간다.
    """
    html = _화면(logged_in)
    assert 'inputEl.dispatchEvent(new Event("change"' in html


# ---------------------------------------------------------------------------
# 배포판 전용 사고: '서버 오류가 발생했습니다 (502)' (2026-09-03)
#
# 로컬(램 16GB·루프백 업로드)에서는 절대 재현되지 않고, 무료 인스턴스
# (512MB·0.1 CPU 한 대를 여러 사람이 공유, 앞에 100초짜리 엣지 타임아웃)
# 에서만 터지던 것들. 아래 테스트들이 그 조건을 대신 지킨다.
# ---------------------------------------------------------------------------

def test_큰_JPEG은_펼치기_전에_줄여서_읽는다(monkeypatch):
    """draft() 없이 load() 하면 5천만 화소가 메모리에서 143MB로 펼쳐진다.

    무료 인스턴스에서 컷을 동시에 만들면 그것만으로 한도를 넘겨 워커가 죽고,
    화면에는 앱의 한국어 오류가 아니라 '서버 오류 (502)'만 뜬다.
    """
    buf = io.BytesIO()
    Image.new("RGB", (4096, 3072), (200, 180, 160)).save(buf, "JPEG", quality=60)

    본_크기 = []
    원래_썸네일 = Image.Image.thumbnail

    def 엿보기(self, size, *a, **kw):
        본_크기.append(self.size)  # thumbnail 을 부르는 시점 = 디코드된 크기
        return 원래_썸네일(self, size, *a, **kw)

    monkeypatch.setattr(Image.Image, "thumbnail", 엿보기)
    img = srv._load_shrunk(buf.getvalue(), 1536)

    assert max(img.size) <= 1536
    assert 본_크기, "thumbnail 이 불리지 않았다"
    assert max(본_크기[0]) <= 2048, (
        "draft 가 안 먹었다 — 원본 그대로(%s) 펼쳐졌다" % (본_크기[0],)
    )


def test_PNG는_draft가_없어도_멀쩡히_읽힌다():
    """draft() 는 JPEG 전용이라 PNG 에서는 아무 일도 하면 안 된다."""
    img = srv._load_shrunk(make_png(2000, 3000), 1536)
    assert max(img.size) <= 1536


def test_시간이_다_되면_다음_후보를_붙잡지_않는다(fake_gemini):
    """후보를 75초씩 줄줄이 기다리면 엣지(100초)에 잘려 502가 된다."""
    fake_gemini["behavior"] = lambda m, p: "ok"
    with pytest.raises(srv.ImageModelUnavailable):
        srv._generate_image_with_fallback(
            srv.genai.Client(api_key="x"), "프롬프트", [],
            deadline=time.monotonic() - 1,
        )
    assert fake_gemini["calls"] == [], "시간이 없는데도 모델을 불렀다"


def test_시간이_남으면_평소대로_생성한다(fake_gemini):
    fake_gemini["behavior"] = lambda m, p: "ok"
    resp = srv._generate_image_with_fallback(
        srv.genai.Client(api_key="x"), "프롬프트", [],
        deadline=time.monotonic() + srv.REQUEST_BUDGET_S,
    )
    assert resp.parts
    assert len(fake_gemini["calls"]) == 1


def test_컷_생성이_시간을_넘기면_정직한_안내를_준다(logged_in, fake_gemini, monkeypatch):
    monkeypatch.setattr(srv, "REQUEST_BUDGET_S", 0)
    fake_gemini["behavior"] = lambda m, p: "ok"
    r = logged_in.post("/api/process", data=upload(), content_type="multipart/form-data")
    assert r.status_code == 502
    assert "혼잡" in r.get_json()["error"]
    assert fake_gemini["calls"] == []


def test_코디가_시간을_넘기면_정직한_안내를_준다(logged_in, fake_gemini, monkeypatch):
    monkeypatch.setattr(srv, "REQUEST_BUDGET_S", 0)
    r = logged_in.post(
        "/api/coordinate",
        data={
            "api_key": "k",
            "image": (io.BytesIO(make_png()), "cut.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 502
    assert "시간" in r.get_json()["error"]


def test_예기치_못한_500도_JSON으로_온다():
    """기본 500 은 HTML 이라 화면이 못 읽고 '서버 오류 (500)' 로만 보인다."""
    with srv.app.test_request_context("/api/process"):
        body, code = srv.too_slow_or_broken(Exception("boom"))
        assert code == 500
        assert "오류" in body.get_json()["error"]


def test_코디_호출에도_타임아웃이_걸려_있다():
    """상한이 없으면 느린 비전 호출이 엣지에 잘려 502 로 보인다."""
    코디 = inspect.getsource(srv.coordinate)
    assert "COORD_TIMEOUT_MS" in 코디
    assert srv.COORD_TIMEOUT_MS < srv.REQUEST_BUDGET_S * 1000


def test_컷마다_원본_사진을_다시_올리지_않는다(logged_in):
    """컷 10장이면 원본을 10번 올리고 서버가 10번 펼쳤다 — 느림의 정체."""
    html = _화면(logged_in)
    assert 'fd.append("image", mainFile)' not in html, "원본을 그대로 올리고 있다"
    assert 'fd.append("detail_image", detailFile)' not in html
    assert "shrinkPhoto" in html and "appendPhoto" in html
    # 한 번 줄인 것을 캐시해 두고 모든 컷이 같이 써야 의미가 있다
    assert "shrunkCache" in html


def test_누끼컷의_투명한_배경을_까맣게_만들지_않는다(logged_in):
    """투명 PNG 를 JPEG 로 바꾸면 배경이 새까매져 상품 판독이 망가진다."""
    html = _화면(logged_in)
    assert "hasAlpha" in html
    assert 'hasAlpha(ctx, canvas) ? "image/png"' in html
