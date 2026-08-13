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
import random

from flask import Flask, request, jsonify, render_template
from PIL import Image
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 24 * 1024 * 1024  # 참고컷 + 누끼컷 2장

ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}
QUICK_MAX = 10  # 빠른 생성 모드 최대 장수
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
    "standing at a three-quarter diagonal angle with both hands tucked "
    "behind the lower back, arms half-hidden behind the torso, chest "
    "open and shoulders relaxed, the front of the outfit fully "
    "unobstructed",
]

# 카테고리별 프레이밍 — 상품이 화면에서 주인공이 되도록 컷을 다르게 잡는다.
PRODUCTS = {
    # 상의는 하의보다 훨씬 타이트하게, 가슴 높이 카메라, 사선 벽면 구도.
    # (4910 남성 상의 인기순 40컷 분석 결과 반영)
    "top": {
        "label": "상의",
        "focus": "top garment",
        "framing": (
            "Frame tightly on the upper body — from just below the chin "
            "down to roughly mid-thigh — with the camera at about chest "
            "height. The top garment must fill the majority of the frame "
            "so its shoulder line, sleeve length, chest print and drape "
            "read clearly. Compose against a wall or architectural line "
            "that runs DIAGONALLY across the frame rather than flat-on, "
            "and keep the floor almost entirely out of shot. Give the "
            "hands something natural to do — resting on a bag strap, "
            "holding a phone or a folded jacket, or tucked into a pocket."
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
    "HEAD CROP — strict: the top edge of the frame must cut across the "
    "BASE OF THE NECK, at roughly collarbone height. No face, no chin, "
    "no jawline, no mouth, no ears, no hair and no head of any kind may "
    "appear anywhere in the image, not even partially, blurred or at the "
    "very edge. If any part of the chin or jaw would enter the frame, "
    "crop lower until it is gone. This is how Korean online clothing "
    "stores shoot 착용컷 — the body starts below the neck. "
)

# 판매 상품이 컷마다 달라지는 것을 막는 잠금 규칙. 두 모드 모두에 들어간다.
GARMENT_LOCK_RULE = (
    "PRODUCT LOCK — the single most important requirement: the {focus} "
    "in the output must be the SAME physical item as in the reference "
    "photo, not a similar-looking one. Match its exact colour and shade, "
    "fabric and weave, sheen, silhouette and cut, length, collar and "
    "neckline shape, sleeve length and cuff, hem finish, pocket "
    "placement, buttons, zips, drawstrings, stitching colour, and any "
    "print, logo, lettering or graphic — including its exact artwork, "
    "size and position on the garment. Do not redesign it, do not "
    "restyle it, do not swap it for another product, and do not add or "
    "remove any detail. Only the way it folds and drapes may change, "
    "because the pose changed. "
)

# 레퍼런스 영상의 핵심 포즈 코칭: 꼿꼿이 서지 말고 '엉거주춤하게'.
# 이미 마음에 든 컷을 기준으로 '같은 자리에서 포즈만' 바꿀 때 쓰는 12종.
# 미스터제이슨 등 4910 인기 쇼핑몰 컷 분석(2026-08) 반영: 다리만 움직이는 정적인
# 자세가 아니라 '팔과 손이 뭔가 하는 중'인 동작 위주다 — 소매를 걷고, 후드를
# 만지고, 밑단을 당기고, 주머니에 깊숙이 찌른다. 같은 장면 유지 규칙 때문에
# 폰/가방 같은 새 소품은 등장시키지 않고, 입고 있는 옷을 만지는 동작만 쓴다.
STANDING_POSES = [
    "both hands tucked deep into the pockets, elbows pushed slightly "
    "outward, shoulders dropped, weight settled on one leg",
    "one hand deep in a pocket, the other hand lifted to adjust the "
    "collar or neckline, caught mid-gesture",
    "both arms raised, hands adjusting the hood behind the neck if the "
    "garment has one, otherwise smoothing the back of the collar or "
    "neckline, elbows framing the chest, sleeves shifting naturally "
    "with the motion",
    "if the sleeves are long, one hand pushing the opposite sleeve up "
    "the forearm as if rolling the cuff, caught halfway; with short "
    "sleeves, one hand lightly pinching and straightening the opposite "
    "sleeve hem",
    "one hand pinching the hem and tugging it lightly sideways so the "
    "fabric pulls taut and shows its texture, the other arm loose",
    "arms loosely crossed low over the torso, one hand gripping the "
    "opposite sleeve fabric, body angled a few degrees off centre",
    "one hand resting on the hip with the elbow out, the other hand "
    "brushing the side seam of the garment flat",
    "both hands lightly holding the bottom hem, straightening the "
    "garment downward, chin-side shoulder relaxed",
    "one arm bent with the hand pressed flat against the chest smoothing "
    "the fabric, the other hand slipped into a pocket",
    "standing at a three-quarter diagonal angle to the camera, both "
    "hands tucked away behind the lower back so the arms half-disappear "
    "behind the torso, chest open, shoulders relaxed, weight settled on "
    "the back leg with the front foot angled toward the camera — the "
    "front of the garment hangs completely unobstructed",
    "one arm swinging slightly across the body as if caught mid-motion, "
    "the sleeve moving with it, the other hand in a pocket, weight "
    "shifting between legs",
    "shoulders opened almost in profile toward the camera, the near hand "
    "tucked in a pocket, the far arm reaching across to adjust the cuff "
    "of the opposite sleeve",
]

# 모델 표준 — 180cm / 79kg, 옷 입었을 때 체격이 살아 보이도록.
MODEL_RULE = (
    "The model must be the same standard model every time: a young Korean "
    "man, about 180cm tall and 79kg, with an athletic, visibly muscular "
    "build — broad shoulders, a full chest, clearly developed arms and "
    "back, a trim waist, long well-proportioned legs, and a warm "
    "light-tan skin tone. He should read as solid and physically "
    "substantial through the clothes: the fabric sits on real shoulders "
    "and a real chest, filling out the garment rather than hanging flat "
    "on a slim frame. Keep this exact body type, build and skin tone "
    "consistent across every image. "
)

# 'AI로 만든 티'를 지우는 핵심 규칙. 4910 실제 인기 피팅컷 분석 결과:
# 전부 무보정 폰카 스냅이고, 하드한 그림자를 피하지 않으며,
# 모델과 배경이 정확히 같은 빛을 받는다 (실제 사진이므로 당연히).
REALISM_RULE = (
    "REALISM — the output must be indistinguishable from a real, "
    "unedited photo the seller casually shot of the model on a recent "
    "smartphone: subtle sensor grain, true-to-life slightly muted "
    "colours, no beauty retouching, no cinematic colour grading, no "
    "artificial glow. Real surfaces stay imperfect — scuffed pavement, "
    "faint stains on walls, uneven grass. The model and the background "
    "MUST share exactly the same light: same direction, same colour "
    "temperature, same hardness, with a natural contact shadow "
    "grounding the shoes to the floor. Hard sunlight and deep crisp "
    "shadows are welcome outdoors at midday. Frame it slightly "
    "casually, as if handheld — a small tilt or off-centre composition "
    "is natural. Never render a polished, showroom-perfect editorial "
    "image. "
)

# 상의 컷의 배경·분위기. 하의 컷보다 벽면/질감/무드 비중이 크다.
TOP_MOOD_RULE = (
    "Because this is a top-garment shot, lean the setting toward an "
    "atmospheric, textured vertical backdrop — a weathered concrete or "
    "plaster wall, exposed brick, a doorway or stairwell edge, a column, "
    "or a quiet interior corner. Let a wall edge or architectural line "
    "cut diagonally through the frame to give depth, and keep the mood "
    "softly warm and a little moody rather than bright and flat. "
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
    # 네이버 패션타운 남성의류 랭킹에서 가장 흔한 브랜드형 배경.
    "seamless": (
        "against a clean seamless studio backdrop in soft off-white, warm "
        "ivory or pale greige, curving gently into the floor with no "
        "visible corner line. One large soft light source from the side "
        "gives the body quiet dimension and lays a single soft shadow on "
        "the backdrop. Nothing else in the frame — the garment carries the "
        "whole image, the way premium Korean brand product pages shoot it"
    ),
    "concrete_wall": (
        "against a raw poured-concrete wall with faint form-tie holes, "
        "subtle staining and fine surface grain, the wall running "
        "diagonally across the frame. Hard afternoon sunlight rakes across "
        "it, leaving a crisp shadow edge and giving the texture real depth"
    ),
    "minimal_wall": (
        "against a smooth off-white, warm beige or pale grey plaster wall "
        "with faint trowel texture. Low directional daylight rakes across "
        "it so a soft gradient falls from one side to the other, and a "
        "single clean shadow anchors the body to the wall"
    ),
    "sunlit_room": (
        "in a quiet minimal interior where late-afternoon sun comes "
        "through a tall window and lays warm geometric light patches "
        "across a pale wall and a wooden or polished concrete floor. A "
        "sheer curtain softens one edge of the light; almost nothing else "
        "is in the frame"
    ),
    "gallery": (
        "in a bright gallery-like space — tall white walls, a pale "
        "seamless floor, generous empty air around the body, and soft even "
        "top light with only the faintest shadow. Calm, spacious and "
        "deliberately understated"
    ),
    "architecture": (
        "beside clean modern architecture — a smooth concrete column, a "
        "run of stone steps, a deep doorway reveal or a simple façade. "
        "Strong architectural lines cut diagonally through the frame and "
        "soft daylight models the surfaces without harshness"
    ),
    "stairwell": (
        "on a quiet stairwell landing — a metal handrail, a painted or "
        "concrete wall, and the diagonal line of the stair edge running "
        "through the frame. Daylight falls from above, giving soft "
        "top-down modelling and a calm enclosed mood"
    ),
    "park_path": (
        "on a quiet tree-lined path where dappled sunlight falls through "
        "the leaves onto a clean paved walkway. Soft layered greenery "
        "recedes behind the body, deep enough to give the frame air"
    ),
    "forest": (
        "on a calm forest trail with tall slender trunks, soft green "
        "foliage, and gentle light filtering down through the canopy in "
        "quiet shafts. Cool green shade with warm highlights"
    ),
    "field": (
        "in an open grassy field or meadow with a soft low horizon, dry "
        "golden grass moving slightly in the breeze, and warm "
        "late-afternoon sun backlighting the scene"
    ),
    "seaside": (
        "near a calm seaside — pale sand or a quiet concrete coastal path "
        "— with muted blue-grey water, a soft horizon line and bright but "
        "diffused overcast daylight"
    ),
    "rooftop": (
        "on a clean open rooftop with an unobstructed pale sky, a low "
        "parapet wall, soft daylight and almost nothing built around. "
        "Open, airy and quiet"
    ),
    "street_soft": (
        "on a calm, tidy city street with restrained modern storefronts, "
        "large clean glass, pale stone paving and soft daylight. Only "
        "minimal unobtrusive signage, no crowds, no visual noise"
    ),
    # ---- 4910 실제 인기컷 분석(2026-08)에서 뽑은 폰카 스냅 계열 ----
    "lawn_park": (
        "on an open sunlit lawn in a Korean neighbourhood park at midday "
        "— slightly patchy green grass underfoot, pine trees and young "
        "street trees behind, a tall street lamp and a paved walkway in "
        "the distance, vivid blue sky with a few cumulus clouds. The "
        "hard overhead sun leaves crisp shadows on the grass, and the "
        "camera sits at a slightly low angle so the body stands against "
        "the sky and treeline"
    ),
    "stair_steps": (
        "on wide outdoor concrete stairs beside a raw concrete wall — "
        "metal handrails, yellow tactile paving blocks at the edge of "
        "the frame, strong midday sun cutting hard diagonal shadows "
        "across the steps. Rough, real street architecture with visible "
        "stains and wear, exactly like a back street of a Korean city"
    ),
    "showroom": (
        "inside a real clothing-shop showroom corner — plain white walls "
        "with a few framed art prints hanging or leaning slightly "
        "off-centre, a black leather sofa or a slim chrome rack at the "
        "edge of the frame, flat bright indoor light like a quick phone "
        "photo taken inside the shop. Lived-in and unstaged, not a "
        "decorated set"
    ),
    "mirror": (
        "as a mirror selfie: the model stands in front of a large "
        "full-length mirror in a shop fitting area or an apartment "
        "hallway and photographs himself, one hand holding a smartphone "
        "at chest height so the phone is visible in the reflection — "
        "adapt the pose so one hand always holds the phone. The "
        "mirror's edge or frame may enter the shot, with a plain "
        "lived-in interior behind"
    ),
    # 카페/갤러리/샵은 '안'이 아니라 '입구 앞'에서 찍는다 — 사용자 레퍼런스 3컷 공통 문법:
    # 외벽+대형 유리, 유리 반사 너머로 실내가 은은하게, 바닥은 보도블록, 파사드는 사선.
    "storefront": (
        "outside on the street, right in front of a building's "
        "ground-level facade — a cafe, gallery or small shop — with a "
        "large glass window or glass door behind the model, the "
        "interior only faintly readable through soft reflections in the "
        "glass. Around the glass, a facade of raw concrete, stone or "
        "pale plaster; underfoot, a block-paved or smooth concrete "
        "sidewalk, perhaps a low entrance step or doorway reveal. Shot "
        "from a slight angle so the facade edges and window frames run "
        "diagonally through the frame, in open natural daylight — the "
        "kind of quick sharp snap taken just outside the door"
    ),
    "roadside": (
        "on an ordinary Korean roadside sidewalk on a bright day — grey "
        "block paving underfoot, a painted yellow road line or crosswalk "
        "arrow on the asphalt beside it, a parked car or SUV partly in "
        "frame, maybe a traffic cone or street trees further back, deep "
        "blue sky. Everyday street clutter left exactly as it is, "
        "because that unpolished ordinariness is what makes the photo "
        "believable; strong direct sun with crisp shadows"
    ),
    "golden_hour": (
        "outdoors during golden hour, with warm low sun raking across the "
        "scene, long soft shadows stretching across the ground, and a "
        "clean uncluttered setting glowing in amber light"
    ),
}

# '랜덤'일 때 실제로 돌려 쓸 후보들 (studio/auto 자신은 제외).
RANDOM_POOL = [
    "seamless", "concrete_wall", "minimal_wall", "sunlit_room", "gallery",
    "architecture", "stairwell", "park_path", "forest",
    "field", "seaside", "rooftop", "street_soft", "golden_hour", "studio",
    "lawn_park", "stair_steps", "showroom", "mirror", "roadside",
    "storefront",
]

# 랜덤일 때 옷에 어울리는 곳을 고르도록 유도.
GARMENT_AWARE_RULE = (
    "Choose the setting so it flatters THIS specific garment: read its "
    "colour, tone, material and mood from the reference photo, then pick "
    "surroundings whose colours sit in harmony with it and make it stand "
    "out rather than blend in. A pale garment wants a deeper or warmer "
    "backdrop; a dark garment wants a lighter, airier one. "
)

BACKGROUND_GROUPS = [
    (
        "추천",
        [
            ("studio", "렌탈 스튜디오 ★"),
            ("auto", "랜덤 (매번 다르게)"),
        ],
    ),
    (
        "폰카 스냅 (무보정 느낌)",
        [
            ("lawn_park", "공원 잔디밭"),
            ("stair_steps", "야외 계단"),
            ("storefront", "건물 앞 스냅"),
            ("roadside", "거리 스냅"),
            ("showroom", "쇼룸 스냅"),
            ("mirror", "거울샷"),
        ],
    ),
    (
        "실내 · 미니멀",
        [
            ("seamless", "무봉제 배경 (브랜드형)"),
            ("minimal_wall", "미니멀 벽"),
            ("sunlit_room", "볕 드는 실내"),
            ("gallery", "갤러리"),
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
            ("concrete_wall", "콘크리트 벽"),
            ("architecture", "모던 건축"),
            ("stairwell", "계단참"),
            ("street_soft", "차분한 거리"),
            ("rooftop", "루프탑"),
        ],
    ),
]

BACKGROUND_RULE_TEMPLATE = (
    "Background style: a calm, simple, believable location, shot "
    "{setting}. The setting should feel natural and effortless — never "
    "busy, cluttered or loud. Avoid neon, large signage, heavy text and "
    "crowds. The background must stay secondary so the product remains "
    "the hero, while still making the item look desirable and worth "
    "buying."
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

# 누끼(상품 단독) 컷을 함께 올린 경우, 상품 디테일의 기준으로 삼는다.
DETAIL_RULE = (
    "TWO images are supplied. The FIRST is the worn fitting cut — use it "
    "for how the garment sits on the body. The SECOND is a clean cut-out "
    "product shot of the exact item being sold — treat it as the "
    "authoritative reference for the item's true colour, fabric texture, "
    "print, graphics, trims, stitching and construction. Where the two "
    "disagree, follow the cut-out shot for the item's appearance. "
)

ACCESSORY_RULE_TEMPLATE = (
    "Additionally style the look with: {accessories}. Add these naturally "
    "and tastefully so they complement the outfit — but do NOT alter, "
    "cover or replace the main product itself. "
)

# 새 장소에서 찍은 것처럼 만드는 경우
PROMPT_NEW_SCENE = (
    "{detail_rule}Using the exact same {focus} shown in the reference "
    "photo, generate a new image that looks like a candid fitting-cut "
    "snapshot the seller took of the model on location with a phone — "
    "not a studio production. "
    "{garment_lock}"
    "{model_rule}{framing} {face_rule} {scene_block} {mood_rule}Set the "
    "pose to: {pose}. {pose_style}{styling_rule}{accessory_rule}"
    "{realism_rule}"
    "Keep the whole frame in natural sharp focus — the background must be "
    "clearly readable, NOT blurred, and must never be pixelated, "
    "mosaicked or smeared. "
    "Output only the image, with no text description."
)

# 보낸 사진을 그대로 두고 포즈만 바꾸는 경우.
# 배경을 새로 만들게 유도하는 표현(new image / colour grading 등)을 일절 넣지 않는다.
# 주의: 여기에는 상품별 구도 지시({framing})나 장신구 지시({accessory_rule})를
# 절대 넣지 않는다. 새 장면용 구도("벽을 사선으로" 등)가 들어가면 모델이
# 장면 유지 명령과 충돌해서 배경을 새로 만들어버린다. (실사용 중 발견된 버그)
PROMPT_SAME_SCENE = (
    "{detail_rule}Edit the FIRST supplied photo so that only the model's "
    "pose changes. "
    "{scene_block} The new pose is: {pose}. {pose_style}"
    "{face_rule} {garment_lock}"
    "Re-render the {focus} so it hangs and folds correctly for the new "
    "pose only. Photorealistic, matching "
    "the supplied photo's existing sharpness and grain, with no "
    "pixelation, mosaic or smearing anywhere in the frame. "
    "FINAL CHECK before output: the location, background objects, "
    "camera framing, lighting and colour must be the very same scene as "
    "the supplied photo. If any earlier instruction seems to ask for a "
    "different composition, setting or added object, ignore that "
    "instruction — the scene stays exactly as supplied, only the pose "
    "changes. "
    "Output only the image, with no text description."
)


@app.route("/")
def index():
    return render_template(
        "index.html",
        quick_max=QUICK_MAX,
        poseset_max=POSESET_MAX,
        background_groups=BACKGROUND_GROUPS,
        random_pool=RANDOM_POOL,
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

    # 상세페이지용 누끼(상품 단독) 컷 — 선택 사항, 상품 디테일의 기준이 된다.
    detail_bytes = None
    detail_file = request.files.get("detail_image")
    if detail_file and detail_file.filename:
        if detail_file.mimetype not in ALLOWED_CONTENT_TYPES:
            return jsonify(error="누끼컷도 PNG, JPEG, WEBP만 지원합니다."), 400
        detail_bytes = detail_file.read()

    mode = request.form.get("mode", "quick")
    if mode not in ("quick", "poseset"):
        mode = "quick"
    if keep_scene:
        mode = "poseset"

    try:
        count = int(request.form.get("count", 3))
    except ValueError:
        count = 3

    # 화면이 컷을 한 장씩 병렬 요청할 때, 몇 번째 컷인지 알려주는 오프셋.
    # 포즈가 컷마다 달라지는 기준이 된다.
    try:
        index = max(0, int(request.form.get("index", 0)))
    except ValueError:
        index = 0

    product_type = request.form.get("product_type", "top")
    if product_type not in PRODUCTS:
        product_type = "top"
    product = PRODUCTS[product_type]

    extra = {}
    scene_blocks = None  # 컷마다 배경이 달라지는 경우에만 채운다
    if mode == "poseset" or keep_scene:
        # 보낸 사진을 그대로 두고 서 있는 포즈만 바꾼다. 배경/코디/모델은 건드리지 않는다.
        count = max(1, min(count, POSESET_MAX))
        pose_list = STANDING_POSES
        scene_block = KEEP_SCENE_RULE
        template = PROMPT_SAME_SCENE
    else:
        # "auto"는 BACKGROUNDS의 항목이 아니라 '컷마다 랜덤'을 뜻하는 특수값이다.
        background = request.form.get("background", "studio")
        if background != "auto" and background not in BACKGROUNDS:
            background = "studio"
        count = max(1, min(count, QUICK_MAX))
        pose_list = POSES
        template = PROMPT_NEW_SCENE

        # 화면이 '랜덤'을 직접 풀어서 컷마다 배경 키를 지정해 보낼 때 붙는 플래그.
        # 배경이 랜덤으로 골라졌으니 옷에 어울리게 연출하라는 규칙을 유지한다.
        garment_aware = request.form.get("garment_aware") == "1"

        if background == "auto":
            # 진짜 랜덤: 매 요청마다 순서를 섞어 컷마다 다른 장소를 쓴다.
            picks = random.sample(RANDOM_POOL, min(count, len(RANDOM_POOL)))
            scene_blocks = [
                BACKGROUND_RULE_TEMPLATE.format(setting=BACKGROUNDS[k])
                + " "
                + GARMENT_AWARE_RULE
                + LOCATION_RULE_VARY
                for k in picks
            ]
            scene_block = scene_blocks[0]
        else:
            scene_block = (
                BACKGROUND_RULE_TEMPLATE.format(setting=BACKGROUNDS[background])
                + " "
                + (GARMENT_AWARE_RULE if garment_aware else "")
                + LOCATION_RULE_VARY
            )

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
        extra["realism_rule"] = REALISM_RULE
        # 상의는 사선 벽면·질감 위주의 무드를 한 겹 더 얹는다.
        # 단, 폰카 스냅 계열 배경은 자기만의 빛/장소 문법이 있어서 얹지 않는다.
        snap_style = background in (
            "lawn_park", "stair_steps", "showroom", "mirror", "roadside",
            "storefront",
        )
        extra["mood_rule"] = (
            TOP_MOOD_RULE if product_type == "top" and not snap_style else ""
        )

    extra["detail_rule"] = DETAIL_RULE if detail_bytes else ""

    accessories = (request.form.get("accessories") or "").strip()
    accessory_rule = (
        ACCESSORY_RULE_TEMPLATE.format(accessories=accessories[:200])
        if accessories
        else ""
    )

    try:
        contents_images = [Image.open(io.BytesIO(image_bytes))]
        if detail_bytes:
            contents_images.append(Image.open(io.BytesIO(detail_bytes)))
    except (OSError, ValueError):
        return jsonify(error="이미지 파일을 읽지 못했습니다. 다른 파일로 시도해주세요."), 400

    client = genai.Client(api_key=api_key)

    results = []
    warning = None

    try:
        for i in range(count):
            pose = pose_list[(index + i) % len(pose_list)]
            block = scene_blocks[i] if scene_blocks else scene_block
            prompt = template.format(
                focus=product["focus"],
                framing=product["framing"],
                face_rule=FACE_RULE,
                garment_lock=GARMENT_LOCK_RULE.format(focus=product["focus"]),
                scene_block=block,
                pose_style=POSE_STYLE_RULE,
                accessory_rule=accessory_rule,
                pose=pose,
                **extra,
            )
            response = client.models.generate_content(
                model=MODEL,
                contents=[prompt, *contents_images],
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
        # 이미 만들어진 컷이 있다면 버리지 않고 경고와 함께 돌려준다.
        if not results:
            status = 401 if e.code in (401, 403) else 400
            return jsonify(error=f"요청이 거부되었습니다: {e.message}"), status
        warning = f"일부 컷 생성이 중단되었습니다: {e.message}"
    except genai_errors.APIError as e:
        if not results:
            return jsonify(error=f"Gemini 요청 중 오류가 발생했습니다: {e.message}"), 502
        warning = f"일부 컷 생성이 중단되었습니다: {e.message}"

    if not results:
        return jsonify(error="이미지가 생성되지 않았습니다. 다시 시도해주세요."), 502

    return jsonify(results=results, warning=warning)


if __name__ == "__main__":
    # threaded=True: 화면이 컷을 여러 장 동시에 요청하므로 병렬 처리가 필요하다
    app.run(host="127.0.0.1", port=5000, threaded=True)
