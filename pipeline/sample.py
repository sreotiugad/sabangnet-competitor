"""샘플 data.json 생성 + 키워드 보조지표 모델러.

- 광고 리포트 샘플: 프론트(app.html)의 합성 공식과 동일한 파라미터로 30일 일별 생성.
- 키워드: 실검색량(네이버)이 없을 때 쓰는 완전 샘플 + 실검색량이 있을 때
  인구/시간대/추이 같은 '모델 추정' 보조지표를 붙이는 함수(model_extras).
"""
import math
from datetime import date, timedelta
import config as C


def _rng(seed):
    # mulberry32 (프론트 MUL 과 동일 계열의 결정적 난수)
    state = {"a": seed & 0xFFFFFFFF}
    def r():
        state["a"] = (state["a"] + 0x6D2B79F5) & 0xFFFFFFFF
        t = state["a"]
        t = ((t ^ (t >> 15)) * (1 | t)) & 0xFFFFFFFF
        t = (t ^ (t + (((t ^ (t >> 7)) * (61 | t)) & 0xFFFFFFFF))) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296.0
    return r


def _hash(s):
    h = 2166136261
    for ch in str(s):
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


# 프론트 SUBS 와 동일 파라미터
SUB_PARAMS = {
    "사방넷":     dict(cost=1740000, cpa=9100, cpc=610, ctr=.034, mo=.7,  media={"네이버":.62,"구글":.38}, ct={"브랜드검색":.14,"파워링크":.33,"쇼핑검색":.21,"구글검색":.16,"실적최대화":.12,"동영상":.04}),
    "사방넷미니": dict(cost=452000,  cpa=8100, cpc=560, ctr=.036, mo=.5,  media={"네이버":.70,"구글":.30}, ct={"브랜드검색":.10,"파워링크":.40,"쇼핑검색":.24,"구글검색":.14,"실적최대화":.09,"동영상":.03}),
    "풀필먼트":   dict(cost=298000,  cpa=11700,cpc=640, ctr=.030, mo=-.4, media={"네이버":.55,"구글":.45}, ct={"브랜드검색":.08,"파워링크":.30,"쇼핑검색":.14,"구글검색":.24,"실적최대화":.18,"동영상":.06}),
    "애드콘":     dict(cost=986000,  cpa=7200, cpc=520, ctr=.037, mo=.6,  media={"네이버":.58,"구글":.42}, ct={"브랜드검색":.18,"파워링크":.34,"쇼핑검색":.06,"구글검색":.20,"실적최대화":.16,"동영상":.06}),
    "엔팩스":     dict(cost=588000,  cpa=9500, cpc=580, ctr=.035, mo=.2,  media={"네이버":.66,"구글":.34}, ct={"브랜드검색":.12,"파워링크":.42,"쇼핑검색":.04,"구글검색":.22,"실적최대화":.14,"동영상":.06}),
    "다우오피스": dict(cost=824000,  cpa=11700,cpc=700, ctr=.033, mo=.3,  media={"네이버":.60,"구글":.40}, ct={"브랜드검색":.16,"파워링크":.30,"쇼핑검색":.02,"구글검색":.28,"실적최대화":.18,"동영상":.06}),
    "다우오피스HR":dict(cost=372000, cpa=13300,cpc=720, ctr=.031, mo=-.5, media={"네이버":.64,"구글":.36}, ct={"브랜드검색":.12,"파워링크":.28,"쇼핑검색":.02,"구글검색":.30,"실적최대화":.20,"동영상":.08}),
}


def _axis(days):
    end = date.today() - timedelta(days=1)
    return [(end - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def _gen_sub(name, days):
    p = SUB_PARAMS[name]
    r = _rng(_hash(name) + days * 7)
    cost, signup, imp, click = [], [], [], []
    for i in range(days):
        t = i / (days - 1) if days > 1 else 0
        trend = 1 + p["mo"] * (t - .5) * .44
        noise = .85 + r() * .30
        dow = (i + 2) % 7
        wk = .9 if dow in (5, 6) else 1.0
        c = max(0.0, p["cost"] * trend * noise * wk)
        cost.append(round(c))
        click.append(round(c / p["cpc"]))
        imp.append(round(c / p["cpc"] / p["ctr"]))
        signup.append(round(c / p["cpa"], 1))
    return {"group": C.BRAND_MAP[name][0], "cost": cost, "signup": signup,
            "imp": imp, "click": click, "media": p["media"], "ct": p["ct"]}


def build_sample_report(days=None):
    days = days or C.PERIOD_DAYS
    axis = _axis(days)
    subs = {name: _gen_sub(name, days) for name in SUB_PARAMS}
    brands = {g: {"subs": C.GROUP_SUBS[g]} for g in C.GROUPS}
    return {"period": {"start": axis[0], "end": axis[-1], "days": days, "dates": axis},
            "report": {"brands": brands, "subs": subs}}


# ── 키워드 ──────────────────────────────────────────────
def comp_level(adv):
    return "낮음" if adv <= 3 else ("중간" if adv <= 8 else "높음")

_COMP_POOL = ["이카운트", "더존비즈온", "네이버웍스", "잡코리아", "사람인", "카페24", "고도몰", "영림원"]
_AD_COPY = ["30일 무료체험 시작", "도입 문의 1위 솔루션", "합리적 요금·중소기업", "전자결재·근태 올인원", "무료 상담 예약"]


def model_extras(kw, total, m_share):
    """실검색량이 있을 때 붙이는 모델 추정 보조지표(인구·시간대·추이·경쟁 브랜드)."""
    r = _rng(_hash(kw) + 999)
    trend = []
    for i in range(12):
        seas = 1 + .28 * math.sin(i / 12 * 6.28 + r() * 3)
        nz = .85 + r() * .3
        v = total * seas * nz
        trend.append({"pc": round(v * (1 - m_share)), "mob": round(v * m_share)})
    male = 28 + round(r() * 44)
    female = 100 - male
    age_raw = [x * (.6 + r() * .8) for x in (6, 18, 26, 22, 16, 12)]
    asum = sum(age_raw) or 1
    age = [round(x / asum * 100) for x in age_raw]
    dow_raw = [x * (.85 + r() * .3) for x in (1, 1, 1, 1, 1.05, .75, .7)]
    dsum = sum(dow_raw) or 1
    dow = [round(x / dsum * 100, 1) for x in dow_raw]
    hour = []
    for h in range(24):
        b = math.exp(-((h - 14) ** 2) / 40) + .5 * math.exp(-((h - 21) ** 2) / 24) + .06
        b *= (.85 + r() * .3)
        hour.append(b)
    hsum = sum(hour) or 1
    hour_p = [round(x / hsum * 100, 1) for x in hour]
    blog = round(total * (1.4 + r() * 13))
    sat = round(blog / total, 1) if total else 0
    pool = _COMP_POOL[:]
    # 간단 셔플
    for i in range(len(pool) - 1, 0, -1):
        j = int(r() * (i + 1))
        pool[i], pool[j] = pool[j], pool[i]
    pool = pool[:4]
    us_pos = int(r() * 5)
    brands = []
    bi = 0
    for pidx in range(5):
        if pidx == us_pos:
            brands.append({"name": C.OUR_BRAND, "us": True, "bid": 0, "copy": f"업무의 모든 것, {C.OUR_BRAND}"})
        else:
            brands.append({"name": pool[bi % len(pool)], "us": False,
                           "bid": 0, "copy": _AD_COPY[(bi + pidx) % len(_AD_COPY)]})
            bi += 1
    return {"trend": trend, "male": male, "female": female, "age": age,
            "dow": dow, "hourP": hour_p, "blog": blog, "sat": sat, "brands": brands}


def build_sample_keyword(kw):
    r = _rng(_hash(kw) + kw.__len__() * 131)
    total = round((4000 + r() * 260000) / 10) * 10
    m_share = .58 + r() * .32
    pc = round(total * (1 - m_share) / 10) * 10
    mob = total - pc
    advertisers = 1 + int(r() * 15)
    lvl = comp_level(advertisers)
    cpc = round((250 + r() * 1500 + (1650 if lvl == "높음" else 1100 if lvl == "중간" else 550)) / 10) * 10
    ex = model_extras(kw, total, m_share)
    sfx = ["가격", "추천", "후기", "비교", "순위", "무료", "프로그램", "도입"]
    related = []
    for s in sfx:
        rr = _rng(_hash(kw + s))
        v = round(total * (.06 + rr() * .55) / 10) * 10
        adv = 1 + int(rr() * 14)
        related.append({"kw": f"{kw} {s}", "v": v, "comp": comp_level(adv),
                        "cpc": round((250 + rr() * 1600) / 10) * 10})
    related.sort(key=lambda x: -x["v"])
    # 우리 브랜드 추정 입찰 = 이 키워드 cpc, 경쟁사는 근사
    for b in ex["brands"]:
        b["bid"] = cpc if b["us"] else round((cpc * (.7 + r() * .8)) / 10) * 10
    yt = model_youtube(kw)
    return {"total": total, "pc": pc, "mob": mob, "mShare": round(m_share, 4),
            "advertisers": advertisers, "comp": lvl, "cpc": cpc,
            "blog": ex["blog"], "sat": ex["sat"], "trend": ex["trend"],
            "male": ex["male"], "female": ex["female"], "age": ex["age"],
            "dow": ex["dow"], "hourP": ex["hourP"], "related": related,
            "brands": ex["brands"], "youtube": yt,
            "posts": {"sim": {"blog": [], "cafe": []}, "date": {"blog": [], "cafe": []}}}


_YT_TYPES = ["완벽 정리", "도입 후기", "비교 리뷰", "튜토리얼", "추천 TOP5", "실사용 꿀팁"]
_YT_CH = ["IT클래스", "오피스랩", "실무노트 TV", "테크리뷰", "스타트업로그", "생산성연구소"]
_YT_AGO = ["3일 전", "1주 전", "2주 전", "1개월 전", "3개월 전", "6개월 전"]

def model_youtube(kw):
    """샘플 YouTube 목록. 썸네일은 없음(프론트가 플레이스홀더 표시).
    실데이터는 pipeline/providers 의 youtube 프로바이더가 채운다."""
    out = []
    for i in range(6):
        r = _rng(_hash(kw + "yt" + str(i)))
        out.append({
            "title": f"{kw} {_YT_TYPES[i % len(_YT_TYPES)]}",
            "channel": _YT_CH[int(r() * len(_YT_CH))],
            "views": round(1000 + r() * 900000),
            "date": _YT_AGO[int(r() * len(_YT_AGO))],
            "thumb": None, "url": "#",
            "dur": f"{int(2 + r() * 13)}:{int(r() * 59):02d}",
        })
    out.sort(key=lambda v: -v["views"])
    return out


def build_sample(days=None):
    rep = build_sample_report(days)
    kw = {k: build_sample_keyword(k) for k in C.KEYWORDS}
    return {"source": "sample", "generated_at": date.today().isoformat(),
            "period": rep["period"], "report": rep["report"], "keyword": kw}
