"""
브라우저에서 모델 착용 사진을 업로드하면, Google Gemini 3.1 Flash Image(나노바나나 2)를
이용해 같은 인물/제품을 유지한 채로 배경과 포즈를 바꾼 새로운 사진 여러 장을
생성해주는 웹앱.

방문자가 자신의 Google AI Studio API 키를 매 요청마다 직접 입력한다. 서버는 그 키를
저장하지 않고 해당 요청 처리에만 사용한다.
"""

import base64
import binascii
import io

from flask import Flask, request, jsonify, render_template
from PIL import Image
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB

ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}
QUICK_MAX = 4  # 빠른 생성 모드 최대 장수
POSESET_MAX = 12  # 포즈 모음 모드 최대 장수 (최소 1)
MODEL = "gemini-3.1-flash-image"

# 쇼핑몰 착용컷의 정석 구도 12종. 앞의 4개는 어떤 상품에나 무난해서
# 빠른 생성 모드에서 우선 사용된다.
POSES = [
    "standing straight toward the camera, weight settled on one leg, one "
    "hand resting lightly in a pocket, shoulders relaxed",
    "turned to a three-quarter angle, body slightly away from the camera, "
    "both hands loose at the sides",
    "captured mid-step, walking calmly and unhurried toward the camera",
    "standing still while leaning lightly against a wall, column or "
    "railing in the scene",
    "standing square to the camera with both hands in pockets, elbows "
    "relaxed outward",
    "in full profile from the side, showing the silhouette and side line "
    "of the garment",
    "seen from behind, showing the back of the garment and its fit across "
    "the shoulders and back",
    "seated naturally on steps, a bench or a low ledge, posture relaxed",
    "standing in a soft contrapposto — one knee slightly bent, hips "
    "gently shifted, a natural unposed stance",
    "standing with arms lightly crossed, upper body angled a few degrees "
    "off centre",
    "one hand lifting or adjusting the hem, cuff or collar of the garment, "
    "drawing attention to its detail and texture",
    "photographed from a slightly low angle so the body line looks long "
    "and the garment's proportions read clearly",
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

# 레퍼런스 영상의 핵심 포즈 코칭: 꼿꼿이 서지 말고 '엉거주춤하게'.
# 골반을 살짝 앞으로 내밀고 무릎을 풀면 옷이 자연스럽게 떨어진다.
# 이미 마음에 든 컷을 기준으로 '같은 자리에서 포즈만' 바꿀 때 쓰는 12종.
# 앉기/걷기/뒷모습 없이 전부 서 있는 자세이며, 손 위치·체중·각도만 미세하게 다르다.
STANDING_POSES = [
    "standing squarely toward the camera, both arms hanging naturally at "
    "the sides",
    "standing with one hand slipped into a pocket, the other arm loose at "
    "the side",
    "standing with both hands in pockets, elbows relaxed slightly outward",
    "body turned a few degrees to one side in a soft three-quarter angle, "
    "arms relaxed",
    "body turned a few degrees to the opposite side, weight shifted onto "
    "the back leg",
    "weight settled onto the left leg, right knee softened and turned "
    "slightly inward",
    "weight settled onto the right leg, left foot placed a little forward",
    "one hand lightly holding the hem or side seam of the garment, "
    "drawing the eye to its drape",
    "arms loosely crossed low over the torso, shoulders dropped",
    "one hand resting on the hip, the other hanging naturally",
    "one hand lifted to adjust a sleeve or strap, the movement caught "
    "mid-gesture",
    "standing almost in profile with the shoulders opened back toward the "
    "camera, showing the side line of the outfit",
]

# 모델 표준 — 사용자가 제시한 기준 컷(근육질 남성 모델)에 맞춰 매번 동일하게.
MODEL_RULE = (
    "The model must be the same standard model every time: a young Korean "
    "man with an athletic, visibly muscular build — broad shoulders, a "
    "defined chest and clearly developed arms, a trim waist, tall "
    "well-proportioned legs, and a warm light-tan skin tone. Keep this "
    "exact body type, build and skin tone consistent across every image. "
)

# 판매 상품은 절대 건드리지 않고 '나머지 코디'만 바꾼다.
STYLINGS = {
    "keep": {"label": "그대로", "desc": ""},
    "minimal": {
        "label": "미니멀 베이직",
        "desc": (
            "a plain fine-gauge tee or knit, straight clean-cut trousers, "
            "and low-profile leather sneakers, all in neutral tones "
            "(white, black, grey, beige)"
        ),
    },
    "street": {
        "label": "스트릿",
        "desc": (
            "an oversized tee or hoodie, wide cargo or loose denim, a ball "
            "cap, and chunky dad sneakers"
        ),
    },
    "amekaji": {
        "label": "아메카지",
        "desc": (
            "a washed chambray or flannel shirt worn loose, wide chino "
            "trousers, and canvas sneakers, in earthy washed tones"
        ),
    },
    "cityboy": {
        "label": "모던 시티보이",
        "desc": (
            "an oversized crisp shirt tucked loosely, wide pleated slacks, "
            "and leather loafers, in muted refined tones"
        ),
    },
    "sporty": {
        "label": "스포티",
        "desc": (
            "a track top or lightweight windbreaker, tapered track pants, "
            "and running sneakers"
        ),
    },
    "workwear": {
        "label": "워크웨어",
        "desc": (
            "a boxy work jacket or coverall shirt, sturdy carpenter "
            "trousers, and worn leather boots"
        ),
    },
}

STYLING_RULE_TEMPLATE = (
    "Restyle the rest of the outfit — every garment EXCEPT the {focus} — "
    "as {desc}. The {focus} itself must stay exactly as it is in the "
    "reference photo, unchanged in colour, fabric, cut and detail; only "
    "the surrounding items change so the look reads as a deliberate "
    "coordinated outfit. "
)

POSE_STYLE_RULE = (
    "Posture direction, following Korean fitting-cut convention: the "
    "stance must look loose and slightly slouched rather than upright and "
    "formal — hips pushed a little forward, knees soft and slightly bent, "
    "shoulders dropped and relaxed, body weight settled unevenly on one "
    "leg. This faintly awkward, unposed stance is what makes the garment "
    "hang and drape naturally. Never a stiff, straight-backed runway pose. "
)

# 심플하되 적당히 고급스러운 장소 위주. 간판/네온 같은 과한 도시 요소는 배제.
BACKGROUNDS = {
    # 레퍼런스 영상(쇼핑몰 빌드업 테크트리)의 렌탈 스튜디오 셋업을 그대로 재현.
    "studio": (
        "in a bright, minimal Korean rental photo studio — a clean "
        "off-white wall and a smooth grey concrete floor, with sheer "
        "white curtains over a large window. Keep a few tasteful props "
        "toward the edge of the frame: a slim chrome-and-glass shelving "
        "unit holding a couple of glass cups and a white sphere lamp, a "
        "small round cafe table, and a monstera plant. Lit only by soft "
        "natural daylight from the window, with gentle patches of "
        "sunlight falling on the wall and floor; artificial lights are "
        "off, so the exposure is calm and slightly deep rather than flat "
        "and bright"
    ),
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
    (
        "추천",
        [
            ("studio", "렌탈 스튜디오 ★"),
            ("auto", "랜덤 (매번 다르게)"),
        ],
    ),
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

# 빠른 생성: 매 컷 장소가 달라져도 됨 / 포즈 모음: 한 장소에서 찍은 것처럼 고정
LOCATION_RULE_VARY = (
    "Use a new, different location from the reference photo."
)
# 보낸 사진을 그대로 쓰고 포즈만 바꿀 때. 배경 프리셋은 일절 개입하지 않는다.
KEEP_SCENE_RULE = (
    "CRITICAL INSTRUCTION — treat the supplied photo as the finished set, "
    "not as loose inspiration. This output must look like another frame "
    "from that very same photo session, taken a few seconds later with "
    "the camera untouched on its tripod. "
    "Do NOT invent, replace or restyle the background. Reproduce the "
    "IDENTICAL location, wall, floor, doorway, window and every existing "
    "object exactly where it already sits in the supplied photo — same "
    "position, same size, same angle. Do not add any new object, do not "
    "remove any object, and do not move, rotate or rescale anything in "
    "the scene. "
    "Keep the lighting, shadow direction, time of day, white balance and "
    "colour grading identical. Keep the camera angle, camera height, "
    "focal length, distance and crop identical. "
    "Keep the same model — same body, build, skin tone, hair and hands — "
    "wearing the exact same outfit, shoes and accessories, unchanged in "
    "every detail down to wrinkles, seams and logos. "
    "The ONLY thing that may differ from the supplied photo is the "
    "model's body pose."
)

ACCESSORY_RULE_TEMPLATE = (
    "Additionally style the look with: {accessories}. Add these naturally "
    "and tastefully so they complement the outfit — but do NOT alter, "
    "cover or replace the main product itself. "
)

# 새 장소에서 찍은 것처럼 만드는 경우
PROMPT_NEW_SCENE = (
    "Using the exact same {focus} shown in the reference photo, generate "
    "a new photorealistic image as if shot by a professional fashion "
    "e-commerce photographer on location. "
    "{model_rule}{framing} {face_rule} {scene_block} Set the pose "
    "to: {pose}. {pose_style}{styling_rule}{accessory_rule}Keep the "
    "item's colour, fabric, texture, fit and details exactly consistent "
    "and clearly recognizable with the reference photo. "
    "Keep the whole frame in natural sharp focus — the background must be "
    "clearly readable, NOT blurred, and must never be pixelated, "
    "mosaicked or smeared. Soft natural lighting, true-to-life colour, "
    "high-resolution quality. "
    "Output only the image, with no text description."
)

# 보낸 사진을 그대로 두고 포즈만 바꾸는 경우.
# 배경을 새로 만들게 유도하는 표현(new image / colour grading 등)을 일절 넣지 않는다.
PROMPT_SAME_SCENE = (
    "Edit the supplied photo so that only the model's pose changes. "
    "{scene_block} The new pose is: {pose}. {pose_style}"
    "{framing} {face_rule} {accessory_rule}"
    "Re-render the {focus} so it hangs and folds correctly for the new "
    "pose, while keeping its colour, fabric, texture, fit, seams and "
    "print exactly as in the supplied photo. Photorealistic, matching "
    "the supplied photo's existing sharpness and grain, with no "
    "pixelation, mosaic or smearing anywhere in the frame. "
    "Output only the image, with no text description."
)


@app.route("/")
def index():
    return render_template(
        "index.html",
        quick_max=QUICK_MAX,
        poseset_max=POSESET_MAX,
        background_groups=BACKGROUND_GROUPS,
        products=[(key, val["label"]) for key, val in PRODUCTS.items()],
        stylings=[(key, val["label"]) for key, val in STYLINGS.items()],
    )


@app.route("/api/process", methods=["POST"])
def process():
    api_key = (request.form.get("api_key") or "").strip()
    if not api_key:
        return jsonify(error="Google AI Studio API 키를 입력해주세요."), 400

    # 앞서 생성된 결과 한 장을 그대로 이어받는 경우(data URL)와
    # 새 사진을 업로드하는 경우를 모두 지원한다.
    reference = request.form.get("reference") or ""
    keep_scene = False
    if reference.startswith("data:image/"):
        try:
            b64 = reference.split(",", 1)[1]
            image_bytes = base64.b64decode(b64)
        except (IndexError, ValueError, binascii.Error):
            return jsonify(error="선택한 이미지를 읽지 못했습니다."), 400
        keep_scene = True
    else:
        image_file = request.files.get("image")
        if not image_file or not image_file.filename:
            return jsonify(error="이미지 파일을 선택해주세요."), 400
        if image_file.mimetype not in ALLOWED_CONTENT_TYPES:
            return jsonify(error="PNG, JPEG, WEBP 이미지만 지원합니다."), 400
        image_bytes = image_file.read()

    mode = request.form.get("mode", "quick")
    if mode not in ("quick", "poseset"):
        mode = "quick"
    if keep_scene:
        mode = "poseset"

    try:
        count = int(request.form.get("count", 3))
    except ValueError:
        count = 3

    product_type = request.form.get("product_type", "top")
    if product_type not in PRODUCTS:
        product_type = "top"
    product = PRODUCTS[product_type]

    extra = {}
    if mode == "poseset" or keep_scene:
        # 보낸 사진을 그대로 두고 서 있는 포즈만 바꾼다. 배경/코디/모델은 건드리지 않는다.
        count = max(1, min(count, POSESET_MAX))
        pose_list = STANDING_POSES
        scene_block = KEEP_SCENE_RULE
        template = PROMPT_SAME_SCENE
    else:
        background = request.form.get("background", "studio")
        if background not in BACKGROUNDS:
            background = "studio"
        count = max(1, min(count, QUICK_MAX))
        pose_list = POSES
        scene_block = (
            BACKGROUND_RULE_TEMPLATE.format(setting=BACKGROUNDS[background])
            + " "
            + LOCATION_RULE_VARY
        )
        template = PROMPT_NEW_SCENE

        styling = request.form.get("styling", "keep")
        if styling not in STYLINGS:
            styling = "keep"
        desc = STYLINGS[styling]["desc"]
        extra["styling_rule"] = (
            STYLING_RULE_TEMPLATE.format(focus=product["focus"], desc=desc)
            if desc
            else ""
        )
        extra["model_rule"] = MODEL_RULE

    accessories = (request.form.get("accessories") or "").strip()
    accessory_rule = (
        ACCESSORY_RULE_TEMPLATE.format(accessories=accessories[:200])
        if accessories
        else ""
    )

    ref_image = Image.open(io.BytesIO(image_bytes))

    client = genai.Client(api_key=api_key)

    results = []

    try:
        for i in range(count):
            pose = pose_list[i % len(pose_list)]
            prompt = template.format(
                focus=product["focus"],
                framing=product["framing"],
                face_rule=FACE_RULE,
                scene_block=scene_block,
                pose_style=POSE_STYLE_RULE,
                accessory_rule=accessory_rule,
                pose=pose,
                **extra,
            )
            response = client.models.generate_content(
                model=MODEL,
                contents=[prompt, ref_image],
                config=types.GenerateContentConfig(
                    response_modalities=[types.Modality.TEXT, types.Modality.IMAGE],
                ),
            )
            image_data_url = None
            for part in response.parts:
                if part.inline_data:
                    b64 = base64.b64encode(part.inline_data.data).decode()
                    image_data_url = f"data:image/png;base64,{b64}"
            if image_data_url:
                results.append({"image": image_data_url})
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
