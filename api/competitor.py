"""Vercel 서버리스 함수 — 경쟁사 스냅샷.

경쟁사명·네이버 검색어·프로모션 URL 을 받아, 이미 구축된 크롤링 엔진을 묶어
'광고 집행 강도·블로그 발행량·검색 추이·SERP 광고 관측·프로모션 스냅샷'을
정확도 라벨(실측/관측/추정/미확인)과 함께 돌려준다.

  /api/competitor?name=<경쟁사명>&kw=<네이버 검색어 별칭>&promo=<프로모션 URL>

재사용: providers_firecrawl(serp_competitors·scrape_promo) + providers_naver_extra
        (content_count·datalab·fetch_posts). 모든 소스는 키 없으면 조용히 스킵.
느린 Firecrawl 호출은 병렬(ThreadPoolExecutor)로 묶어 Vercel 30초 안에 끝낸다.
"""
import os
import sys
import json
import math
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import providers_firecrawl as FC   # noqa: E402
import providers_naver_extra as NX  # noqa: E402


def _fix_kr(s):
    """Vercel 쿼리스트링 한글 mojibake 복원 (api/keyword 와 동일)."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except UnicodeError:
        try:
            return s.encode("utf-8", "surrogateescape").decode("utf-8")
        except UnicodeError:
            return s


def _trend_dir(series):
    """최근 3개월 평균 vs 이전 3개월 평균으로 추이 방향·변화율."""
    vals = [(x.get("pc", 0) + x.get("mob", 0)) if isinstance(x, dict) else (x or 0) for x in (series or [])]
    if len(vals) < 6:
        return ("보합", 0)
    recent = sum(vals[-3:]) / 3.0
    prev = (sum(vals[-6:-3]) / 3.0) or 1.0
    pct = round((recent - prev) / prev * 100)
    d = "상승" if pct >= 8 else "하락" if pct <= -8 else "보합"
    return (d, pct)


def _intensity(ads, content, trend_dir):
    """광고 집행 강도 0-100 (관측 신호 합성 → 추정). 근거 문자열 포함."""
    ads_total = len(ads)
    comp = [i for i, a in enumerate(ads) if a.get("us")]
    comp_rank = (comp[0] + 1) if comp else 0
    score = 0
    if comp_rank:
        score += max(15, 45 - (comp_rank - 1) * 8)   # 1위45·2위37·3위29…
    score += min(20, ads_total * 3)                  # 광고 밀도
    if content and content > 0:
        score += min(20, round(math.log10(max(1, content)) * 5))
    score += 15 if trend_dir == "상승" else 7 if trend_dir == "보합" else 0
    score = max(0, min(100, score))
    band = "높음" if score >= 67 else "보통" if score >= 34 else "낮음"
    # comp_rank = 이 검색어에서 '추적 대상 이름'이 몇 번째 광고로 노출되는지(us:true 첫 위치).
    # 문구는 "경쟁사"가 아니라 "노출순위"로 — 이 도구는 남의 경쟁사를 감시할 때도 쓰지만
    # 자기 브랜드(예: 뷰티 브랜드 본인)를 등록해 검색 노출 순위를 보는 데도 쓴다. 후자일 때
    # "경쟁사 광고 1위"라고 하면 정반대로 읽힌다.
    parts = [f"관측 광고주 {ads_total}개"]
    parts.append(f"노출순위 {comp_rank}위" if comp_rank else "노출순위 미확인")
    if content:
        parts.append(f"블로그 {int(content):,}건")
    conf = "보통" if (comp_rank and content) else "낮음"
    return {"score": score, "band": band, "confidence": conf,
            "basis": " · ".join(parts), "compRank": comp_rank, "adsTotal": ads_total}


def snapshot(name, kw, promo_url, logs):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as pool:
        f_serp = pool.submit(FC.serp_competitors, kw, [name], logs) if kw else None
        f_promo = pool.submit(FC.scrape_promo, promo_url, logs) if promo_url else None
        f_cc = pool.submit(NX.content_count, kw, logs) if kw else None
        f_dl = pool.submit(NX.datalab, kw, 1000, 0.7, logs) if kw else None
        f_posts = pool.submit(NX.fetch_posts, kw, 5, logs) if kw else None

        def g(f):
            try:
                return f.result() if f else None
            except Exception as e:
                logs.append(f"[competitor] 소스 오류: {str(e)[:100]}")
                return None
        serp, promo = g(f_serp), g(f_promo)
        cc, dl, posts = g(f_cc), g(f_dl), g(f_posts)

    ads = (serp or {}).get("ads", [])
    organic = (serp or {}).get("organic", [])
    content = cc if cc is not None else ((posts or {}).get("total") if posts else None)
    series = (dl or {}).get("trend") or []
    tdir, tpct = _trend_dir(series)
    inten = _intensity(ads, content or 0, tdir)
    blog = ((posts or {}).get("sim") or {}).get("blog") or []
    topics = [p.get("title", "") for p in blog if p.get("title")][:5]

    return {
        "name": name, "kw": kw,
        "intensity": {**inten, "label": "추정"},
        "content": {"volume": content, "topics": topics,
                    "posts": blog[:5],
                    "label": "실측" if cc is not None else ("관측" if posts else "미확인")},
        "trend": {"dir": tdir, "pct": tpct, "series": series,
                  "label": "실측" if series else "미확인"},
        "ads": ads, "organic": organic,
        "adsLabel": "관측" if serp else "미확인",
        "promo": promo,
        "promoLabel": "관측" if promo else ("미확인" if promo_url else "미설정"),
        "_any": bool(serp or promo or cc is not None or dl or posts),
    }


def ad_search_url(src, q):
    """경쟁사 이름만으로 광고 라이브러리 검색 URL 생성(URL 직접 입력 대체)."""
    from urllib.parse import quote
    qq = quote(q, safe="")
    if src == "meta":
        return ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
                "&country=KR&q=%s&search_type=keyword_unordered&media_type=all" % qq)
    if src == "google":
        return "https://adstransparency.google.com/?region=KR&query=%s" % qq
    return ""


def ad_gallery(url, logs):
    """Meta 라이브러리 / Google 투명성센터 한 곳의 광고 소재만 스크랩(분리 호출).
    느리거나 실패해도 본 스냅샷과 무관 → 프론트가 별도로 lazy 호출."""
    res = FC.scrape_ads(url, logs)
    return {"ads": (res or {}).get("ads", []), "label": "관측" if res else "미확인",
            "_any": bool(res)}


def _diag(logs):
    return {
        "env_seen": {
            "FIRECRAWL_API_KEY": bool(os.environ.get("FIRECRAWL_API_KEY")),
            "NAVER_DEV_CLIENT_ID": bool(os.environ.get("NAVER_DEV_CLIENT_ID")),
            "NAVER_DEV_CLIENT_SECRET": bool(os.environ.get("NAVER_DEV_CLIENT_SECRET")),
        },
        "logs": logs,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        name = _fix_kr((qs.get("name", [""])[0] or "").strip())
        kw = _fix_kr((qs.get("kw", [""])[0] or "").strip()) or name
        promo = (qs.get("promo", [""])[0] or "").strip()
        only = (qs.get("only", [""])[0] or "").strip()   # 'meta'|'google' → 광고 갤러리만
        debug = (qs.get("debug", [""])[0] or "").strip() in ("1", "true", "yes")
        logs = []
        # 광고 라이브러리 소재 갤러리(분리 호출) — 느리거나 실패해도 개요와 무관
        if only in ("meta", "google", "shot"):
            url = _fix_kr((qs.get("url", [""])[0] or "").strip())
            qname = _fix_kr((qs.get("q", [""])[0] or "").strip())
            src = (qs.get("src", ["meta"])[0] or "meta").strip()   # shot 의 대상 라이브러리
            if not url and qname:            # URL 없으면 이름으로 검색 URL 자동생성
                url = ad_search_url(src if only == "shot" else only, qname)
            if not url:
                return self._send({"error": "url 또는 q(이름)가 필요합니다"}, 400, cache=False)
            try:
                if only == "shot":
                    shot = FC.page_shot(url, logs)
                    res = {"shot": (shot or {}).get("shot", ""), "_any": bool(shot)}
                else:
                    res = ad_gallery(url, logs)
            except Exception as e:
                return self._send({"error": str(e)[:200]}, 500, cache=False)
            if debug:
                return self._send(_diag(logs), 200, cache=False)
            got = res.pop("_any", False)
            res["src"] = only
            # 스크린샷(GCS)만 캐시. meta/google 개별소재는 이미지 원본URL이 몇 분내 만료돼
            # 캐시하면 다 깨지므로 매번 신선하게(no-store).
            return self._send(res, 200, cache=(got and only == "shot"))
        if not name and not kw and not promo:
            return self._send({"error": "name·kw·promo 중 하나는 필요합니다"}, 400, cache=False)
        try:
            snap = snapshot(name, kw, promo, logs)
        except Exception as e:
            return self._send({"error": str(e)[:200]}, 500, cache=False)
        if debug:
            return self._send(_diag(logs), 200, cache=False)
        real = snap.pop("_any", False)
        snap["_demo"] = not real
        self._send(snap, 200, cache=real)

    def _send(self, obj, code, cache=True):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # 경쟁사 스냅샷은 1시간 캐시(실데이터만) — Firecrawl 크레딧·네이버 쿼터 절약
        self.send_header("Cache-Control",
                         "public, max-age=3600, s-maxage=3600" if cache else "no-store, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
