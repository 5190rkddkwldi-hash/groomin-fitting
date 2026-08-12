"""
브라우저에서 사진을 업로드하면, Google Gemini 3.1 Flash Image(나노바나나 2)를 이용해
배경 스타일을 분석하고 비슷한 분위기의 새 배경 이미지를 생성해주는 웹앱.

방문자가 자신의 Google AI Studio API 키를 매 요청마다 직접 입력한다. 서버는 그 키를
저장하지 않고 해당 요청 처리에만 사용한다.
"""

import base64
import io

from flask import Flask, request, jsonify, render_template
from PIL import Image
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB

ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_COUNT = 4
MODEL = "gemini-3.1-flash-image"

PROMPT = (
    "This photo shows a person wearing/holding a product in front of a "
    "background. Generate a NEW image that shows only that background: "
    "same color palette, lighting, mood, setting and props, in the same "
    "photographic style, but completely empty — no people, no products, "
    "nothing placed in the scene. Also briefly describe the background "
    "style in one or two English sentences before generating the image."
)


@app.route("/")
def index():
    return render_template("index.html", max_count=MAX_COUNT)


@app.route("/api/process", methods=["POST"])
def process():
    api_key = (request.form.get("api_key") or "").strip()
    if not api_key:
        return jsonify(error="Google AI Studio API 키를 입력해주세요."), 400

    image_file = request.files.get("image")
    if not image_file or not image_file.filename:
        return jsonify(error="이미지 파일을 선택해주세요."), 400
    if image_file.mimetype not in ALLOWED_CONTENT_TYPES:
        return jsonify(error="PNG, JPEG, WEBP 이미지만 지원합니다."), 400

    try:
        count = int(request.form.get("count", 3))
    except ValueError:
        count = 3
    count = max(1, min(count, MAX_COUNT))

    ref_image = Image.open(io.BytesIO(image_file.read()))

    client = genai.Client(api_key=api_key)

    style_prompt = None
    images = []

    try:
        for _ in range(count):
            response = client.models.generate_content(
                model=MODEL,
                contents=[PROMPT, ref_image],
                config=types.GenerateContentConfig(
                    response_modalities=[types.Modality.TEXT, types.Modality.IMAGE],
                ),
            )
            for part in response.parts:
                if part.text and not style_prompt:
                    style_prompt = part.text.strip()
                elif part.inline_data:
                    b64 = base64.b64encode(part.inline_data.data).decode()
                    images.append(f"data:image/png;base64,{b64}")
    except genai_errors.ClientError as e:
        status = 401 if e.code in (401, 403) else 400
        return jsonify(error=f"요청이 거부되었습니다: {e.message}"), status
    except genai_errors.APIError as e:
        return jsonify(error=f"Gemini 요청 중 오류가 발생했습니다: {e.message}"), 502

    if not images:
        return jsonify(error="이미지가 생성되지 않았습니다. 다시 시도해주세요."), 502

    return jsonify(prompt=style_prompt or "", images=images)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
