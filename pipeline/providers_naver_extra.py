"""네이버 추가 실데이터 소스.

1) DataLab 검색어트렌드  → 12개월 추이·성별·연령 (상대지수 기반, 총량은 키워드툴에 앵커)
   필요: NAVER_DEV_CLIENT_ID / NAVER_DEV_CLIENT_SECRET  (네이버 개발자센터 앱)
2) 검색 API(blog/cafe)   → 콘텐츠 발행량(문서수)
   필요: 위 개발자 앱 동일 키
3) 검색광고 Estimate     → 예상 입찰가(CPC)
   필요: NAVER1_* (검색광고, 키워드툴과 동일)

시간대별(0~23시)은 어떤 공개 API도 제공하지 않으므로 항상 모델 추정.
모든 함수는 실패 시 None → 상위에서 모델값 유지.
"""
import os
import time
import json
import hmac
import base64
import hashlib
from datetime import date
import requests

DEV_ID = lambda: os.environ.get("NAVER_DEV_CLIENT_ID", "")
DEV_SECRET = lambda: os.environ.get("NAVER_DEV_CLIENT_SECRET", "")
# NAVER API HUB(NCP) 엔드포인트 — 2026-07-31 개발자센터 openapi.naver.com 신규종료 이관
DATALAB = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
SEARCH = "https://naverapihub.apigw.ntruss.com/search/v1/{}"
SEARCHAD = "https://api.searchad.naver.com"

# DataLab 연령대 코드 → 프론트 6버킷
AGE_BUCKETS = [
    ("10대", ["1", "2"]),
    ("20대", ["3", "4"]),
    ("30대", ["5", "6"]),
    ("40대", ["7", "8"]),
    ("50대", ["9", "10"]),
    ("60+", ["11"]),
]


def _dev_headers():
    # NAVER API HUB 인증 헤더 (구 개발자센터 X-Naver-Client-* → X-NCP-APIGW-*)
    return {"X-NCP-APIGW-API-KEY-ID": DEV_ID(), "X-NCP-APIGW-API-KEY": DEV_SECRET(),
            "Content-Type": "application/json"}


def _datalab_sum(kw, extra):
    """DataLab 호출 후 ratio 합계 반환(상대 비교용). 실패 시 None."""
    end = date.today()
    start = date(end.year - 1, end.month, 1)
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(),
            "timeUnit": "month",
            "keywordGroups": [{"groupName": kw, "keywords": [kw]}]}
    body.update(extra)
    try:
        r = requests.post(DATALAB, headers=_dev_headers(), data=json.dumps(body), timeout=12)
        if r.status_code != 200:
            return None
        data = r.json()["results"][0]["data"]
        return sum(float(x["ratio"]) for x in data), data
    except Exception:
        return None


def datalab(kw, total, m_share, logs=None):
    """추이(12개월 절대 근사)·성별·연령. 개발자 키 없으면 None."""
    logs = logs if logs is not None else []
    if not (DEV_ID() and DEV_SECRET()):
        return None
    base = _datalab_sum(kw, {})
    if not base:
        logs.append(f"[datalab] {kw} 기본 추이 실패")
        return None
    _, series = base
    ratios = [float(x["ratio"]) for x in series]
    mx = max(ratios) or 1
    trend = []
    for rt in ratios[-12:]:
        v = rt / mx * total
        trend.append({"pc": round(v * (1 - m_share)), "mob": round(v * m_share)})
    while len(trend) < 12:
        trend.insert(0, {"pc": 0, "mob": 0})

    # 성별
    male = female = None
    gm = _datalab_sum(kw, {"gender": "m"})
    gf = _datalab_sum(kw, {"gender": "f"})
    if gm and gf:
        sm, sf = gm[0], gf[0]
        tot = (sm + sf) or 1
        male = round(sm / tot * 100)
        female = 100 - male

    # 연령 (6버킷)
    age = None
    sums = []
    ok = True
    for _, codes in AGE_BUCKETS:
        s = _datalab_sum(kw, {"ages": codes})
        if not s:
            ok = False
            break
        sums.append(s[0])
        time.sleep(0.1)
    if ok and sum(sums) > 0:
        t = sum(sums)
        age = [round(x / t * 100) for x in sums]

    logs.append(f"[datalab] {kw} trend=OK gender={'OK' if male is not None else '-'} age={'OK' if age else '-'}")
    out = {"trend": trend}
    if male is not None:
        out["male"], out["female"] = male, female
    if age:
        out["age"] = age
    return out


def _strip_tags(s):
    """네이버 검색결과 title/description 의 <b></b> 태그·HTML 엔티티 제거."""
    import re
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", str(s or ""))).strip()


def _fmt_postdate(s):
    s = str(s or "")
    return f"{s[0:4]}.{s[4:6]}.{s[6:8]}" if len(s) == 8 and s.isdigit() else ""


def _relevant(kw, title, desc):
    """이 글의 '제목'이 키워드를 실제로 언급하나(본문/설명은 보지 않음). 네이버
    블로그 검색은 '그룹웨어'를 '그룹'+'웨어'로 쪼개 매칭해, 제목에 키워드가
    없는 엉뚱한 글(볼보그룹 등)도 올리므로 제목 기준으로 후처리해 거른다.
    - 단어 1개: 공백 제거 키워드가 제목에 통째로 포함돼야 함.
    - 여러 단어: 모든 토큰이 제목에 포함돼야 함."""
    tokens = [t for t in kw.split() if t]
    if not tokens:
        return True
    nt = title.replace(" ", "")
    if len(tokens) == 1:
        return kw.replace(" ", "") in nt
    return all((t in title) or (t in nt) for t in tokens)


def _one_search(kw, endpoint, author_f, sort, n):
    """블로그/카페 검색 1회 → (items, total). 실패 시 (None, 0).
    넉넉히(20개) 받아 키워드가 실제로 들어간 글만 상위 n개. 관련글이 아예 없으면
    (극저볼륨 등) 원본 상위 n개로 폴백해 빈칸을 피한다. total 은 원본(느슨) 기준."""
    try:
        r = requests.get(SEARCH.format(endpoint), headers=_dev_headers(),
                         params={"query": kw, "display": 20, "sort": sort}, timeout=10)
        if r.status_code != 200:
            return None, 0
        j = r.json()
        raw = j.get("items", []) or []

        def _mk(it):
            return {
                "title": _strip_tags(it.get("title")),
                "desc": _strip_tags(it.get("description")),
                "url": it.get("link", ""),
                "author": _strip_tags(it.get(author_f)),
                "date": _fmt_postdate(it.get("postdate")),
            }
        rel = []
        for it in raw:
            m = _mk(it)
            if _relevant(kw, m["title"], m["desc"]):
                rel.append(m)
            if len(rel) >= n:
                break
        # 관련 글이 0건이면 예전엔 무관한 원본 상위 n개로 '빈 칸 회피' 폴백을
        # 했는데, 그게 바로 "제목에 경쟁사명도 없는 엉뚱한 글이 뜬다"는 문제였다.
        # 관련 글이 없으면 빈 목록이 맞는 답이다(프론트가 빈 상태를 정직히 표시).
        return rel, int(j.get("total", 0))
    except Exception:
        return None, 0


def fetch_posts(kw, n=5, logs=None):
    """블로그·카페 상위 글을 '관련순(sim)·최신순(date)' 둘 다 n개씩 + 총 문서수.
    {'sim':{'blog':[...],'cafe':[...]}, 'date':{...}, 'total':int}.
    개발자 키 없거나 전부 실패 시 None. (네이버 검색은 sim·date 두 정렬만 지원)"""
    logs = logs if logs is not None else []
    if not (DEV_ID() and DEV_SECRET()):
        return None
    out = {"sim": {"blog": [], "cafe": []}, "date": {"blog": [], "cafe": []}, "total": 0}
    got = False
    endpoints = (("blog", "blog", "bloggername"), ("cafearticle", "cafe", "cafename"))
    for sort in ("sim", "date"):
        for endpoint, key, author_f in endpoints:
            items, total = _one_search(kw, endpoint, author_f, sort, n)
            if items is None:
                continue
            got = True
            out[sort][key] = items
            if sort == "sim":       # total 은 정렬 무관 — 한 번만(sim) 합산
                out["total"] += total
    if not got:
        return None
    logs.append(f"[posts] {kw} 관련순 {len(out['sim']['blog'])}/{len(out['sim']['cafe'])}"
                f"·최신순 {len(out['date']['blog'])}/{len(out['date']['cafe'])}")
    return out


def content_count(kw, logs=None):
    """블로그+카페 문서수 = 콘텐츠 발행량. 개발자 키 없으면 None."""
    logs = logs if logs is not None else []
    if not (DEV_ID() and DEV_SECRET()):
        return None
    total = 0
    got = False
    for kind in ("blog", "cafearticle"):
        try:
            r = requests.get(SEARCH.format(kind), headers=_dev_headers(),
                             params={"query": kw, "display": 1}, timeout=10)
            if r.status_code == 200:
                total += int(r.json().get("total", 0))
                got = True
        except Exception:
            pass
    if not got:
        return None
    logs.append(f"[search] {kw} 콘텐츠 {total}건")
    return total


# ── 검색광고 Estimate (예상 입찰가) ──
def _sa_headers(acc, uri, method="POST"):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}.{method}.{uri}"
    sig = base64.b64encode(hmac.new(str(acc["secret_key"]).encode(), msg.encode(), hashlib.sha256).digest()).decode()
    return {"X-Timestamp": ts, "X-API-KEY": str(acc["api_key"]),
            "X-Customer": str(acc["customer_id"]), "X-Signature": sig,
            "Content-Type": "application/json"}


def estimate_cpc(kw, acc, logs=None):
    """평균 노출위치(2위) 예상 입찰가. 실패 시 None → 모델 추정 유지."""
    logs = logs if logs is not None else []
    if not acc:
        return None
    uri = "/estimate/average-position-bid/keyword"
    body = {"device": "MOBILE", "items": [{"key": kw.replace(" ", ""), "position": 2}]}
    try:
        r = requests.post(SEARCHAD + uri, headers=_sa_headers(acc, uri),
                          data=json.dumps(body), timeout=12)
        if r.status_code != 200:
            return None
        est = r.json().get("estimate", [])
        if est and est[0].get("bid"):
            return int(est[0]["bid"])
    except Exception as e:
        logs.append(f"[estimate] {kw} 오류: {e}")
    return None


def estimate_position_bids(kw, acc, positions=(1, 2, 3), logs=None):
    """한 키워드의 '목표 노출순위별' 예상 입찰가를 한 번의 호출로. {position: bid}.
    average-position-bid 의 items 에 같은 키워드를 순위별로 넣어 1콜로 받는다.
    실패/누락분 생략 → {}. cpc(2위)와 '몇 위 노리면 얼마' 슬라이더에 함께 쓴다."""
    logs = logs if logs is not None else []
    if not acc:
        return {}
    uri = "/estimate/average-position-bid/keyword"
    k = kw.replace(" ", "")
    items = [{"key": k, "position": int(p)} for p in positions]
    body = {"device": "MOBILE", "items": items}
    try:
        r = requests.post(SEARCHAD + uri, headers=_sa_headers(acc, uri),
                          data=json.dumps(body), timeout=12)
        if r.status_code != 200:
            return {}
        est = r.json().get("estimate", [])
        out = {}
        for it, e in zip(items, est):
            bid = e.get("bid") if isinstance(e, dict) else None
            if bid:
                out[it["position"]] = int(bid)
        return out
    except Exception as e:
        logs.append(f"[estimate] {kw} 순위별 오류: {str(e)[:80]}")
        return {}


def estimate_cpc_batch(keywords, acc, logs=None):
    """여러 키워드의 예상 입찰가를 '한 번의 호출'로 받는다(position 2 기준).
    {공백제거_키워드: bid} 반환. 실패/누락분은 생략 → 상위에서 기존 추정값 유지."""
    logs = logs if logs is not None else []
    if not acc or not keywords:
        return {}
    uri = "/estimate/average-position-bid/keyword"
    items = [{"key": k.replace(" ", ""), "position": 2} for k in keywords]
    body = {"device": "MOBILE", "items": items}
    try:
        r = requests.post(SEARCHAD + uri, headers=_sa_headers(acc, uri),
                          data=json.dumps(body), timeout=15)
        if r.status_code != 200:
            logs.append(f"[estimate] 연관 CPC status={r.status_code}")
            return {}
        est = r.json().get("estimate", [])
        out = {}
        for it, e in zip(items, est):
            bid = e.get("bid") if isinstance(e, dict) else None
            if bid:
                out[it["key"]] = int(bid)
        logs.append(f"[estimate] 연관 CPC {len(out)}/{len(items)}건 실값")
        return out
    except Exception as e:
        logs.append(f"[estimate] 연관 CPC 오류: {str(e)[:80]}")
        return {}
