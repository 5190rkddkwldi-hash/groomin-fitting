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
import json
import os
import random
from datetime import timedelta

from flask import (
    Flask, request, jsonify, render_template, session, redirect, url_for,
)
from PIL import Image
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 24 * 1024 * 1024  # 참고컷 + 누끼컷 2장
# 12포즈 모드는 고른 컷을 파일이 아니라 data URL 텍스트 필드(reference)로 보낸다.
# Werkzeug 3.1부터 일반 폼 필드 합계가 기본 500KB로 제한돼 그 요청이 413으로
# 잘리므로, 텍스트 필드 한도도 전체 업로드 한도와 같게 올린다.
app.config["MAX_FORM_MEMORY_SIZE"] = 24 * 1024 * 1024

# 공유용 간단 로그인: 추천인 코드가 맞으면 상호명으로 입장한다.
# 배포 시에는 환경변수로 코드와 세션 키를 바꿀 수 있다.
app.secret_key = os.environ.get("SECRET_KEY", "groomin-fitting-dev-secret")
app.permanent_session_lifetime = timedelta(days=90)
REFERRAL_CODE = os.environ.get("REFERRAL_CODE", "grooming2026")

ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}
QUICK_MAX = 10  # 빠른 생성 모드 최대 장수
POSESET_MAX = 13  # 포즈 모음 모드 최대 장수 = 기본 포즈 1 + 변주 12
# 이미지 모델 후보. 첫 후보가 무응답/혼잡이면 다음 후보로 자동 폴백한다.
# (플래너 텍스트 모델과 같은 패턴. 2026-08-17 실제 발생: 구글 혼잡으로
# 3.1-flash-image는 2분+ 무응답, 3-pro-image는 503 'high demand'.)
# 사용자 방침: 화질이 우선 — lite 같은 하위 모델로 몰래 낮추지 않는다.
# 전부 실패하면 '혼잡하니 잠시 후 재시도' 오류를 그대로 보여준다.
IMAGE_MODELS = [
    "gemini-3.1-flash-image",
    "gemini-3.1-flash-image-preview",
    "gemini-3-pro-image",
]
# 이미지 생성 1회 최대 대기(밀리초). 정상 생성은 보통 10~60초 안에 끝난다.
IMAGE_TIMEOUT_MS = 75_000
# 한 번 성공한 모델을 기억해, 죽은 모델의 타임아웃을 컷마다 다시 기다리지 않는다.
_image_model_pick = {"name": None}


class ImageModelUnavailable(RuntimeError):
    """모든 이미지 모델 후보가 실패했을 때."""


# 폰 원본(수 MB)을 그대로 보내면 업로드·처리에 컷당 몇 초씩 낭비된다.
# 생성 품질에는 긴 변 1536px이면 충분하므로 그 이상은 줄여서 보낸다.
# (코디 판독은 1024px이면 충분해서 더 줄여 부른다.)
def _load_shrunk(raw, max_side=1536):
    img = Image.open(io.BytesIO(raw))
    img.load()
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    return img


def _generate_image_with_fallback(client, prompt, images):
    cached = _image_model_pick["name"]
    candidates = ([cached] if cached else []) + [
        m for m in IMAGE_MODELS if m != cached
    ]
    last_err = None
    for model_name in candidates:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[prompt, *images],
                config=types.GenerateContentConfig(
                    response_modalities=[types.Modality.TEXT, types.Modality.IMAGE],
                ),
            )
        except genai_errors.ClientError as e:
            if e.code in (401, 403):
                raise  # 키 문제는 폴백해도 소용없다
            last_err = e
            continue
        except genai_errors.APIError as e:
            last_err = e
            continue
        except Exception as e:  # 타임아웃 등 전송 계층 오류
            last_err = e
            continue
        _image_model_pick["name"] = model_name
        return response
    raise ImageModelUnavailable(
        "Google 이미지 생성 서버가 혼잡해 응답하지 않습니다. "
        "일시적인 현상이니 잠시 후 다시 시도해주세요."
    )

# 쇼핑몰 착용컷의 '기본값' 포즈 — 한쪽 주머니에 손을 넣고 편하게 선 정석 자세.
# 어떤 상품에나 안전하게 맞아서, 빠른 생성과 포즈 모음 모두 1번 컷은 항상 이 포즈다.
# (2026-09-01 사용자 요청: "기본 포즈는 기본적으로 나오게")
BASE_POSE = (
    "the standard fitting-cut stance: standing toward the camera, turned "
    "only a few degrees off centre, one hand slipped casually into a "
    "pocket as far as the wrist — the trouser pocket if the top garment "
    "has none — while the other arm hangs loose and straight down the "
    "side; the weight sinks onto the leg on the pocket side so that hip "
    "settles low, the other foot rests flat about half a step to the side "
    "with the knee soft and the toe turned out a little, the shoulders "
    "relaxed and carried level, the front of the outfit left completely "
    "unobstructed"
)

# 쇼핑몰 착용컷의 정석 구도. 0번은 항상 기본 포즈이고, 뒤로 갈수록 변주가 커진다.
POSES = [
    BASE_POSE,
    "turned to a three-quarter angle, body slightly away from the camera, "
    "both hands loose at the sides",
    "captured mid-step, walking calmly and unhurried toward the camera",
    "standing still while leaning lightly against a wall, column or "
    "railing in the scene",
    "standing square to the camera with both hands in pockets, elbows "
    "relaxed outward",
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

# 포즈 모음(같은 장면) 모드 전용 얼굴 규칙.
# 빠른 생성은 새 장면을 만들기 때문에 늘 목 아래 크롭(FACE_RULE)이지만,
# 포즈 모음은 '올린 사진의 다음 컷'이라 올린 사진을 따라가야 한다:
#   얼굴이 나온 사진 → 같은 사람 얼굴을 그대로(다른 사람/미화 금지)
#   얼굴이 없는 사진 → 지금까지처럼 얼굴이 절대 등장하지 않게
FACE_ADAPTIVE_RULE = (
    "FACE — decide this from the supplied photo itself, then follow it "
    "exactly. "
    "CASE A, the supplied photo already shows the model's face or any part "
    "of the head: keep that identical person. Reproduce the very same face — "
    "the same facial structure and proportions, the same eyes, eyebrows, "
    "nose, mouth and jaw shape, the same skin tone and texture, the same "
    "hairstyle, hair colour, length, parting and hairline, and the same "
    "facial hair if there is any. Keep the same expression and roughly the "
    "same head angle, letting the head follow the new stance only as far as "
    "the body naturally carries it. Someone looking at the two photos must "
    "see the same individual photographed a second later, so treat the face "
    "as locked identity to be copied rather than something to redraw, "
    "idealise, smooth, slim, age or restyle. Keep the head inside the frame "
    "exactly as the supplied photo frames it. "
    "CASE B, the supplied photo is already cropped below the head so that no "
    "face is visible: keep exactly that same crop. The top edge of the frame "
    "stays at the BASE OF THE NECK, at roughly collarbone height, and the "
    "body starts below the neck — this is how Korean online clothing stores "
    "shoot 착용컷. No face, no chin, no jawline, no mouth, no ears, no hair "
    "and no head of any kind may appear anywhere in the image, not even "
    "partially, blurred or at the very edge. If any part of the chin or jaw "
    "would enter the frame, crop lower until it is gone. "
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
    "remove any detail. Preserve the exact FIT as worn in the reference "
    "photo — the same degree of looseness or slimness on the body, the "
    "same length on the torso or leg. Only the way it folds and drapes "
    "may change, because the pose changed. "
)

# 코디 스타일 '그대로'일 때: 판매 상품 외 나머지 착장도 참조 사진과 같게 잠근다.
# (이 규칙이 없으면 모델이 신발·바지 등을 마음대로 지어내는 문제가 있었다.)
OUTFIT_KEEP_RULE = (
    "OUTFIT LOCK — the rest of the outfit must also stay faithful to the "
    "reference photo: every other visible garment, footwear and accessory "
    "keeps the same type, colour and overall look as actually worn in the "
    "reference photo. Do not invent, swap or restyle any item of the "
    "outfit. "
)

# 레퍼런스 영상의 핵심 포즈 코칭: 꼿꼿이 서지 말고 '엉거주춤하게'.
# 이미 마음에 든 컷을 기준으로 '같은 자리에서 포즈만' 바꿀 때 쓰는 12종.
# 미스터제이슨 등 4910 인기 쇼핑몰 컷 분석(2026-08) 반영: 다리만 움직이는 정적인
# 자세가 아니라 '팔과 손이 뭔가 하는 중'인 동작 위주다 — 소매를 걷고, 후드를
# 만지고, 밑단을 당기고, 주머니에 깊숙이 찌른다. 같은 장면 유지 규칙 때문에
# 폰/가방 같은 새 소품은 등장시키지 않고, 입고 있는 옷을 만지는 동작만 쓴다.
# 2026-09-01: 상체만 바뀌고 하체가 거의 고정되는 문제 → 12종 전부에 그 팔
# 동작과 맞물리는 하체 문구(체중 실리는 다리·무릎·발 위치·발끝 방향·골반
# 라인)를 붙였다. 과장된 화보 포즈가 아니라 실제 쇼핑몰 컷 수준의 작은 변화.
STANDING_POSES = [
    BASE_POSE,

    "both hands tucked deep into the pockets, elbows pushed slightly "
    "outward, the shoulders loose but held level — the weight sinks onto "
    "one leg so that "
    "hip rides higher, while the other foot rests half a step forward "
    "with the knee soft and the toe angled a little outward",

    "one hand deep in a pocket, the other hand lifted to adjust the "
    "collar or neckline, caught mid-gesture — the hip on the pocket side "
    "pushes out and carries the weight, the opposite foot drawn back a "
    "half step with the heel barely off the ground",

    "both arms raised, hands adjusting the hood behind the neck if the "
    "garment has one, otherwise smoothing the back of the collar or "
    "neckline, elbows framing the chest, sleeves shifting naturally "
    "with the motion — the body settles low as the arms come up: feet "
    "about hip-width apart, one knee clearly more bent than the other, "
    "hips tilted toward the slack leg",

    "if the sleeves are long, one hand pushing the opposite sleeve up "
    "the forearm as if rolling the cuff, caught halfway; with short "
    "sleeves, one hand lightly pinching and straightening the opposite "
    "sleeve hem — the stance turns with the working arm: one foot set "
    "forward on a slight diagonal taking most of the weight, the back "
    "leg trailing straight and relaxed",

    "one hand pinching the hem and tugging it lightly sideways so the "
    "fabric pulls taut and shows its texture, the other arm loose — the "
    "weight stays on the leg nearest that hand and its hip settles down, "
    "while the other foot rests flat a small distance to the side with "
    "the knee relaxed",

    "arms loosely crossed low over the torso, one hand gripping the "
    "opposite sleeve fabric, body angled a few degrees off centre — the "
    "weight sits back on one heel while the other leg slides forward, "
    "that knee almost straight and the toe turned out",

    "one hand resting on the hip with the elbow out, the other hand "
    "brushing the side seam of the garment flat — the hips push toward "
    "the hand on the hip and that leg straightens to take the weight, "
    "the far leg relaxed with a soft knee and the foot turned away",

    "both hands lightly holding the bottom hem, straightening the "
    "garment downward, chin-side shoulder relaxed — the feet come close "
    "together and almost parallel, both knees loose, the weight settled "
    "back onto the heels so the torso leans in a fraction",

    "one arm bent with the hand pressed flat against the chest smoothing "
    "the fabric, the other hand slipped into a pocket — caught mid-shift: "
    "one foot pivoting on the ball of the foot with that knee rolling "
    "gently inward, the other leg planted straight and holding the weight",

    "standing at a three-quarter diagonal angle to the camera, both "
    "hands tucked away behind the lower back so the arms half-disappear "
    "behind the torso, chest open, shoulders relaxed, weight settled on "
    "the back leg with the front foot angled toward the camera, that "
    "front knee soft and the hip line dropped on the loose side — the "
    "front of the garment hangs completely unobstructed",

    "one arm swinging slightly across the body as if caught mid-motion, "
    "the sleeve moving with it, the other hand in a pocket — a half step "
    "is in progress: the front foot lands flat while the back heel has "
    "just lifted, the weight travelling between the legs and the hips "
    "rotating a few degrees with it",

    "shoulders opened almost in profile toward the camera, the near hand "
    "tucked in a pocket, the far arm reaching across to adjust the cuff "
    "of the opposite sleeve — the feet line up one behind the other "
    "along that same diagonal, the front knee soft with the weight "
    "forward and the back heel slightly raised",
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
    # 고정 문구가 없다 — /api/coordinate 가 상품을 보고 그때그때 지어낸 코디를
    # styling_desc 로 실어 보낸다. desc 가 비어 있으면 '그대로'로 안전하게 떨어진다.
    "auto": {"label": "★ AI 자동 코디", "desc": ""},
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

# AI 자동 코디에서만 얹는 상체 규칙. 쇼핑몰 착용컷이 실물보다 태가 사는 이유는
# 어깨가 쳐지지 않고 어깨선이 수평으로 넓게 잡히기 때문이다.
# 옷의 재단·핏은 건드리지 않고 '자세'만 다룬다 (GARMENT_LOCK_RULE 과 충돌 방지).
# 어깨 라인 — 기본은 올린 사진을 그대로 따라간다. 넓히기는 사용자가 버튼으로 켠다.
# (2026-09-01 사용자 요청: "원래 사진에 맞게 연출하고, 어깨 넓게는 버튼으로 따로")
SHOULDERS = {
    "keep": {
        "label": "사진 그대로",
        "rule": (
            "SHOULDER LINE — take the shoulders straight from the reference "
            "photo: the same width across, the same slope, the same way they "
            "carry. Reproduce that shoulder line as it is rather than "
            "improving it. "
        ),
    },
    "wide": {
        "label": "어깨 넓게 보정",
        # '조금'이 핵심이다. 세게 밀면 보디빌더가 되어 같은 사람으로 안 보인다.
        "rule": (
            "SHOULDER LINE — give the upper body the light lift a Korean "
            "shopping-mall fitting cut has: the shoulders sit open and rolled "
            "slightly back so the collarbone line reads level, and the neck "
            "stays long. Widen the shoulder line only a little — a gentle "
            "correction of the reference photo, the same body standing "
            "better, still the width a real person has in an everyday "
            "snapshot. This is carriage, not extra muscle: the build stays "
            "the model's own and the waist is unchanged. Everything from the "
            "ribs down keeps the loose, unposed stance described above, and "
            "the garment's own cut, fit and length are unchanged — this "
            "governs posture only. "
        ),
    },
}

# 상의를 하의에 넣어 입을지. 옷의 실제 기장을 바꾸는 게 아니라 '입는 방식'만 정한다.
TUCKS = {
    "keep": {"label": "사진 그대로", "rule": "", "ko": ""},
    "in": {
        "label": "넣어서 입기",
        "ko": "상의를 하의에 넣어 입는다(허리선과 벨트가 보인다)",
        "rule": (
            "TUCK — the top is worn tucked into the waistband of the "
            "trousers the whole way round, so the waistline reads clearly "
            "and the belt or waistband is visible. Let the fabric blouse "
            "very slightly over the waistband the way a real tuck sits, "
            "rather than pulled flat and tight. This changes only how the "
            "top is WORN — its actual length, cut and fit stay exactly as "
            "they are. "
        ),
    },
    "out": {
        "label": "빼서 입기",
        "ko": "상의를 빼서 입는다(밑단이 그대로 보인다)",
        "rule": (
            "TUCK — the top is worn untucked, hanging loose over the "
            "waistband so its full hem line and true length are visible "
            "all the way round. This changes only how the top is WORN — "
            "its actual length, cut and fit stay exactly as they are. "
        ),
    },
}


POSE_STYLE_RULE = (
    "Posture direction, following Korean fitting-cut convention: the "
    "stance must look loose and slightly slouched rather than upright and "
    "formal — hips pushed a little forward, knees soft and slightly bent, "
    "the shoulders loose and unforced while the shoulder line itself "
    "stays level and square, body weight settled unevenly on one "
    "leg. This faintly awkward, unposed stance is what makes the garment "
    "hang and drape naturally. Never a stiff, straight-backed runway pose. "
    "WHOLE-BODY COHERENCE — the pose belongs to the whole body, not just "
    "the arms. Whatever the hands are doing, the hips, legs and feet move "
    "with them as one connected stance: which leg carries the weight, how "
    "the hip line tilts, how much each knee bends, where each foot is "
    "planted and which way its toe points all follow the upper body, and "
    "they land differently in every cut. Keep the change restrained — the "
    "everyday way a real person stands while a friend photographs their "
    "outfit: the feet stay under the body and within about shoulder width, "
    "each foot on its own side of the body, side by side or one a little "
    "ahead of the other, "
    "and the shift stays small, half a step, a turned toe, one soft knee, "
    "a hip settled to one side. The model stays standing on the same spot "
    "of floor as before. "
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
    # 주의: "raw/poured concrete(노출 콘크리트)"라는 단어 자체가 모델에게
    # 거푸집 구멍(규칙적 O 무늬)을 그리게 만든다 — 금지 문구보다 표현 교체가 답.
    # 2026-08-20: "구멍 없이"처럼 아티팩트를 이름으로 부르는 부정문도 같은 이유로
    # 전부 걷어냈다(전역 규칙 포함). 이름을 부르면 그린다. 긍정 묘사만 남길 것.
    "concrete_wall": (
        "against a smooth, evenly finished concrete wall — one clean "
        "continuous surface with soft tonal variation and faint "
        "irregular weathering, troweled evenly from edge to edge, the "
        "wall running diagonally across the frame. Hard "
        "afternoon sunlight rakes across it, leaving a crisp shadow "
        "edge and giving the surface real depth"
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
        "across a pale warm-toned wall and a wooden floor. A sheer "
        "curtain softens one edge of the light — the warmth of the "
        "light and the wood is what fills the frame"
    ),
    # 주의: "empty white void 금지" 같은 부정 문구를 넣으면 모델이 흰 벽
    # 자체를 피해 빈티지 폐건물풍으로 튄다 — 흰 벽을 긍정문으로 고정할 것.
    "gallery": (
        "in a bright modern gallery — tall, freshly painted clean white "
        "walls with one or two large framed artworks hung sparely, a "
        "long low wooden or stone bench, a pale seamless floor and soft "
        "even top light. Calm, spacious and well kept, like a real "
        "exhibition room between shows — the artworks and bench give "
        "the space life"
    ),
    "architecture": (
        "beside clean modern architecture — a smooth seamless concrete "
        "column or wall with an even untextured finish, a run of stone "
        "steps, a deep doorway reveal or a simple façade. Strong "
        "architectural lines cut diagonally through the frame and soft "
        "daylight models the surfaces without harshness; every concrete "
        "surface stays perfectly smooth and evenly finished, its tone "
        "shifting only gently across the plane"
    ),
    "stairwell": (
        "on a quiet stairwell landing — a metal handrail, a smoothly "
        "painted wall, the diagonal line of the stair edge running "
        "through the frame, and a tall window or glass-block wall "
        "letting daylight pour in from the side so the corner feels "
        "bright, warm and calm"
    ),
    "park_path": (
        "on a quiet tree-lined path where dappled sunlight falls through "
        "the leaves onto a clean paved walkway. Soft layered greenery "
        "recedes behind the body, deep enough to give the frame air"
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
        "on a pleasant rooftop terrace — a low parapet wall, warm wooden "
        "deck tiles or pale pavers underfoot, a simple bench along one "
        "side, neighbouring rooftops softly visible in the distance "
        "under an open sky. Bright, airy and lived-in"
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
    # 주의: 예전의 "yellow tactile paving(점자블록)" 문구는 모델이 노란
    # 점 패턴을 화면 곳곳에 규칙적으로 찍어내는 부작용이 있어 뺐다.
    "stair_steps": (
        "on wide outdoor concrete stairs beside a smooth finished "
        "concrete wall — "
        "metal handrails, strong midday sun cutting hard diagonal shadows "
        "across the steps. The concrete surfaces are smooth and evenly "
        "finished, marked only by irregular natural stains and wear. "
        "Rough, real street architecture, exactly like "
        "a back street of a Korean city"
    ),
    "showroom": (
        "inside a real clothing-shop showroom corner — plain white walls "
        "with a few framed art prints hanging or leaning slightly "
        "off-centre, a black leather sofa or a slim chrome rack at the "
        "edge of the frame, flat bright indoor light like a quick phone "
        "photo taken inside the shop. Lived-in and unstaged, not a "
        "decorated set"
    ),
    # 스몰맨 레퍼런스(2026-08): 흰 벽 + 원목 가구 + 기대둔 액자 + 매거진.
    # 소품이 무드를 만드는 '꾸며진 편집샵 코너' — 빈 실내와 정반대.
    "styled_corner": (
        "inside a warmly styled select-shop or cafe corner — a clean "
        "white wall with a mid-century wooden sideboard or low shelf in "
        "warm cherry or walnut tone, a few framed fashion prints "
        "leaning casually against the wall or shelf, and a couple of "
        "design magazines or art books stacked nearby. Bright, soft "
        "daylight fills the space so the mood is clean and cosy, never "
        "dim or moody. The model stands close to the furniture so the "
        "corner reads as a curated shop display, and he may hold a "
        "rolled-up magazine loosely in one hand"
    ),
    # 미스터제이슨류 폰카 스냅: 한적한 주택가 골목 — 차도·차량 없이 담장과 대문만.
    "alley_snap": (
        "in a quiet Korean residential alley on a bright day — a low "
        "wall of warm brick or smoothly painted blocks, a simple metal "
        "or wooden gate, maybe a few shallow entrance steps, clean "
        "block paving underfoot and no cars or road markings in sight. "
        "Warm sunlight falls along the wall and leaves crisp shadows — "
        "the kind of calm back lane between houses where a seller "
        "quickly shoots a fitting cut"
    ),
    # 카페는 '안'이 아니라 입구/테라스에서 찍는 게 이 장르의 문법 — 테라스 좌석 버전.
    "cafe_terrace": (
        "on the outdoor terrace of a minimal cafe — one or two simple "
        "wooden or black metal chairs and a small table set against the "
        "cafe's glass front, warm daylight, clean pale paving, the "
        "interior softly visible through the window behind. The model "
        "stands beside the chairs or leans lightly on the table edge, "
        "as natural as a quick snap taken while grabbing a coffee"
    ),
    # 사용자 레퍼런스: 매끈한 콘크리트 카페/갤러리 파사드 + 통유리 + 철제 벤치.
    "concrete_cafe": (
        "just outside a modern minimalist concrete cafe or gallery — a "
        "smooth pale concrete facade with a floor-to-ceiling glass "
        "window, the interior softly visible through the glass, clean "
        "pale concrete paving underfoot, and a low black steel bench, "
        "planter or rope stanchion placed near the entrance. Soft "
        "diffused daylight with gentle shadows. The model stands close "
        "to the glass and facade, right by the entrance, as if he just "
        "stepped outside for a moment"
    ),
    # 카페/갤러리/샵은 '안'이 아니라 '입구 앞'에서 찍는다 — 사용자 레퍼런스 3컷 공통 문법:
    # 외벽+대형 유리, 유리 반사 너머로 실내가 은은하게, 바닥은 보도블록, 파사드는 사선.
    "storefront": (
        "outside on the street, right in front of a building's "
        "ground-level facade — a cafe, gallery or small shop — with a "
        "large glass window or glass door behind the model, the "
        "interior only faintly readable through soft reflections in the "
        "glass. Around the glass, a facade of smooth finished concrete, "
        "stone or "
        "pale plaster; underfoot, a block-paved or smooth concrete "
        "sidewalk, perhaps a low entrance step or doorway reveal. Shot "
        "from a slight angle so the facade edges and window frames run "
        "diagonally through the frame, in open natural daylight — the "
        "kind of quick sharp snap taken just outside the door. The model "
        "must stand right AT the entrance — beside the door frame, on "
        "the entrance step, or close against the facade — as if he just "
        "stepped out of the place, never floating in open space away "
        "from the building"
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
    # 랜덤은 '한 브랜드가 찍은 세트'처럼 보여야 하므로 무드가 검증된 것만 넣는다.
    # 들판/바닷가/골든아워는 원할 때 직접 선택하는 용도로만 남긴다.
    # roadside(차도·주차 차량이 보이는 거리 스냅)는 쇼핑몰 피팅컷 무드와
    # 어긋난다는 피드백(2026-08-17)으로 랜덤에서 제외 — 직접 선택은 가능.
    "studio", "seamless", "concrete_wall", "minimal_wall", "sunlit_room",
    "gallery", "architecture", "stairwell", "street_soft", "rooftop",
    "park_path", "lawn_park", "stair_steps", "showroom",
    "storefront", "concrete_cafe", "styled_corner",
    "alley_snap", "cafe_terrace",
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
            ("concrete_cafe", "모던 카페 앞"),
            ("cafe_terrace", "카페 테라스"),
            ("alley_snap", "골목 스냅"),
            ("roadside", "거리 스냅"),
            ("showroom", "쇼룸 스냅"),
        ],
    ),
    (
        "실내 · 미니멀",
        [
            ("seamless", "무봉제 배경 (브랜드형)"),
            ("minimal_wall", "미니멀 벽"),
            ("sunlit_room", "볕 드는 실내"),
            ("gallery", "갤러리"),
            ("styled_corner", "감성 우드 코너"),
        ],
    ),
    (
        "자연",
        [
            ("park_path", "공원 산책로"),
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
    "crowds. Concrete and stone surfaces read as one smooth, "
    "continuous, evenly finished material, its tone shifting only "
    "softly and irregularly across the surface, like troweled "
    "plaster. The place "
    "must feel warm and inviting, the way a Korean select shop stages "
    "its lookbook: warm material tones, generous natural light, and — "
    "only where such things naturally live — furnishings that form one "
    "coherent, integrated corner of the place, like furniture set "
    "against a wall with prints and books arranged on it. Never fill "
    "space by dropping a single random object such as a lone potted "
    "plant or stool beside the model; if the spot is naturally "
    "minimal, let warm light and tone carry the frame instead. The "
    "background "
    "must stay secondary so the product remains "
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

# 같은 프리셋으로 여러 장 뽑을 때, 컷마다 '같은 스타일의 다른 장소'가 나오게
# 하는 변주 규칙. 프리셋 묘사문이 동일하면 모델이 비슷한 장면만 그리는 문제의 해결책.
SCENE_VARIETY_RULE = (
    "This is cut #{n} of one product page. All cuts share one consistent "
    "style, but each cut must read as a DIFFERENT real spot of that "
    "style — as if the seller walked around the neighbourhood and "
    "photographed the product at several different places with the same "
    "vibe. Never repeat the same wall, building, window or framing "
    "across cuts. The place should be attractive enough that a shopper "
    "glancing at it thinks the product belongs to a nice life. "
)

# index(컷 번호)에 따라 돌아가며 걸리는 구체적 변주 축.
SCENE_VARIETY = [
    "",
    "For THIS cut, pick a different specific place in the same style: "
    "change the dominant surface material and its colour — for example "
    "smooth grey concrete becomes warm beige stone, pale brick, dark "
    "charcoal panelling or softly painted plaster — while keeping the "
    "described mood intact. ",
    "For THIS cut, shift the light: choose a different time of day and "
    "colour temperature — cool overcast, bright open shade, or low warm "
    "late-afternoon sun — with the shadows changing to match. ",
    "For THIS cut, change the camera's relationship to the place: let "
    "the main architectural line run across the opposite diagonal, or "
    "stand closer to or further from the backdrop so a different amount "
    "of ground and depth shows. ",
    "For THIS cut, vary the supporting elements: a different style of "
    "door, window, bench, planter, step or railing that would naturally "
    "belong to such a place — still calm, minimal and believable. ",
]

# 누끼(상품 단독) 컷을 함께 올린 경우, '표면 디테일'의 기준으로만 삼는다.
# 주의: 예전 문구("두 사진이 다르면 누끼컷을 따르라")는 모델이 핏·실루엣·
# 나머지 착장까지 누끼컷 기준으로 새로 그려버리는 부작용이 있었다.
DETAIL_RULE = (
    "TWO images are supplied. The FIRST is the worn fitting cut — it is "
    "the MASTER reference for the whole image: the complete outfit, and "
    "how the garment actually FITS the body — its silhouette, looseness, "
    "length and proportions on the body all follow the FIRST photo "
    "exactly. The SECOND is a clean cut-out product shot of the exact "
    "item being sold — use it ONLY to correct fine SURFACE details of "
    "that one item: its true colour and shade, fabric texture, print, "
    "graphics, lettering, trims and stitching. The cut-out must NEVER "
    "override the garment's fit, silhouette, length or how it drapes on "
    "the body, and must never change, restyle or replace any OTHER "
    "garment, footwear or accessory in the outfit — everything except "
    "those surface details stays exactly as in the FIRST photo. "
)

# 코디를 새로 짜는 경우(코디 스타일이 '그대로'가 아닐 때)의 누끼 규칙.
# 위 DETAIL_RULE 은 "나머지 착장도 첫 사진 그대로"라고 못박기 때문에 그대로 쓰면
# 코디 지시와 정면으로 충돌한다 — 파는 상품만 잠그고 나머지는 풀어준다.
DETAIL_RULE_RESTYLE = (
    "TWO images are supplied. The FIRST is the worn fitting cut — it "
    "shows how the garment being sold actually FITS the body: its "
    "silhouette, looseness, length and proportions on the body all follow "
    "the FIRST photo exactly. The SECOND is a clean cut-out product shot "
    "of that same item — read it closely and treat it as the truth for "
    "the item's fine SURFACE detail: its exact colour and shade, fabric "
    "texture and weave, print, graphics, lettering, trims, buttons, zips "
    "and stitching. The cut-out must NEVER override the garment's fit, "
    "silhouette or length. The REST of the outfit is deliberately being "
    "restyled and is described below — neither photo governs it. "
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
    "{model_rule}{framing} {shot_variety}{face_rule} {scene_block} "
    "{mood_rule}Set the "
    "pose to: {pose}. {pose_style}{shoulder_rule}{styling_rule}{tuck_rule}"
    "{accessory_rule}"
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


# ============================================================
# 상세페이지 기획 (스토리보드 생성) — /planner
# ChatGPT의 '기획 최적화 테크트리 5.0' 류 GPT를 의류 쇼핑몰 전용으로
# 재설계한 것. 출력 형식을 JSON 스키마로 고정해 매번 같은 구조의
# 기획안이 나오도록 한다 (대화형 GPT보다 일관성이 좋은 이유).
# ============================================================

# 텍스트 모델 — 앞에서부터 시도하고, 없거나 은퇴한 모델이면 다음 후보로 넘어간다.
# 전부 실패하면 계정에서 실제 사용 가능한 flash 계열을 조회해 이어서 시도한다.
PLAN_MODELS = [
    "gemini-3.6-flash", "gemini-3.5-flash-lite",
    "gemini-3.1-flash", "gemini-2.5-flash",
]


def _plan_model_candidates(client):
    """정적 후보 + 계정에서 조회한 flash 계열 텍스트 모델(최신순)."""
    candidates = list(PLAN_MODELS)
    try:
        discovered = []
        for m in client.models.list():
            name = (getattr(m, "name", "") or "").split("/")[-1]
            actions = getattr(m, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            if "flash" not in name:
                continue
            if any(x in name for x in
                   ("image", "live", "tts", "audio", "embedding", "preview")):
                continue
            if name not in candidates:
                discovered.append(name)
        candidates += sorted(discovered, reverse=True)
    except Exception:
        pass  # 목록 조회가 안 되면 정적 후보만으로 진행
    return candidates

# 설득 전략 — 상세페이지 전체를 끌고 가는 뼈대. 'auto'면 모델이 상품에
# 맞는 것을 직접 고른다.
PLAN_STRATEGIES = {
    "auto": {
        "label": "자동 추천",
        "desc": "",
    },
    "problem": {
        "label": "문제-해결형",
        "desc": (
            "고객이 옷에서 겪는 불편(핏이 안 맞음, 소재 불만, 금방 후줄근해짐 등)을 "
            "먼저 짚고, 이 상품이 그 해결책임을 논리적으로 보여주는 구성"
        ),
    },
    "emotional": {
        "label": "감성 · 무드형",
        "desc": (
            "브랜드 무드와 감성 카피가 중심. 사진의 분위기와 짧은 문장으로 "
            "'입고 싶다'는 기분을 만드는 구성"
        ),
    },
    "lifestyle": {
        "label": "라이프스타일형",
        "desc": (
            "이 옷을 입고 보내는 하루·상황(출근, 데이트, 주말 나들이)을 "
            "연출해서 사용 맥락으로 설득하는 구성"
        ),
    },
    "social": {
        "label": "리뷰 · 신뢰형",
        "desc": (
            "후기 인용 자리, 재구매·판매량 수치 자리, 디테일 검증 컷 등 "
            "사회적 증거와 신뢰 요소를 앞세우는 구성"
        ),
    },
    "compare": {
        "label": "비교 · 차별형",
        "desc": (
            "흔한 일반 제품과 이 상품의 차이를 비교 구조(일반 vs 이 제품)로 "
            "또렷하게 보여주는 구성"
        ),
    },
    "value": {
        "label": "구성 · 혜택형",
        "desc": (
            "가격 대비 가치, 세트 구성, 혜택을 전면에 내세워 '지금 사는 게 "
            "이득'임을 강조하는 구성"
        ),
    },
}

# 카피 톤 — 모든 섹션의 문장 말투를 통일한다.
PLAN_TONES = {
    "basic": {
        "label": "깔끔 · 신뢰",
        "desc": "군더더기 없는 깔끔한 존댓말, 차분하고 신뢰감 있게",
    },
    "emotional": {
        "label": "감성적",
        "desc": "부드럽고 감성적인 문장, 시적인 표현도 조금 섞어서",
    },
    "hip": {
        "label": "힙 · 캐주얼",
        "desc": "짧고 힙한 구어체, 친한 또래에게 말하듯 가볍게",
    },
    "premium": {
        "label": "프리미엄",
        "desc": "절제되고 고급스러운 톤, 짧은 문장, 형용사 남발 금지",
    },
}

PLAN_PROMPT = """당신은 한국 온라인 의류 쇼핑몰 상세페이지 기획 전문가입니다.
아래 상품 정보로, 모바일에서 위→아래로 스크롤하며 읽는 상세페이지의
스토리보드(기획안)를 작성하세요.

[상품 정보]
- 상품명: {name}
- 카테고리: {category}
- 특징·장점: {features}
- 소재·핏·디테일: {material}
- 타겟 고객: {target}
- 가격대: {price}

[작성 규칙]
- 설득 전략: {strategy_line}
- 카피 톤: {tone_desc}. 모든 섹션에서 이 톤을 유지한다.
- 섹션은 8~10개. 반드시 다음 흐름을 갖춘다: 첫 화면 후킹 → (전략에 맞는
  공감/문제 제기 또는 무드 연출) → 핵심 장점 소개 → 소재·디테일 →
  핏·사이즈 안내 → 코디 제안 → 착용컷 갤러리 → 구매 유도 마무리.
  배송·교환 안내는 맨 마지막.
- headline(헤드카피)은 15자 안팎으로 짧고 강하게. subcopy는 한 문장.
- body는 2~4문장. 문장 사이 줄바꿈은 \\n으로.
- image_guide에는 이 섹션에 어떤 사진을 어떻게 배치할지 구체적으로 쓴다.
  판매자는 AI 피팅컷 생성기로 얼굴 없는(목 아래 크롭) 착용컷을 만들 수
  있고, 쓸 수 있는 배경 프리셋은 다음과 같다: {preset_names}.
  섹션마다 어떤 프리셋 컷을 몇 장 쓰면 좋을지, 누끼컷·디테일 접사가
  필요한지까지 제안한다.
- 근거 없는 과장(최고, 1위, 유일 등)과 허위 후기 문구는 쓰지 않는다.
  리뷰 섹션은 '실제 후기를 넣을 자리'로 안내만 한다.
- 한국어로 쓴다.

[출력 형식]
아래 구조의 JSON 하나만 출력한다. 다른 텍스트는 절대 붙이지 않는다.
{{
  "one_liner": "이 상품을 한 줄로 정의하는 컨셉 문장",
  "strategy": {{"main": "사용한 설득 전략 이름", "reason": "이 상품에 이 전략을 쓴 이유 1~2문장"}},
  "sections": [
    {{
      "name": "섹션 이름",
      "goal": "이 섹션의 역할 한 줄",
      "headline": "헤드카피",
      "subcopy": "서브카피 한 문장",
      "body": "본문 카피",
      "image_guide": "이미지 연출·배치 가이드",
      "cta": "구매 유도 문구 (필요한 섹션에만, 없으면 빈 문자열)"
    }}
  ],
  "hashtags": ["상품 등록에 쓸 검색 태그 8~12개, # 없이"]
}}"""

# 프리셋 한글 라벨 목록 — 기획 프롬프트에서 이미지 가이드 제안에 쓴다.
PRESET_LABELS = ", ".join(
    label.replace(" ★", "")
    for _, items in BACKGROUND_GROUPS
    for key, label in items
    if key != "auto"
)


def _friendly_client_error(e):
    """구글 오류를 비개발자도 알아듣는 한국어 안내로 바꾼다."""
    msg = e.message or ""
    if "API key not valid" in msg or "API_KEY_INVALID" in msg:
        return ("API 키가 올바르지 않습니다. aistudio.google.com에서 발급한 "
                "'AIza'로 시작하는 키 전체를 공백 없이 붙여넣어주세요.")
    if e.code == 429 or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
        return ("API 사용량 한도에 걸렸습니다. 잠시(1분쯤) 후 다시 시도하거나, "
                "aistudio.google.com에서 결제를 등록하면 한도가 늘어납니다.")
    return f"요청이 거부되었습니다: {msg}"


def _extract_json(text):
    """모델 응답에서 JSON 객체를 꺼낸다. 코드펜스가 붙어도 견딘다."""
    try:
        return json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no json object found")
        return json.loads(text[start:end + 1])


@app.errorhandler(413)
def request_too_large(e):
    # 기본 413은 HTML이라 화면 JS가 못 읽는다 — JSON으로 통일
    return jsonify(error="이미지 용량이 너무 큽니다. 더 작은 사진으로 시도해주세요."), 413


@app.before_request
def require_login():
    """로그인 게이트: 추천인 코드로 입장한 세션만 통과시킨다."""
    if request.path.startswith(("/login", "/static", "/favicon.ico")):
        return None
    if session.get("shop"):
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="로그인이 필요합니다. 페이지를 새로고침해주세요."), 401
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        shop = (request.form.get("shop") or "").strip()
        if code != REFERRAL_CODE:
            error = "추천인 코드가 올바르지 않습니다."
        elif not shop:
            error = "상호명을 입력해주세요."
        else:
            session["shop"] = shop[:40]
            session.permanent = True
            nxt = request.args.get("next") or "/"
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = "/"
            return redirect(nxt)
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("shop", None)
    return redirect(url_for("login"))


@app.route("/")
def index():
    return render_template(
        "index.html",
        shop=session.get("shop"),
        quick_max=QUICK_MAX,
        poseset_max=POSESET_MAX,
        background_groups=BACKGROUND_GROUPS,
        random_pool=RANDOM_POOL,
        products=[(key, val["label"]) for key, val in PRODUCTS.items()],
        stylings=[(key, val["label"]) for key, val in STYLINGS.items()],
        tucks=[(key, val["label"]) for key, val in TUCKS.items()],
        ref_uses=[(key, val["label"]) for key, val in REF_USES.items()],
        shoulders=[(key, val["label"]) for key, val in SHOULDERS.items()],
    )


@app.route("/planner")
def planner():
    return render_template(
        "planner.html",
        shop=session.get("shop"),
        products=[(key, val["label"]) for key, val in PRODUCTS.items()],
        strategies=[(key, val["label"]) for key, val in PLAN_STRATEGIES.items()],
        tones=[(key, val["label"]) for key, val in PLAN_TONES.items()],
    )


@app.route("/api/plan", methods=["POST"])
def plan():
    api_key = (request.form.get("api_key") or "").strip()
    if not api_key:
        return jsonify(error="Google AI Studio API 키를 입력해주세요."), 400

    name = (request.form.get("name") or "").strip()
    features = (request.form.get("features") or "").strip()
    if not name or not features:
        return jsonify(error="상품명과 특징·장점은 꼭 입력해주세요."), 400

    category = request.form.get("category", "top")
    if category not in PRODUCTS:
        category = "top"

    material = (request.form.get("material") or "").strip() or "입력 없음"
    target = (request.form.get("target") or "").strip() or "20~30대 한국 남성"
    price = (request.form.get("price") or "").strip() or "입력 없음"

    strategy = request.form.get("strategy", "auto")
    if strategy not in PLAN_STRATEGIES:
        strategy = "auto"
    if strategy == "auto":
        candidates = " / ".join(
            f"{v['label']}({v['desc']})"
            for k, v in PLAN_STRATEGIES.items()
            if k != "auto"
        )
        strategy_line = (
            "다음 후보 중 이 상품에 가장 잘 맞는 전략을 직접 골라 적용한다: "
            + candidates
        )
    else:
        s = PLAN_STRATEGIES[strategy]
        strategy_line = f"반드시 '{s['label']}' 전략으로 구성한다 — {s['desc']}"

    tone = request.form.get("tone", "basic")
    if tone not in PLAN_TONES:
        tone = "basic"

    prompt = PLAN_PROMPT.format(
        name=name[:100],
        category=PRODUCTS[category]["label"],
        features=features[:1500],
        material=material[:800],
        target=target[:200],
        price=price[:100],
        strategy_line=strategy_line,
        tone_desc=PLAN_TONES[tone]["desc"],
        preset_names=PRESET_LABELS,
    )

    client = genai.Client(api_key=api_key)
    last_err = None
    for model in _plan_model_candidates(client):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.5,
                ),
            )
        except genai_errors.ClientError as e:
            # 모델이 없거나 은퇴했으면(no longer available) 다음 후보로 넘어간다.
            msg = (e.message or "").lower()
            if e.code == 404 or "no longer available" in msg or "not found" in msg:
                last_err = e
                continue
            status = 401 if e.code in (401, 403) else 400
            return jsonify(error=_friendly_client_error(e)), status
        except genai_errors.APIError as e:
            return jsonify(error=f"Gemini 요청 중 오류가 발생했습니다: {e.message}"), 502

        text = (response.text or "").strip()
        try:
            data = _extract_json(text)
        except ValueError:
            return jsonify(
                error="기획안 형식을 읽지 못했습니다. 한 번 더 시도해주세요."
            ), 502
        if not isinstance(data, dict) or not data.get("sections"):
            return jsonify(
                error="기획안이 비어 있습니다. 한 번 더 시도해주세요."
            ), 502
        return jsonify(plan=data, model=model)

    msg = last_err.message if last_err else "알 수 없는 오류"
    return jsonify(error=f"사용 가능한 텍스트 모델을 찾지 못했습니다: {msg}"), 502



# ============================================================
# AI 자동 코디 — 피팅컷(+누끼컷)을 보고 이 상품에 맞는 코디를 직접 짠다.
# ============================================================
#
# 왜 텍스트 모델을 한 번 더 거치나:
#   1) 이미지 모델에게 "알아서 코디해"라고 하면 컷마다 다른 옷을 입힌다.
#      여기서 한 번만 코디를 확정해 모든 컷에 같은 문장을 실어 보낸다.
#   2) 누끼컷의 디테일(색·소재·프린트)을 말로 정확히 읽어내는 일은
#      이미지 모델보다 텍스트(비전) 모델이 훨씬 잘한다.
#
# 인스타 참고에 대하여: 인스타그램은 로그인 없이 인기 피드를 읽을 수 없어서
# 실시간으로 긁어오지 못한다. 대신 (a) 아래 INSTA_FEED_DOCTRINE 으로 피드에서
# 실제로 잘 먹히는 코디 문법을 상시 반영하고, (b) 사용자가 인스타 스크린샷을
# 올리면 그 사진을 직접 보고 참고하게 한다.

INSTA_FEED_DOCTRINE = """인기 인스타 피드(한국 남성 패션 계정·쇼핑몰 스냅)에서 실제로 반응이 좋은 코디의 문법을 따른다:
- 색은 3색 이내. 무채색(블랙·그레이·아이보리·차콜)이나 흙색 계열을 바탕으로 깔고, 포인트 색은 많아야 하나.
- 실루엣은 대비로 잡는다. 상의가 오버핏이면 하의는 정돈된 라인으로, 둘 다 넉넉하면 발목과 신발에서 정리해준다.
- 기장 밸런스가 룩의 완성도를 좌우한다. 밑단이 신발에 살짝 닿아 한 번 접히는 정도가 피드에서 가장 잘 나온다.
- 신발이 분위기를 결정한다. 상품의 톤에 맞춰 로우탑 레더 스니커즈 / 스웨이드 러너 / 로퍼 / 워크부츠 중 하나로 확실히 정한다.
- 로고를 여러 개 겹치지 않는다. 브랜드보다 소재감과 톤으로 보여준다.
- 소품은 하나면 충분하다(볼캡, 얇은 목걸이, 크로스백, 시계 중 하나).
- 화보처럼 꾸민 룩이 아니라 '실제로 저렇게 입고 나갔다'는 인상이어야 한다.
- 계절이 읽혀야 한다. 상품의 소재와 두께에서 계절을 판단하고 거기에 맞는 레이어링을 짠다."""

# 참고 스크린샷을 어디까지 참고할지. 배경을 참고하면 배경 프리셋 대신
# 스크린샷에서 읽어낸 장소가 모든 컷의 배경이 된다.
REF_USES = {
    "codi": {
        "label": "코디만 참고",
        "task": (
            "이 사진에서는 **코디만** 참고한다 — 색 조합, 실루엣, 아이템 구성, "
            "무드. 장소·배경은 참고하지 않는다. 그대로 베끼지 말고 우리 상품에 "
            "맞게 새로 해석한다."
        ),
        "wants_scene": False,
    },
    "background": {
        "label": "배경만 참고",
        "task": (
            "이 사진에서는 **배경(촬영 장소)만** 참고한다 — 공간의 종류, 벽·바닥의 "
            "재질과 색, 빛의 방향과 시간대, 놓여 있는 가구·소품의 결. 이 사진에 "
            "나온 옷은 참고하지 않는다(코디는 우리 상품만 보고 짠다). 같은 장소를 "
            "복제하지 말고, '같은 결의 다른 실제 장소'로 해석한다."
        ),
        "wants_scene": True,
    },
    "both": {
        "label": "코디 + 배경 둘 다",
        "task": (
            "이 사진에서 **코디와 배경을 모두** 참고한다 — 색 조합·실루엣·아이템 "
            "구성, 그리고 공간의 재질·빛·소품의 결까지. 둘 다 그대로 베끼지 말고 "
            "우리 상품에 맞게 새로 해석한다."
        ),
        "wants_scene": True,
    },
}

# 여러 장을 뽑을 때 컷마다 카메라를 조금씩 다르게 놓는다.
# 카테고리별 {framing} 안에서 움직이는 '작은 차이'로만 쓴다 — 프레이밍 자체를
# 뒤엎는 지시를 넣으면 상품이 화면에서 작아지거나 얼굴이 들어온다.
SHOT_VARIETY = [
    "",
    "For THIS cut, step the camera a little closer than usual so the "
    "garment fills more of the frame and less of the surroundings shows. ",
    "For THIS cut, lower the camera slightly and tilt it up a touch, so "
    "the body line reads a little longer. ",
    "For THIS cut, move the camera a step to one side so the model sits "
    "off-centre and the place opens up on the other side of the frame. ",
    "For THIS cut, back off slightly and leave a little more room around "
    "the model, so the location reads more clearly behind him. ",
    "For THIS cut, turn the model a few degrees further off the camera "
    "axis so the shot reads as a three-quarter view. ",
]

# 참고 스크린샷에서 읽어낸 장소를 배경으로 쓸 때. 프리셋과 같은 품질 규칙을
# 그대로 태우려고 BACKGROUND_RULE_TEMPLATE 의 {setting} 자리에 꽂아 쓴다.
REF_SCENE_NOTE = (
    "This location came from a photo the seller admires. Build a real "
    "place of that same character rather than copying that photo frame "
    "for frame. "
)


COORDINATE_PROMPT = """너는 한국 남성 의류 쇼핑몰의 스타일리스트다. 판매자가 상품을 대충 걸치고 찍은 피팅컷을 보내왔고, 이 상품이 가장 잘 팔릴 코디를 짜야 한다.

[받은 사진]
{image_guide}

[1단계 — 상품을 정확히 읽는다]
판매 상품은 **{focus_ko}**({focus_en})다. 이것만 보고 다음을 확정한다:
종류와 핏(오버·레귤러·슬림), 기장, 정확한 색과 톤, 소재와 짜임, 프린트·자수·레터링의 유무와 내용, 포켓·단추·지퍼·스트링 같은 디테일, 그리고 소재 두께에서 읽히는 계절.
누끼컷이 함께 왔다면 색·소재·프린트는 반드시 누끼컷을 기준으로 판단한다(피팅컷은 조명 때문에 색이 틀어져 있다).

[2단계 — 코디를 짠다]
{focus_ko}는 절대 바꾸지 않는다. 판매 상품을 뺀 나머지 전부(같이 입는 다른 옷·아우터·이너, 신발, 양말, 모자·가방 같은 소품)를 새로 정한다.
{doctrine}
{tuck_line}{reference_line}{accessory_line}
모델은 180cm 79kg 근육질의 한국 남성이다. 남성복으로만 짠다.

[출력 형식]
아래 키를 가진 JSON 객체 하나만 출력한다. 설명이나 코드펜스를 붙이지 않는다.
- "product": 읽어낸 상품을 한국어 한 줄로. 예) "인디고 워시드 데님 셔츠, 오버핏, 가슴 포켓 2개, 두꺼운 코튼"
- "coordination": 짠 코디를 한국어 한 줄로. 판매자가 보고 바로 이해할 수 있게. 예) "화이트 헤비 코튼 티셔츠, 차콜 와이드 슬랙스, 화이트 레더 로우탑 스니커즈"
- "reason": 왜 이 코디인지 한국어 한 문장.
- "styling_en": 이미지 생성 모델에게 넘길 영어 구절. **명사구로만** 쓴다(문장·마침표·명령문 금지). "Restyle the rest of the outfit as ___" 의 빈칸에 그대로 들어간다. 판매 상품({focus_en})은 여기에 절대 포함하지 않는다. 예) "a plain white heavy-cotton tee, wide charcoal pleated trousers, and white leather low-top sneakers"
{scene_output}"""

COORDINATE_SCHEMA = {
    "type": "object",
    "properties": {
        "product": {"type": "string"},
        "coordination": {"type": "string"},
        "reason": {"type": "string"},
        "styling_en": {"type": "string"},
        "scene_ko": {"type": "string"},
        "scene_en": {"type": "string"},
    },
    "required": ["product", "coordination", "styling_en"],
}

# styling_en 이 명사구가 아니라 잔소리를 달고 오는 경우가 있어 길이를 자른다.
MAX_STYLING_DESC = 400
MAX_SCENE_DESC = 400


def _read_image_upload(field, label, required=False):
    """업로드 이미지 하나를 검사해서 바이트로. 문제가 있으면 (None, 오류메시지)."""
    f = request.files.get(field)
    if not f or not f.filename:
        if required:
            return None, f"{label}을(를) 선택해주세요."
        return None, None
    if f.mimetype not in ALLOWED_CONTENT_TYPES:
        return None, f"{label}은(는) PNG, JPEG, WEBP만 지원합니다."
    return f.read(), None


@app.route("/api/coordinate", methods=["POST"])
def coordinate():
    """피팅컷(+누끼컷·인스타 레퍼런스)을 읽고 코디를 한 벌 짜서 돌려준다.

    화면은 컷을 만들기 **전에 이 엔드포인트를 딱 한 번** 부르고,
    받은 styling_en 을 모든 컷 요청에 실어 보낸다. 그래야 10장이 같은 코디로 나온다.
    """
    api_key = (request.form.get("api_key") or "").strip()
    if not api_key:
        return jsonify(error="Google AI Studio API 키를 입력해주세요."), 400

    image_bytes, err = _read_image_upload("image", "피팅컷", required=True)
    if err:
        return jsonify(error=err), 400
    detail_bytes, err = _read_image_upload("detail_image", "누끼컷")
    if err:
        return jsonify(error=err), 400
    insta_bytes, err = _read_image_upload("insta_image", "인스타 레퍼런스")
    if err:
        return jsonify(error=err), 400

    product_type = request.form.get("product_type", "top")
    if product_type not in PRODUCTS:
        product_type = "top"
    product = PRODUCTS[product_type]

    # 사진이 몇 장 가는지에 따라 '몇 번째 사진이 무엇인지'를 정확히 알려준다.
    guides = ["첫 번째 사진: 판매자가 상품을 대충 입고 찍은 피팅컷."]
    if detail_bytes:
        guides.append(
            "두 번째 사진: 같은 상품의 누끼컷(상품 단독). "
            "색·소재·프린트·부자재의 기준은 이 사진이다."
        )
    if insta_bytes:
        guides.append(
            f"{'세' if detail_bytes else '두'} 번째 사진: 판매자가 참고하라고 준 "
            "인스타그램 피드 스크린샷. 이 사진의 코디 무드·색 조합·실루엣을 "
            "참고하되 그대로 베끼지는 말고, 우리 상품에 맞게 새로 해석한다."
        )
    else:
        guides.append("누끼컷이 없으면 피팅컷만으로 최대한 정확히 판단한다."
                      if not detail_bytes else "")

    accessories = (request.form.get("accessories") or "").strip()
    accessory_line = (
        "\n판매자가 이건 꼭 넣어달라고 했다: "
        + accessories[:200]
        + ". 코디에 자연스럽게 포함한다.\n"
        if accessories else ""
    )
    tuck = request.form.get("tuck", "keep")
    if tuck not in TUCKS:
        tuck = "keep"
    tuck_line = (
        "이 컷에서는 " + TUCKS[tuck]["ko"]
        + ". 그에 맞게 하의의 허리 라인과 벨트, 기장 밸런스까지 고려해 코디한다."
        + "\n"
        if TUCKS[tuck]["ko"] else ""
    )
    ref_use = request.form.get("ref_use", "both")
    if ref_use not in REF_USES:
        ref_use = "both"
    # 참고 사진이 없으면 참고 방식도 의미가 없다.
    wants_scene = bool(insta_bytes) and REF_USES[ref_use]["wants_scene"]
    reference_line = (
        REF_USES[ref_use]["task"] + "\n" if insta_bytes else ""
    )
    # 배경까지 참고하는 경우에만 장소를 함께 받아온다.
    SCENE_OUTPUT = (
        '- "scene_ko": 참고 사진에서 읽어낸 촬영 장소를 한국어 한 줄로. '
        '예) "흰 벽과 원목 선반이 있는 편집샵 코너, 큰 창에서 들어오는 오후 자연광"\n'
        '- "scene_en": 그 장소를 영어 구절로. 반드시 "in a ..." 또는 "on a ..." 처럼 '
        '전치사로 시작하는 **구절**로 쓴다(문장 금지). '
        '예) in a warm select-shop corner with a white wall, a light oak shelf '
        'and afternoon daylight from a large window'
    )
    scene_output = SCENE_OUTPUT if wants_scene else ""

    prompt = COORDINATE_PROMPT.format(
        image_guide="\n".join(g for g in guides if g),
        focus_ko=product["label"],
        focus_en=product["focus"],
        doctrine=INSTA_FEED_DOCTRINE,
        tuck_line=tuck_line,
        reference_line=reference_line,
        scene_output=scene_output,
        accessory_line=accessory_line,
    )

    try:
        images = [_load_shrunk(image_bytes, 1024)]
        if detail_bytes:
            images.append(_load_shrunk(detail_bytes, 1024))
        if insta_bytes:
            images.append(_load_shrunk(insta_bytes, 1024))
    except (OSError, ValueError):
        return jsonify(error="이미지 파일을 읽지 못했습니다. 다른 파일로 시도해주세요."), 400

    client = genai.Client(api_key=api_key)
    last_err = None
    for model in _plan_model_candidates(client):
        try:
            response = client.models.generate_content(
                model=model,
                contents=[prompt, *images],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=COORDINATE_SCHEMA,
                    # 매번 똑같은 코디만 나오면 '다시 짜기'가 의미가 없다.
                    temperature=1.0,
                ),
            )
        except genai_errors.ClientError as e:
            msg = (e.message or "").lower()
            if e.code == 404 or "no longer available" in msg or "not found" in msg:
                last_err = e
                continue
            status = 401 if e.code in (401, 403) else 400
            return jsonify(error=_friendly_client_error(e)), status
        except genai_errors.APIError as e:
            return jsonify(error=f"Gemini 요청 중 오류가 발생했습니다: {e.message}"), 502

        try:
            data = _extract_json((response.text or "").strip())
        except ValueError:
            return jsonify(
                error="코디 결과를 읽지 못했습니다. 한 번 더 시도해주세요."
            ), 502
        styling_en = (data or {}).get("styling_en", "").strip() if isinstance(data, dict) else ""
        if not styling_en:
            return jsonify(
                error="코디가 비어 있습니다. 한 번 더 시도해주세요."
            ), 502
        scene_en = (data.get("scene_en") or "").strip() if wants_scene else ""
        return jsonify(
            coordination={
                "product": (data.get("product") or "").strip(),
                "coordination": (data.get("coordination") or "").strip(),
                "reason": (data.get("reason") or "").strip(),
                "styling_en": styling_en[:MAX_STYLING_DESC],
                "scene_ko": ((data.get("scene_ko") or "").strip()
                             if wants_scene else ""),
                "scene_en": scene_en[:MAX_SCENE_DESC],
            },
            model=model,
        )

    msg = last_err.message if last_err else "알 수 없는 오류"
    return jsonify(error=f"사용 가능한 텍스트 모델을 찾지 못했습니다: {msg}"), 502


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
    restyling = False    # 나머지 착장을 새로 입히는 중인가 (누끼 규칙이 갈린다)
    if mode == "poseset" or keep_scene:
        # 보낸 사진을 그대로 두고 서 있는 포즈만 바꾼다. 배경/코디/모델은 건드리지 않는다.
        count = max(1, min(count, POSESET_MAX))
        pose_list = STANDING_POSES
        scene_block = KEEP_SCENE_RULE
        template = PROMPT_SAME_SCENE
        # 올린 사진에 얼굴이 있으면 그 얼굴을 그대로 지키고,
        # 없으면 지금까지처럼 목 아래 크롭을 유지한다.
        face_rule = FACE_ADAPTIVE_RULE
    else:
        # "auto"는 BACKGROUNDS의 항목이 아니라 '컷마다 랜덤'을 뜻하는 특수값이다.
        background = request.form.get("background", "studio")
        if background != "auto" and background not in BACKGROUNDS:
            background = "studio"
        count = max(1, min(count, QUICK_MAX))
        pose_list = POSES
        template = PROMPT_NEW_SCENE
        # 새 장면을 만드는 모드는 언제나 목 아래 크롭.
        face_rule = FACE_RULE

        # 화면이 '랜덤'을 직접 풀어서 컷마다 배경 키를 지정해 보낼 때 붙는 플래그.
        # 배경이 랜덤으로 골라졌으니 옷에 어울리게 연출하라는 규칙을 유지한다.
        garment_aware = request.form.get("garment_aware") == "1"

        # 참고 스크린샷에서 배경까지 읽어온 경우 — 프리셋을 밀어내고 이 장소를 쓴다.
        # 컷마다 SCENE_VARIETY 변주 축을 얹어 같은 결의 '다른 장소'가 되게 한다.
        scene_desc = (request.form.get("scene_desc") or "").strip()[:MAX_SCENE_DESC]

        if scene_desc:
            scene_block = (
                BACKGROUND_RULE_TEMPLATE.format(setting=scene_desc)
                + " "
                + REF_SCENE_NOTE
                + LOCATION_RULE_VARY
                + " "
                + SCENE_VARIETY[index % len(SCENE_VARIETY)]
            )
        elif background == "auto":
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
            # 사용자가 프리셋 하나를 직접 골라 여러 장 뽑는 경우:
            # 컷 번호에 따라 '같은 스타일의 다른 장소' 변주를 강제한다.
            if not garment_aware:
                scene_block += (
                    " "
                    + SCENE_VARIETY_RULE.format(n=index + 1)
                    + SCENE_VARIETY[index % len(SCENE_VARIETY)]
                )
            else:
                # 랜덤 배정은 컷마다 프리셋이 다르지만, 같은 프리셋이 매번
                # 거의 똑같은 장면으로 렌더되는 문제가 있어 변주 축만 얹는다.
                scene_block += " " + SCENE_VARIETY[index % len(SCENE_VARIETY)]

        styling = request.form.get("styling", "keep")
        if styling not in STYLINGS:
            styling = "keep"
        desc = STYLINGS[styling]["desc"]
        if styling == "auto":
            # AI 자동 코디: 화면이 /api/coordinate 로 미리 받아온 코디 문장이
            # 컷마다 실려 온다(모든 컷이 같은 문장이라 착장이 통일된다).
            # 비어 있으면 '그대로'로 안전하게 떨어진다.
            desc = (request.form.get("styling_desc") or "").strip()[:MAX_STYLING_DESC]
        restyling = bool(desc)
        # 어깨 라인은 화면의 버튼이 정한다. 기본은 올린 사진 그대로.
        shoulder = request.form.get("shoulder", "keep")
        if shoulder not in SHOULDERS:
            shoulder = "keep"
        extra["shoulder_rule"] = SHOULDERS[shoulder]["rule"]
        # 여러 장 뽑을 때 컷마다 카메라를 조금씩 다르게. 포즈·배경 디테일은
        # 이미 컷 번호로 갈리므로 여기서는 구도 축만 더한다.
        extra["shot_variety"] = (
            SHOT_VARIETY[index % len(SHOT_VARIETY)] if styling == "auto" else ""
        )

        tuck = request.form.get("tuck", "keep")
        if tuck not in TUCKS:
            tuck = "keep"
        extra["tuck_rule"] = TUCKS[tuck]["rule"]

        extra["styling_rule"] = (
            STYLING_RULE_TEMPLATE.format(focus=product["focus"], desc=desc)
            if desc
            else OUTFIT_KEEP_RULE
        )
        extra["model_rule"] = MODEL_RULE
        extra["realism_rule"] = REALISM_RULE
        # 상의는 사선 벽면·질감 위주의 무드를 한 겹 더 얹는다.
        # 단, 폰카 스냅 계열 배경은 자기만의 빛/장소 문법이 있어서 얹지 않는다.
        snap_style = background in (
            "lawn_park", "stair_steps", "showroom", "roadside", "storefront",
            "concrete_cafe", "styled_corner",
        )
        extra["mood_rule"] = (
            TOP_MOOD_RULE
            if product_type == "top" and not snap_style and not scene_desc
            else ""
        )

    # 누끼컷 규칙은 코디를 새로 짜는지에 따라 갈린다. 기본 DETAIL_RULE 은
    # "나머지 착장도 첫 사진 그대로"라고 못박기 때문에, 코디를 새로 입히는
    # 경우에 그대로 쓰면 코디 지시와 정면으로 충돌한다.
    if not detail_bytes:
        extra["detail_rule"] = ""
    else:
        extra["detail_rule"] = DETAIL_RULE_RESTYLE if restyling else DETAIL_RULE

    accessories = (request.form.get("accessories") or "").strip()
    accessory_rule = (
        ACCESSORY_RULE_TEMPLATE.format(accessories=accessories[:200])
        if accessories
        else ""
    )

    try:
        contents_images = [_load_shrunk(image_bytes)]
        if detail_bytes:
            contents_images.append(_load_shrunk(detail_bytes))
    except (OSError, ValueError):
        return jsonify(error="이미지 파일을 읽지 못했습니다. 다른 파일로 시도해주세요."), 400

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=IMAGE_TIMEOUT_MS),
    )

    results = []
    warning = None

    try:
        for i in range(count):
            pose = pose_list[(index + i) % len(pose_list)]
            block = scene_blocks[i] if scene_blocks else scene_block
            prompt = template.format(
                focus=product["focus"],
                framing=product["framing"],
                face_rule=face_rule,
                garment_lock=GARMENT_LOCK_RULE.format(focus=product["focus"]),
                scene_block=block,
                pose_style=POSE_STYLE_RULE,
                accessory_rule=accessory_rule,
                pose=pose,
                **extra,
            )
            # 모델이 가끔 이미지 없이 텍스트만 돌려준다(안전필터/일시 오류).
            # 그 경우 한 번만 즉시 재시도하면 대부분 성공한다.
            image_data_url = None
            for attempt in range(2):
                response = _generate_image_with_fallback(
                    client, prompt, contents_images
                )
                # 안전필터 등으로 응답이 아예 비면 parts가 None이라 그대로 돌면 500이 난다
                for part in response.parts or []:
                    if part.inline_data:
                        b64 = base64.b64encode(part.inline_data.data).decode()
                        image_data_url = f"data:image/png;base64,{b64}"
                if image_data_url:
                    break
            if image_data_url:
                results.append({"image": image_data_url})
    except ImageModelUnavailable as e:
        if not results:
            return jsonify(error=str(e)), 502
        warning = str(e)
    except genai_errors.ClientError as e:
        # 이미 만들어진 컷이 있다면 버리지 않고 경고와 함께 돌려준다.
        if not results:
            status = 401 if e.code in (401, 403) else 400
            return jsonify(error=_friendly_client_error(e)), status
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
