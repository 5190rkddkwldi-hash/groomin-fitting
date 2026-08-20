# -*- coding: utf-8 -*-
"""웹 요청 흐름 검증 — 네트워크 없이 가짜 Gemini 로 돌린다.

`conftest.py` 의 `fake_gemini` 픽스처가 `genai.Client` 를 가로채므로
API 키 없이 로그인 게이트 · 업로드 검사 · 폴백 · 부분 실패까지 전부 확인된다.
"""
import io
import json

import pytest

import app as srv
from conftest import client_error, make_data_url, make_png, server_error


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


def test_로그인하면_두_페이지_모두_열린다(logged_in):
    assert logged_in.get("/").status_code == 200
    assert logged_in.get("/planner").status_code == 200


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


def test_포즈_모음은_장면_유지_템플릿을_쓴다(logged_in, fake_gemini):
    logged_in.post("/api/process", data=upload(mode="poseset", count="2"),
                   content_type="multipart/form-data")
    for prompt in fake_gemini["prompts"]:
        assert "FINAL CHECK" in prompt, "장면 유지 확인 문장이 빠졌다"
        assert "Edit the FIRST supplied photo" in prompt


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


# ---------------------------------------------------------------- 기획(planner)

def test_기획은_상품명과_특징이_필요하다(logged_in, fake_gemini):
    r = logged_in.post("/api/plan", json={"api_key": "k", "name": "", "features": ""})
    assert r.status_code == 400


def test_기획_요청에도_키가_필요하다(logged_in, fake_gemini):
    r = logged_in.post("/api/plan", json={"name": "반팔", "features": "면 100%"})
    assert r.status_code == 400
    assert "키" in r.get_json()["error"]


# ---------------------------------------------------------------- 업로드 한도

def test_너무_큰_요청은_친절한_413(logged_in):
    big = b"0" * (srv.app.config["MAX_CONTENT_LENGTH"] + 1024)
    r = logged_in.post("/api/process",
                       data=upload(image=(io.BytesIO(big), "big.png", "image/png")),
                       content_type="multipart/form-data")
    assert r.status_code == 413
    assert "error" in r.get_json()
