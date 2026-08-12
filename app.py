"""
브라우저에서 모델 착용 사진을 업로드하면, Google Gemini 3.1 Flash Image(나노바나나 2)를
이용해 같은 인물/제품을 유지한 채로 배경과 포즈를 바꾼 새로운 사진 여러 장을
생성해주는 웹앱.

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

POSES = [
    "standing naturally with hands in pockets",
    "standing at a slight angle with arms crossed",
    "captured mid-step as if walking casually down the street",
    "leaning casually against a wall, railing, or storefront in the scene",
]

FRAMING = {
    "top": (
        "Frame the shot from just below the chin down to around the hips "
        "or upper thighs, focusing on the upper body so the top garment "
        "fills most of the frame."
    ),
    "bottom": (
        "Frame the shot from around the chest down to the shoes, focusing "
        "on the lower body so the full length and fit of the pants/bottom "
        "garment is clearly visible."
    ),
}

FACE_RULE = (
    "Absolutely do not show the person's face. Crop the frame so nothing "
    "above the chin or jawline is visible — exactly like Korean online "
    "clothing-store 'fitting cut' (착용컷) product photos, where the "
    "model's face is never shown."
)

BACKGROUNDS = {
    "auto": (
        "a trendy urban setting — pick something different each time, such "
        "as a cafe storefront, a textured concrete wall, retro signage, a "
        "sidewalk with street furniture, or a narrow alleyway"
    ),
    "cafe": (
        "in front of a stylish cafe storefront, with glass windows, wooden "
        "or metal frames, outdoor chairs and a sandwich board sign"
    ),
    "concrete": (
        "against a raw textured concrete or plaster wall with subtle "
        "stains, cracks and urban texture"
    ),
    "alley": (
        "in a narrow urban back alley with pipes, air-conditioning units, "
        "small signage and worn walls"
    ),
    "retro": (
        "on a retro shopping street with vintage Korean signage, old shop "
        "fronts and hand-painted lettering"
    ),
    "brick": (
        "against a red or beige brick wall with visible mortar lines and "
        "warm texture"
    ),
    "park": (
        "on a tree-lined path or in a park, with greenery, dappled "
        "sunlight and a paved walkway"
    ),
    "subway": (
        "near a subway entrance or station exit, with tiled walls, metal "
        "railings and urban signage"
    ),
    "minimal": (
        "in a minimal modern interior with clean white or beige walls, "
        "soft natural window light and simple furniture"
    ),
    "night": (
        "on a city street at night, lit by warm shop signs, neon glow and "
        "street lamps"
    ),
}

BACKGROUND_LABELS = [
    ("auto", "랜덤 (매번 다르게)"),
    ("cafe", "카페 앞"),
    ("concrete", "콘크리트 벽"),
    ("alley", "좁은 골목길"),
    ("retro", "레트로 간판 거리"),
    ("brick", "벽돌 벽"),
    ("park", "공원 / 나무길"),
    ("subway", "지하철 입구"),
    ("minimal", "미니멀 실내"),
    ("night", "밤거리 네온"),
]

BACKGROUND_RULE_TEMPLATE = (
    "Background style: candid, realistic Korean street-fashion 'fitting "
    "cut' photography, shot {setting}. Natural lighting, a slightly "
    "candid snapshot feel, NOT a plain studio backdrop."
)

PROMPT_TEMPLATE = (
    "Using the exact same person and the exact same {focus} shown in the "
    "reference photo, generate a new photorealistic image as if captured "
    "by a professional street-fashion photographer on location. "
    "{framing} {face_rule} {background_rule} Use a new, different "
    "real-world location from the reference photo, and change the pose "
    "to: {pose}. Keep the garment's color, fabric, fit and details "
    "clearly consistent and recognizable with the reference photo. "
    "Natural lighting, high-resolution editorial photography look. "
    "Before generating the image, briefly describe the new scene and "
    "pose in one English sentence."
)


@app.route("/")
def index():
    return render_template(
        "index.html", max_count=MAX_COUNT, backgrounds=BACKGROUND_LABELS
    )


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

    product_type = request.form.get("product_type", "top")
    if product_type not in FRAMING:
        product_type = "top"
    focus = "top garment" if product_type == "top" else "pants/bottom garment"

    background = request.form.get("background", "auto")
    custom_background = (request.form.get("custom_background") or "").strip()
    if custom_background:
        setting = f"in a location described as: {custom_background[:300]}"
    else:
        setting = BACKGROUNDS.get(background, BACKGROUNDS["auto"])
    background_rule = BACKGROUND_RULE_TEMPLATE.format(setting=setting)

    ref_image = Image.open(io.BytesIO(image_file.read()))

    client = genai.Client(api_key=api_key)

    results = []

    try:
        for i in range(count):
            pose = POSES[i % len(POSES)]
            prompt = PROMPT_TEMPLATE.format(
                focus=focus,
                framing=FRAMING[product_type],
                face_rule=FACE_RULE,
                background_rule=background_rule,
                pose=pose,
            )
            response = client.models.generate_content(
                model=MODEL,
                contents=[prompt, ref_image],
                config=types.GenerateContentConfig(
                    response_modalities=[types.Modality.TEXT, types.Modality.IMAGE],
                ),
            )
            caption = None
            image_data_url = None
            for part in response.parts:
                if part.text and not caption:
                    caption = part.text.strip()
                elif part.inline_data:
                    b64 = base64.b64encode(part.inline_data.data).decode()
                    image_data_url = f"data:image/png;base64,{b64}"
            if image_data_url:
                results.append({"image": image_data_url, "caption": caption or ""})
    except genai_errors.ClientError as e:
        status = 401 if e.code in (401, 403) else 400
        return jsonify(error=f"요청이 거부되었습니다: {e.message}"), status
    except genai_errors.APIError as e:
        return jsonify(error=f"Gemini 요청 중 오류가 발생했습니다: {e.message}"), 502

    if not results:
        return jsonify(error="이미지가 생성되지 않았습니다. 다시 시도해주세요."), 502

    return jsonify(results=results)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
