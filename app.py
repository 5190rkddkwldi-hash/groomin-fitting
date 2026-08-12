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
    "standing relaxed and upright, weight on one leg, hands resting "
    "naturally at the sides or lightly in pockets",
    "standing at a slight three-quarter angle, one hand in a pocket, "
    "shoulders relaxed",
    "captured mid-step, walking calmly and unhurried",
    "standing still and leaning lightly against a wall, column or railing "
    "in the scene",
]

# 카테고리별 프레이밍 — 상품이 화면에서 주인공이 되도록 컷을 다르게 잡는다.
PRODUCTS = {
    "top": {
        "label": "상의",
        "focus": "top garment",
        "framing": (
            "Frame from just below the chin down to the upper thighs so "
            "the top garment fills most of the frame; its shoulder line, "
            "sleeve length and drape must be clearly readable."
        ),
    },
    "bottom": {
        "label": "하의 · 바지",
        "focus": "pants / bottom garment",
        "framing": (
            "Frame from around the chest down to below the shoes so the "
            "full length, rise and drape of the bottoms is clearly "
            "visible, including how they break over the shoes."
        ),
    },
    "shoes": {
        "label": "신발",
        "focus": "shoes / footwear",
        "framing": (
            "Frame from around the knees or calves down to the ground, "
            "shot from a low angle so the footwear is the clear subject "
            "and its silhouette, sole and material are fully visible."
        ),
    },
    "outer": {
        "label": "아우터 · 코트",
        "focus": "outerwear (jacket / coat)",
        "framing": (
            "Frame from just below the chin down to below the hem of the "
            "outerwear so its full length, lapels, closure and drape are "
            "clearly visible."
        ),
    },
    "dress": {
        "label": "원피스",
        "focus": "dress / one-piece",
        "framing": (
            "Frame from just below the chin down to below the shoes so "
            "the entire dress — its length, silhouette and how it falls — "
            "is fully visible."
        ),
    },
    "bag": {
        "label": "가방",
        "focus": "bag",
        "framing": (
            "Frame the torso and hip area so the bag is the clear subject "
            "— its size relative to the body, strap length and material "
            "must be obvious."
        ),
    },
    "full": {
        "label": "전신 코디",
        "focus": "complete outfit",
        "framing": (
            "Frame from just below the chin down to below the shoes so "
            "the entire styled outfit reads as one coordinated look."
        ),
    },
}

FACE_RULE = (
    "Absolutely do not show the person's face. Crop the frame so nothing "
    "above the chin or jawline is visible — exactly like Korean online "
    "clothing-store 'fitting cut' (착용컷) product photos, where the "
    "model's face is never shown."
)

# 심플하되 적당히 고급스러운 장소 위주. 간판/네온 같은 과한 도시 요소는 배제.
BACKGROUNDS = {
    "auto": (
        "a calm, tastefully understated location — vary it each time "
        "between a quiet minimal interior, a clean architectural exterior, "
        "and a soft natural setting"
    ),
    "minimal_wall": (
        "against a smooth off-white, beige or warm grey plaster wall, with "
        "soft directional daylight and a gentle shadow falling across it"
    ),
    "sunlit_room": (
        "in a quiet minimal interior with a large window, warm daylight "
        "falling across a wooden or polished concrete floor, and very few "
        "objects in the frame"
    ),
    "gallery": (
        "in a bright gallery-like space with clean white walls, generous "
        "empty space and soft even light"
    ),
    "quiet_cafe": (
        "in a refined, quiet cafe interior with wood, stone and linen "
        "textures, soft window light, and no visible signage or lettering"
    ),
    "architecture": (
        "beside clean modern architecture — smooth stone steps, a concrete "
        "column or a simple façade — with calm geometry and soft daylight"
    ),
    "park_path": (
        "on a quiet tree-lined path with soft greenery, dappled sunlight "
        "and a clean paved walkway"
    ),
    "forest": (
        "on a calm forest trail with tall trees, soft green foliage and "
        "gentle light filtering through the leaves"
    ),
    "field": (
        "in an open grassy field or meadow with a soft natural horizon and "
        "warm late-afternoon light"
    ),
    "seaside": (
        "near a calm seaside — soft sand or a quiet coastal path — with "
        "muted natural colours and diffused daylight"
    ),
    "rooftop": (
        "on a clean open rooftop with an unobstructed sky, soft daylight "
        "and minimal surrounding structures"
    ),
    "street_soft": (
        "on a calm, tidy city street with restrained modern storefronts, "
        "soft daylight and only minimal, unobtrusive signage"
    ),
    "golden_hour": (
        "outdoors during golden hour, with warm low sunlight, long soft "
        "shadows and a clean uncluttered setting"
    ),
}

BACKGROUND_GROUPS = [
    ("추천", [("auto", "랜덤 (매번 다르게)")]),
    (
        "실내 · 미니멀",
        [
            ("minimal_wall", "미니멀 벽"),
            ("sunlit_room", "볕 드는 실내"),
            ("gallery", "갤러리"),
            ("quiet_cafe", "조용한 카페"),
        ],
    ),
    (
        "자연",
        [
            ("park_path", "공원 산책로"),
            ("forest", "숲길"),
            ("field", "들판"),
            ("seaside", "바닷가"),
            ("golden_hour", "골든아워"),
        ],
    ),
    (
        "도시 (절제)",
        [
            ("architecture", "모던 건축"),
            ("street_soft", "차분한 거리"),
            ("rooftop", "루프탑"),
        ],
    ),
]

BACKGROUND_RULE_TEMPLATE = (
    "Background style: a calm, simple but quietly upscale location, shot "
    "{setting}. The setting should feel natural and effortless — never "
    "busy, cluttered or loud. Avoid neon, large signage, heavy text, "
    "crowds and visual noise. The background must stay soft and secondary "
    "so the product remains the hero, while still making the item look "
    "desirable and worth buying."
)

PROMPT_TEMPLATE = (
    "Using the exact same person and the exact same {focus} shown in the "
    "reference photo, generate a new photorealistic image as if shot by a "
    "professional fashion e-commerce photographer on location. "
    "{framing} {face_rule} {background_rule} Use a new, different "
    "location from the reference photo, and set the pose to: {pose}. The "
    "pose must look natural, relaxed and unforced — never stiff or "
    "exaggerated. Keep the item's colour, fabric, texture, fit and "
    "details exactly consistent and clearly recognizable with the "
    "reference photo. Soft natural lighting, clean colour grading, "
    "shallow depth of field, high-resolution editorial quality. "
    "Before generating the image, briefly describe the new scene and pose "
    "in one English sentence."
)


@app.route("/")
def index():
    return render_template(
        "index.html",
        max_count=MAX_COUNT,
        background_groups=BACKGROUND_GROUPS,
        products=[(key, val["label"]) for key, val in PRODUCTS.items()],
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
    if product_type not in PRODUCTS:
        product_type = "top"
    product = PRODUCTS[product_type]

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
                focus=product["focus"],
                framing=product["framing"],
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
