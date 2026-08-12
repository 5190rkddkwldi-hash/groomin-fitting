"""
브라우저에서 사진을 업로드하면, 배경 스타일을 분석해서 비슷한 분위기의
새 배경 이미지를 AI로 생성해주는 웹앱.

방문자가 자신의 OpenAI API 키를 매 요청마다 직접 입력한다. 서버는 그 키를
저장하지 않고 해당 요청 처리에만 사용한다.
"""

import base64

from flask import Flask, request, jsonify, render_template
from openai import OpenAI, AuthenticationError, OpenAIError

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB

ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_COUNT = 4
ALLOWED_SIZES = {"1024x1024", "1024x1536", "1536x1024"}

DESCRIBE_INSTRUCTION = (
    "This image shows a person wearing/holding a product in front of a "
    "background. Describe ONLY the background as a single concise English "
    "prompt suitable for an AI image generator, covering: color palette, "
    "lighting/mood, setting/props, and texture. Do not mention the person "
    "or the product at all. Output only the prompt text, nothing else."
)


@app.route("/")
def index():
    return render_template("index.html", max_count=MAX_COUNT)


@app.route("/api/process", methods=["POST"])
def process():
    api_key = (request.form.get("api_key") or "").strip()
    if not api_key:
        return jsonify(error="OpenAI API 키를 입력해주세요."), 400

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

    size = request.form.get("size", "1024x1024")
    if size not in ALLOWED_SIZES:
        size = "1024x1024"

    image_bytes = image_file.read()
    b64_input = base64.b64encode(image_bytes).decode()
    data_url = f"data:{image_file.mimetype};base64,{b64_input}"

    client = OpenAI(api_key=api_key)

    try:
        describe_resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": DESCRIBE_INSTRUCTION},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            max_tokens=200,
        )
        style_prompt = describe_resp.choices[0].message.content.strip()

        gen_prompt = (
            "An empty product-photography background, no people, no "
            f"products, nothing placed on it. Style: {style_prompt}"
        )
        gen_resp = client.images.generate(
            model="gpt-image-1",
            prompt=gen_prompt,
            size=size,
            n=count,
        )
    except AuthenticationError:
        return jsonify(error="API 키가 올바르지 않습니다."), 401
    except OpenAIError as e:
        return jsonify(error=f"OpenAI 요청 중 오류가 발생했습니다: {e}"), 502

    images = [
        f"data:image/png;base64,{item.b64_json}"
        for item in gen_resp.data
        if getattr(item, "b64_json", None)
    ]

    return jsonify(prompt=style_prompt, images=images)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
