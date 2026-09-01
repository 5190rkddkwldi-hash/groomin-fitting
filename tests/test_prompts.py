# -*- coding: utf-8 -*-
"""프롬프트·설정이 지켜야 할 규칙들 (네트워크 없이 검사).

여기 있는 검사는 대부분 **실제로 사고가 났던 지점**이다.
프롬프트 문구를 고칠 때 이 파일이 빨간불이면, 십중팔구 예전 버그를 되살린 것이다.
자세한 배경은 CONTRIBUTING.md 의 "프롬프트를 고칠 때" 항목을 읽어보라.
"""
import re

import app as srv


# ---------------------------------------------------------------- 배경 프리셋

def test_랜덤_풀은_전부_실제_프리셋이다():
    unknown = [k for k in srv.RANDOM_POOL if k not in srv.BACKGROUNDS]
    assert unknown == [], f"BACKGROUNDS 에 없는 키가 랜덤 풀에 있음: {unknown}"


def test_랜덤_풀에_중복이_없다():
    assert len(srv.RANDOM_POOL) == len(set(srv.RANDOM_POOL))


def _grouped_keys():
    """BACKGROUND_GROUPS 는 (그룹이름, [(키, 라벨), ...]) 모양이다."""
    return {key for _label, items in srv.BACKGROUND_GROUPS for key, _t in items}


def test_배경_그룹의_키도_전부_실제_프리셋이다():
    # "auto"는 프리셋이 아니라 '컷마다 랜덤'을 뜻하는 특수값이다.
    unknown = {k for k in _grouped_keys() if k != "auto" and k not in srv.BACKGROUNDS}
    assert unknown == set(), f"BACKGROUNDS 에 없는 키가 목록에 있음: {unknown}"


def test_모든_프리셋이_어느_그룹엔가_들어_있다():
    """화면의 배경 고르기 목록은 그룹으로 그려진다. 빠지면 고를 수 없다."""
    missing = set(srv.BACKGROUNDS) - _grouped_keys()
    assert missing == set(), f"어느 그룹에도 안 들어간 프리셋: {missing}"


def test_차도_배경은_랜덤_풀에서_빠져_있다():
    """roadside(주차 차량·차선)는 랜덤으로 나오면 뜬금없어서 직접 선택 전용이다."""
    assert "roadside" not in srv.RANDOM_POOL
    assert "roadside" in srv.BACKGROUNDS


# ---------------------------------------------------------------- 금지 문구

# 프롬프트에 절대 들어가면 안 되는 표현들.
#
# ★ 핵심 원칙: 아티팩트를 **이름으로 부르면 그린다.**
#   "구멍 없이", "점무늬 금지" 같은 부정문도 마찬가지다. 원하는 표면을
#   긍정문으로 묘사하는 것만이 통한다. (2026-08-20 재발 확인)
BANNED = {
    # 그 단어 자체가 거푸집 자국을 부른다
    "form-tie": "콘크리트에 규칙적인 점 구멍이 생긴다",
    "raw concrete": "raw/poured 라는 단어가 거친 거푸집 자국을 부른다",
    "poured concrete": "raw/poured 라는 단어가 거친 거푸집 자국을 부른다",
    # 아티팩트를 이름으로 부르는 순간(설령 금지문이어도) 그려진다
    "circular hole": "구멍을 이름으로 부르면 그린다 — 긍정 묘사로 바꿀 것",
    "round hole": "구멍을 이름으로 부르면 그린다 — 긍정 묘사로 바꿀 것",
    "holes or dot": "구멍을 이름으로 부르면 그린다 — 긍정 묘사로 바꿀 것",
    "dots or studs": "점무늬를 이름으로 부르면 그린다 — 긍정 묘사로 바꿀 것",
    "pockmark": "구멍을 이름으로 부르면 그린다 — 긍정 묘사로 바꿀 것",
    # 점자블록 → 규칙적인 점 패턴
    "tactile paving": "노란 점자블록의 점 패턴이 아티팩트로 번진다",
    # 부정문은 그 단어 자체를 피하게 만든다
    "never an empty white": "부정문이라 흰 벽 자체를 피하게 만든다",
    "mostly plain": "배경 연출을 통째로 억눌러 화면이 휑해진다",
}


def _prompt_strings():
    """실제로 모델에게 보내는 문자열만 모은다 (주석·한글 설명은 제외)."""
    out = {("BACKGROUNDS:" + k): v for k, v in srv.BACKGROUNDS.items()}
    for name in ("FACE_RULE", "GARMENT_LOCK_RULE", "OUTFIT_KEEP_RULE", "MODEL_RULE",
                 "REALISM_RULE", "TOP_MOOD_RULE", "BACKGROUND_RULE_TEMPLATE",
                 "LOCATION_RULE_VARY", "KEEP_SCENE_RULE", "SCENE_VARIETY_RULE",
                 "DETAIL_RULE", "GARMENT_AWARE_RULE", "POSE_STYLE_RULE",
                 "FACE_ADAPTIVE_RULE",
                 "PROMPT_NEW_SCENE", "PROMPT_SAME_SCENE"):
        out[name] = getattr(srv, name)
    for i, v in enumerate(srv.SCENE_VARIETY):
        out["SCENE_VARIETY[%d]" % i] = v
    return out


def _banned_hits():
    hits = []
    for where, text in _prompt_strings().items():
        low = str(text).lower()
        for bad in BANNED:
            if bad in low:
                hits.append((where.split(":")[-1], bad))
    return hits


def test_금지된_문구가_하나도_없다():
    hits = _banned_hits()
    assert hits == [], "되살아난 금지 문구: " + "; ".join(
        f"{w} → {b} ({BANNED[b]})" for w, b in hits)


# 콘크리트가 화면을 크게 차지하는 자리들. 스쳐 지나가는 언급(예: 해안 산책로)은 제외.
CONCRETE_SURFACES = ("concrete wall", "concrete facade", "concrete column",
                     "concrete floor", "concrete paving", "concrete sidewalk",
                     "concrete stairs")


def test_콘크리트_배경은_매끈함을_긍정문으로_말한다():
    """구멍을 '금지'하는 대신, 매끈한 표면을 직접 묘사해야 한다."""
    good = ("smooth", "evenly finished", "troweled", "seamless", "continuous")
    for key, text in srv.BACKGROUNDS.items():
        low = text.lower()
        if not any(surface in low for surface in CONCRETE_SURFACES):
            continue
        assert any(g in low for g in good), (
            f"{key}: 콘크리트가 화면을 크게 차지하는데 매끈하다는 긍정 묘사가 없다")


def test_전역_배경_규칙에도_아티팩트_이름이_없다():
    """모든 컷에 붙는 규칙이라 여기에 단어가 들어가면 전부 오염된다."""
    low = srv.BACKGROUND_RULE_TEMPLATE.lower()
    for word in ("hole", "stud", "dot"):
        assert word not in low, f"전역 규칙에 '{word}' 가 들어 있다"


def test_하위_이미지_모델로_몰래_내려가지_않는다():
    """화질이 우선이라는 방침. lite 계열은 폴백 후보에 넣지 않는다."""
    assert srv.IMAGE_MODELS[0] == "gemini-3.1-flash-image"
    assert not any("lite" in m for m in srv.IMAGE_MODELS)


# ---------------------------------------------------------------- 불변 규칙

def test_얼굴_노출_금지_규칙이_살아_있다():
    assert "{face_rule}" in srv.PROMPT_NEW_SCENE
    assert "{face_rule}" in srv.PROMPT_SAME_SCENE
    assert re.search(r"\bchin\b|\bneck\b|\bface\b", srv.FACE_RULE, re.I)


def test_상품_잠금_규칙이_두_템플릿_모두에_있다():
    assert "{garment_lock}" in srv.PROMPT_NEW_SCENE
    assert "{garment_lock}" in srv.PROMPT_SAME_SCENE


def test_같은_장면_모드에는_구도_지시를_넣지_않는다():
    """새 장면용 구도/장신구 지시가 섞이면 배경이 바뀌어버린다 (실사용 버그)."""
    assert "{framing}" not in srv.PROMPT_SAME_SCENE
    assert "{accessory_rule}" not in srv.PROMPT_SAME_SCENE
    assert "{mood_rule}" not in srv.PROMPT_SAME_SCENE


def test_같은_장면_모드에_최종_확인_문장이_있다():
    assert "FINAL CHECK" in srv.PROMPT_SAME_SCENE


# ---------------------------------------------------------------- 조립 가능성

def _fill(template, **over):
    product = srv.PRODUCTS["top"]
    values = dict(
        detail_rule="", focus=product["focus"], framing=product["framing"],
        garment_lock=srv.GARMENT_LOCK_RULE.format(focus=product["focus"]),
        model_rule=srv.MODEL_RULE, face_rule=srv.FACE_RULE,
        scene_block="SCENE", mood_rule="", pose="POSE",
        pose_style=srv.POSE_STYLE_RULE, styling_rule="", accessory_rule="",
        shoulder_rule="", tuck_rule="", shot_variety="",
        realism_rule=srv.REALISM_RULE,
    )
    values.update(over)
    return template.format(**values)


def test_새_장면_프롬프트가_빈칸_없이_조립된다():
    out = _fill(srv.PROMPT_NEW_SCENE)
    assert "{" not in out and "}" not in out
    assert len(out) > 400


def test_같은_장면_프롬프트가_빈칸_없이_조립된다():
    out = srv.PROMPT_SAME_SCENE.format(
        detail_rule="", focus=srv.PRODUCTS["top"]["focus"],
        scene_block=srv.KEEP_SCENE_RULE, pose="POSE",
        pose_style=srv.POSE_STYLE_RULE, face_rule=srv.FACE_RULE,
        garment_lock=srv.GARMENT_LOCK_RULE.format(focus=srv.PRODUCTS["top"]["focus"]),
    )
    assert "{" not in out and "}" not in out


def test_모든_상품_종류로_조립된다():
    for key, product in srv.PRODUCTS.items():
        out = _fill(srv.PROMPT_NEW_SCENE, focus=product["focus"],
                    framing=product["framing"],
                    garment_lock=srv.GARMENT_LOCK_RULE.format(focus=product["focus"]))
        assert "{" not in out, f"{key} 조립 실패"


def test_모든_배경_프리셋으로_장면_블록이_조립된다():
    for key, setting in srv.BACKGROUNDS.items():
        block = srv.BACKGROUND_RULE_TEMPLATE.format(setting=setting)
        assert "{" not in block, f"{key} 배경 문구에 남은 자리표시자가 있음"
        assert len(block) > 60


def test_스타일링_선택지가_전부_조립된다():
    for key, styling in srv.STYLINGS.items():
        desc = styling["desc"]
        rule = (srv.STYLING_RULE_TEMPLATE.format(focus="top", desc=desc)
                if desc else srv.OUTFIT_KEEP_RULE)
        assert "{" not in rule, f"{key} 스타일링 조립 실패"


def test_그대로_두기_스타일링은_착장_잠금_문구를_쓴다():
    """빈 문자열이면 모델이 신발·바지를 마음대로 지어낸다 (실제로 났던 문제)."""
    assert srv.STYLINGS["keep"]["desc"] == ""
    assert len(srv.OUTFIT_KEEP_RULE) > 40


def test_장면_변주_축이_비어_있지_않다():
    # 0번은 '변주 없음'이라 일부러 빈 문자열이다.
    assert len(srv.SCENE_VARIETY) >= 3
    assert srv.SCENE_VARIETY[0] == ""
    assert all(len(v) > 20 for v in srv.SCENE_VARIETY[1:])
    assert "{n}" in srv.SCENE_VARIETY_RULE


def test_포즈_목록이_충분하다():
    assert len(srv.POSES) >= 8
    assert len(srv.STANDING_POSES) >= srv.POSESET_MAX


# 2026-09-01 사용자 요청: "기본 포즈(한쪽 주머니에 손)는 기본적으로 나오게".
# 두 모드 모두 1번 컷(index 0)이 이 포즈여야 하므로 목록 맨 앞에 고정한다.
def test_기본_포즈가_두_모드_모두_첫_컷이다():
    assert srv.POSES[0] == srv.BASE_POSE
    assert srv.STANDING_POSES[0] == srv.BASE_POSE
    low = srv.BASE_POSE.lower()
    assert "one hand slipped casually into a" in low and "pocket" in low
    # 나머지 포즈가 기본 포즈를 그대로 반복하지는 않아야 한다.
    assert srv.POSES.count(srv.BASE_POSE) == 1
    assert srv.STANDING_POSES.count(srv.BASE_POSE) == 1


def test_포즈_모음은_기본_포즈를_빼고도_변주가_충분하다():
    """POSESET_MAX 장을 뽑을 때 목록이 한 바퀴 돌지 않아야 컷이 안 겹친다."""
    assert len(srv.STANDING_POSES) >= srv.POSESET_MAX


# 2026-09-01: 상체(팔·손)만 묘사돼 있어 하체가 컷마다 거의 고정되던 문제.
# 12종 전부가 자기 팔 동작에 맞물리는 하체 문구를 갖고 있어야 한다.
LOWER_BODY_WORDS = ("leg", "knee", "foot", "feet", "heel", "toe", "hip", "step")


def test_포즈마다_하체_지시가_있다():
    빠진것 = [
        i for i, pose in enumerate(srv.STANDING_POSES)
        if not any(w in pose.lower() for w in LOWER_BODY_WORDS)
    ]
    assert 빠진것 == [], f"하체 문구가 없는 포즈 번호: {빠진것}"


def test_하체_지시가_포즈마다_다르다():
    """전부 'weight on one leg' 한 문장이면 결국 같은 하체가 나온다."""
    조각 = []
    for pose in srv.STANDING_POSES:
        조각.append(frozenset(
            w for w in LOWER_BODY_WORDS if w in pose.lower()
        ))
    assert len(set(조각)) >= 6, "하체 묘사가 서로 너무 비슷하다"


def test_다리를_꼬는_포즈는_없다():
    """앞으로 다리를 꼬는 동작은 과해 보여서 뺐다 (2026-09-01 사용자 피드백)."""
    다리교차 = ("legs cross", "cross at the ankle", "crossed at the ankle",
              "ankle", "one foot behind the other leg")
    for i, pose in enumerate(srv.STANDING_POSES):
        low = pose.lower()
        걸린것 = [w for w in 다리교차 if w in low]
        assert 걸린것 == [], f"{i}번 포즈에 다리 교차가 되살아났다: {걸린것}"
    assert "each foot on its own side" in srv.POSE_STYLE_RULE


def test_얼굴_규칙이_두_경우를_모두_다룬다():
    r = srv.FACE_ADAPTIVE_RULE
    assert "CASE A" in r and "CASE B" in r
    low = r.lower()
    # 얼굴 있는 사진 → 같은 사람
    for w in ("same face", "hairstyle", "same individual"):
        assert w in low, f"얼굴 일관성 문구 '{w}' 가 빠졌다"
    # 얼굴 없는 사진 → 지금까지의 규칙 그대로
    for w in ("base of the neck", "no chin", "no ears"):
        assert w in low, f"목 아래 크롭 문구 '{w}' 가 빠졌다"


def test_전신_연동_규칙이_살아있다():
    low = srv.POSE_STYLE_RULE.lower()
    assert "whole-body" in low
    for w in ("hip", "knee", "foot", "weight"):
        assert w in low


# ---------------------------------------------------------------- 업로드 한도

def test_폼_텍스트_한도가_업로드_한도와_같다():
    """12포즈 모드는 고른 컷을 data URL 텍스트 필드로 보낸다.
    Werkzeug 3.1 기본값(500KB)이면 항상 413 이 난다."""
    assert srv.app.config["MAX_FORM_MEMORY_SIZE"] == srv.app.config["MAX_CONTENT_LENGTH"]
    assert srv.app.config["MAX_CONTENT_LENGTH"] >= 16 * 1024 * 1024


def test_기획_모델_후보가_최신순이다():
    assert srv.PLAN_MODELS[0].startswith("gemini-3")
