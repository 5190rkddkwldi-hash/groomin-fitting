# -*- coding: utf-8 -*-
"""테스트 공용 준비물.

이 프로젝트는 Gemini API 를 호출하지만, 테스트는 **네트워크를 쓰지 않는다.**
`fake_gemini` 픽스처가 `genai.Client` 를 가로채서 원하는 대로 응답하게 만든다.
덕분에 API 키 없이도 전체 흐름을 검증할 수 있다.
"""
import base64
import io
import os
import sys
import types as pytypes

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as srv  # noqa: E402
from google.genai import errors as genai_errors  # noqa: E402


# ---------------------------------------------------------------- 이미지 도우미

def make_png(w=64, h=96, color=(120, 140, 200)):
    """업로드용 작은 PNG 바이트."""
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def make_data_url(w=64, h=96):
    return "data:image/png;base64," + base64.b64encode(make_png(w, h)).decode()


# ---------------------------------------------------------------- 가짜 Gemini

class FakeInline:
    def __init__(self, data):
        self.data = data


class FakePart:
    def __init__(self, data=None):
        self.inline_data = FakeInline(data) if data is not None else None


class FakeResponse:
    """parts 가 None 인 경우(안전필터 등)도 재현할 수 있다."""

    def __init__(self, parts, text=""):
        self.parts = parts
        self.text = text


class FakeModels:
    def __init__(self, recorder):
        self.rec = recorder

    def generate_content(self, model=None, contents=None, config=None):
        self.rec["calls"].append({"model": model, "contents": contents, "config": config})
        prompt = contents[0] if contents else ""
        self.rec["prompts"].append(prompt)
        behavior = self.rec["behavior"]
        outcome = behavior(model, prompt) if callable(behavior) else behavior
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "empty":
            return FakeResponse(None, "이미지를 만들지 못했습니다")
        if outcome == "text_only":
            return FakeResponse([FakePart(None)], "설명만 왔음")
        return FakeResponse([FakePart(make_png(8, 8))])

    def list(self):
        return [pytypes.SimpleNamespace(name="models/gemini-3.6-flash")]


class FakeClient:
    def __init__(self, recorder, **kwargs):
        self.rec = recorder
        self.rec["client_kwargs"].append(kwargs)
        self.models = FakeModels(recorder)


@pytest.fixture
def fake_gemini(monkeypatch):
    """genai.Client 를 가로채는 픽스처.

    사용법:
        fake_gemini["behavior"] = "ok"                       # 항상 이미지 1장
        fake_gemini["behavior"] = lambda model, prompt: ...  # 모델별로 다르게
        fake_gemini["prompts"]                               # 실제로 보낸 프롬프트들
    """
    rec = {"calls": [], "prompts": [], "client_kwargs": [], "behavior": "ok"}

    def factory(*args, **kwargs):
        return FakeClient(rec, **kwargs)

    monkeypatch.setattr(srv.genai, "Client", factory)
    # 성공한 모델을 기억하는 전역 캐시는 테스트끼리 섞이면 안 된다
    monkeypatch.setattr(srv, "_image_model_pick", {"name": None})
    return rec


# ---------------------------------------------------------------- Flask

@pytest.fixture
def client():
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        yield c


@pytest.fixture
def logged_in(client):
    """추천인 코드로 입장한 세션."""
    client.post("/login", data={"code": srv.REFERRAL_CODE, "shop": "테스트샵"})
    return client


# ---------------------------------------------------------------- 오류 만들기

def client_error(code, message):
    return genai_errors.ClientError(code, {"error": {"message": message}})


def server_error(code, message):
    return genai_errors.ServerError(code, {"error": {"message": message}})
