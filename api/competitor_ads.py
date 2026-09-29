"""Vercel 서버리스 함수 — 경쟁사 Meta 광고 소재 온디맨드(실시간) 조회.

comp-ads/manifest.json(GitHub Actions "FB Ads Capture" 수동 트리거로만 갱신되는
정적 캡처)의 대안. ScrapeCreators Facebook 광고 라이브러리 API로 요청 시점에
그때그때 호출해 임의 경쟁사에 대해 즉시 결과를 돌려준다(구글 조회와 같은 계정/키).

엔드포인트(ScrapeCreators, 헤더 x-api-key: SCRAPECREATORS_API_KEY):
  - GET /v1/facebook/adLibrary/search/companies?query=<이름>  → 광고주 page_id
  - GET /v1/facebook/adLibrary/company/ads?pageId=<id>        → 그 광고주 광고(정확)
  - GET /v1/facebook/adLibrary/search/ads?query=<키워드>       → 키워드 검색(폴백)
반환 광고는 FB GraphQL snapshot 구조라 fbcdn 이미지 URL을 세션/IP 무관 다운로드 가능.

  /api/competitor_ads?kw=<검색어>&name=<표시용 이름>&url=<광고주 라이브러리 URL>
  (debug=1 진단 로그, probe=1 첫 광고 원본 구조 확인)

프론트(index.html DUComp.renderDA)는 이 엔드포인트를 우선 호출하고,
키 미설정이거나 실패하면 기존 comp-ads/manifest.json 정적 캡처로 폴백한다.
"""
import os
import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

import requests

API_BASE = "https://api.scrapecreators.com/v1/facebook/adLibrary"
MAX_ADS = 24
KST = timezone(timedelta(hours=9))
_now_kst = lambda: datetime.now(KST).strftime("%Y-%m-%d %H:%M")   # Vercel 서버는 UTC라 KST로 보정


def _fix_kr(s):
    """Vercel 쿼리스트링 한글 mojibake 복원 (api/competitor.py 와 동일)."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except UnicodeError:
        try:
            return s.encode("utf-8", "surrogateescape").decode("utf-8")
        except UnicodeError:
            return s


def _ad_library_url(kw, country="KR"):
    return ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
            "&country=%s&q=%s&search_type=keyword_unordered&media_type=all" % (country, quote(kw)))


_BAD_DOM = ("facebook.com", "instagram.com", "fb.com", "l.facebook.com",
            "fb.me", "wa.me", "youtube.com", "linktr.ee",
            # 단축링크·리테일러·마켓 — 브랜드 본인 도메인이 아니므로 Google 도메인 조회에 부적합
            "oy.run", "naver.me", "smartstore.naver.com", "shopping.naver.com",
            "coupang.com", "oliveyoung.co.kr", "bit.ly", "me2.do", "abr.ge",
            "vip.link", "link.coupang.com", "kko.to", "open.kakao.com")


def _ad_domain(snap):
    """광고주 본인 도메인 추출: Meta 광고의 caption('themedicube.co.kr')이나 link_url.
    이건 광고주가 자기 광고에 직접 건 랜딩이라 그 브랜드의 실제 도메인 = Google
    투명성센터 조회(도메인 기준)에 그대로 쓸 수 있다."""
    cap = (snap.get("caption") or "").strip().lower()
    host = None
    if cap and "." in cap and " " not in cap:
        host = cap.split("/")[0]
    if not host:
        lu = snap.get("link_url") or ""
        try:
            host = urlparse(lu if "://" in lu else "http://" + lu).netloc.lower()
        except Exception:
            host = None
    if not host:
        return None
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or any(host == b or host.endswith("." + b) for b in _BAD_DOM):
        return None
    return host


def _perf(item):
    """성과 프록시: 광고를 얼마나 오래·계속 집행 중인지.
    광고주는 안 먹히는 소재를 바로 끄므로, 오래·활성 집행 = 그 브랜드에게 검증된 승자.
    Meta는 경쟁사 실성과(CTR·전환)를 공개하지 않으니 이것이 유일하게 정직한 신호.
      days = 집행 일수(활성이면 오늘까지, 종료면 종료일까지)
      act  = 현재 집행 중 여부
      since= 집행 시작일(YYYY-MM-DD)"""
    sd = item.get("start_date")
    ed = item.get("end_date")
    act = bool(item.get("is_active"))
    days = None
    if isinstance(sd, (int, float)) and sd > 0:
        now = datetime.now(timezone.utc).timestamp()
        end = ed if (isinstance(ed, (int, float)) and ed > sd) else now
        if act:                       # 활성 광고는 지금 이 순간까지 계속 집행 중
            end = max(end, now)
        days = int((end - sd) // 86400)
    since = (item.get("start_date_formatted") or "")[:10] or None
    return {"days": days, "act": act, "since": since}


def _normalize(item):
    """FB 광고 라이브러리 원본 아이템(snapshot) → 프론트 da-item 카드가 기대하는 {u,t,type,...} 리스트.
    ⚠️ 이미지 광고(캐러셀·DPA·DCO)는 크리에이티브를 snapshot.cards[] 에 담는다.
    images/videos 만 읽으면 그런 이미지 광고를 통째로 놓쳐 '전부 영상'으로 보인다.
    videos·images·cards 를 모두 훑고, 각 크리에이티브를 영상/이미지로 판정한다.
    각 카드에 광고 단위 성과 프록시(집행기간·활성)를 함께 붙인다."""
    snap = item.get("snapshot") or {}
    body = ((snap.get("body") or {}).get("text") or "")[:400]
    perf = _perf(item)
    out = []

    def add(url, ty, vurl=None):
        # url = 카드에 표시할 정지 이미지(영상이면 포스터). vurl = 실제 재생용 영상 파일 URL.
        if url:
            d = {"u": url, "t": body, "type": ty,
                 "days": perf["days"], "act": perf["act"], "since": perf["since"]}
            if vurl:
                d["v"] = vurl          # 프론트에서 <video>로 호버 재생
            out.append(d)

    for v in (snap.get("videos") or []):
        add(v.get("video_preview_image_url"), "video",
            v.get("video_hd_url") or v.get("video_sd_url"))
    for im in (snap.get("images") or []):
        add(im.get("original_image_url") or im.get("resized_image_url"), "image")
    # 카드(캐러셀·DPA·DCO): 카드마다 영상이면 poster+영상URL, 아니면 이미지
    for c in (snap.get("cards") or []):
        vu = c.get("video_preview_image_url")
        if vu:
            add(vu, "video", c.get("video_hd_url") or c.get("video_sd_url"))
        else:
            add(c.get("original_image_url") or c.get("resized_image_url"), "image")
    return out


def _raw_probe(item):
    """(임시) 실데이터에서 날짜/활성 필드 키·값을 확인하기 위한 요약."""
    def summ(d):
        o = {}
        for k, v in d.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                o[k] = v
            elif isinstance(v, list):
                o[k] = "[list %d]" % len(v)
            elif isinstance(v, dict):
                o[k] = "{keys: %s}" % ",".join(list(v.keys())[:14])
        return o
    snap = item.get("snapshot") or {}
    return {"top": summ(item), "snapshot": summ(snap)}


def _extract_items(data):
    """ScrapeCreators 응답에서 광고(또는 회사) 리스트를 유연하게 뽑는다.
    래퍼 키(ads/results/searchResults/companies/data/items)가 뭐든, 리스트를 찾는다."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("ads", "results", "searchResults", "companies", "data", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return v
        for v in data.values():          # 폴백: 딕셔너리들의 첫 리스트
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def _first_page_id(data):
    """search/companies 응답에서 첫 회사의 광고 라이브러리 page id."""
    for it in _extract_items(data):
        for k in ("page_id", "pageId", "id", "ad_library_page_id", "adLibraryPageId"):
            v = it.get(k)
            if v:
                return str(v)
    return None


def _pid_from_url(u):
    """등록된 Meta 광고 라이브러리 URL에서 view_all_page_id / id 추출(있으면 정확 조회)."""
    if not u:
        return None
    try:
        q = parse_qs(urlparse(u).query)
        for k in ("view_all_page_id", "id", "page_id"):
            if q.get(k):
                return q[k][0]
    except Exception:
        pass
    return None


def _sc_get(path, params, key, logs):
    try:
        r = requests.get(API_BASE + path, params=params, headers={"x-api-key": key}, timeout=45)
    except Exception as e:
        logs.append("❌ [sc]%s 요청 실패: %s" % (path, str(e)[:160]))
        return None
    if r.status_code >= 400:
        logs.append("❌ [sc]%s status=%s body=%s" % (path, r.status_code, r.text[:160]))
        return None
    try:
        return r.json()
    except Exception:
        logs.append("❌ [sc]%s JSON 파싱 실패" % path)
        return None


def collect(kw, country="KR", max_ads=MAX_ADS, logs=None, page_url=None, probe=False):
    """ScrapeCreators Facebook 광고 라이브러리로 경쟁사 광고를 가져온다.
    우선순위: (1) 등록된 페이지 URL의 page_id → company/ads (정확),
    (2) 이름으로 search/companies → page_id → company/ads (정확·자동셋업),
    (3) 폴백: search/ads 키워드 검색(무관 광고 섞일 수 있음).
    파싱(_normalize/_ad_domain/_perf)은 FB snapshot 구조 그대로 재활용."""
    logs = logs if logs is not None else []
    key = os.environ.get("SCRAPECREATORS_API_KEY")
    if not key:
        logs.append("⚠️ [sc] SCRAPECREATORS_API_KEY 없음 — 건너뜀")
        return None
    items, precise = None, False
    pid = _pid_from_url(page_url)
    if not pid and kw:                     # 이름 → 회사 page id (자동셋업)
        cj = _sc_get("/search/companies", {"query": kw}, key, logs)
        if cj is not None:
            pid = _first_page_id(cj)
            if pid:
                logs.append("[sc] search/companies '%s' → pageId=%s" % (kw, pid))
    if pid:                                # 그 광고주 광고만 정확히
        aj = _sc_get("/company/ads", {"pageId": pid, "country": country}, key, logs)
        if aj is not None:
            items = _extract_items(aj)
            precise = True
            logs.append("[sc] company/ads pageId=%s ads=%d" % (pid, len(items)))
    if not items:                          # 폴백: 키워드 검색
        sj = _sc_get("/search/ads", {"query": kw or "", "country": country}, key, logs)
        if sj is None:
            return None
        items = _extract_items(sj)
        logs.append("[sc] search/ads '%s' ads=%d (키워드 검색 — 무관 광고 섞일 수 있음)" % (kw, len(items)))
    if not isinstance(items, list):
        items = []
    images, seen = [], set()
    fmt_dist = {}          # snapshot.display_format 분포(진짜 타입 확인용)
    dom_dist = {}          # 광고주 도메인 분포(Google 조회에 자동 재사용)
    for it in items:
        snap = it.get("snapshot") or {}
        df = (snap.get("display_format") or "?")
        if snap:
            fmt_dist[df] = fmt_dist.get(df, 0) + 1
            dm = _ad_domain(snap)
            if dm:
                dom_dist[dm] = dom_dist.get(dm, 0) + 1
        for im in _normalize(it):
            if im["u"] in seen:
                continue
            seen.add(im["u"])
            images.append(im)
            if len(images) >= max_ads:
                break
        if len(images) >= max_ads:
            break
    # 승자(오래·활성 집행) 소재를 앞으로: 활성 우선 → 집행일수 내림차순
    images.sort(key=lambda im: (1 if im.get("act") else 0, im.get("days") or -1), reverse=True)
    dl = [im["days"] for im in images if isinstance(im.get("days"), int)]
    perf_sum = {"maxDays": max(dl) if dl else None,
                "active": sum(1 for im in images if im.get("act")),
                "winners": sum(1 for d in dl if d >= 30)}   # 30일+ 집행 = 검증 소재
    ad_domain = max(dom_dist, key=dom_dist.get) if dom_dist else None   # 최빈 광고주 도메인
    logs.append("[sc] 완료 kw=%s images=%d formats=%s perf=%s domain=%s" % (kw, len(images), fmt_dist, perf_sum, ad_domain))
    if probe and items:
        logs.append("PROBE:" + json.dumps(_raw_probe(items[0]), ensure_ascii=False))
    return {"kw": kw, "images": images, "count": len(images), "formats": fmt_dist,
            "perf": perf_sum, "adDomain": ad_domain, "at": _now_kst(),
            "source": "scrapecreators_live", "precise": precise}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        kw = _fix_kr((qs.get("kw", [""])[0] or "").strip())
        name = _fix_kr((qs.get("name", [""])[0] or "").strip()) or kw
        page_url = _fix_kr((qs.get("url", [""])[0] or "").strip())
        debug = (qs.get("debug", [""])[0] or "").strip() in ("1", "true", "yes")
        probe = (qs.get("probe", [""])[0] or "").strip() in ("1", "true", "yes")
        if not kw and not page_url:
            return self._send({"error": "kw 또는 url이 필요합니다"}, 400)
        logs = []
        try:
            res = collect(kw or name, logs=logs, page_url=page_url or None, probe=probe)
        except Exception as e:
            return self._send({"error": str(e)[:200], "logs": logs}, 500)
        if res is None:
            return self._send({"error": "미설정 또는 수집 실패", "logs": logs}, 503)
        res["name"] = name
        if debug or probe:
            res["logs"] = logs
        self._send(res, 200)

    def _send(self, obj, code):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # 소재가 실제로 담긴 성공 응답만 Vercel CDN에 캐시 → 같은 경쟁사(kw/url) 반복 조회 시
        # 6시간에 딱 1번만 실제 ScrapeCreators 호출(유료), 그 사이는 CDN이 응답(비용 0). 실패·0건은
        # 캐시 금지(간헐적 0건이 6h 굳는 것 방지·다음 조회 재시도 가능).
        # (이전 no-store는 매 조회마다 유료 실행 → 월 한도 초과 사고의 원인)
        if code == 200 and (obj.get("count") or 0) > 0:
            self.send_header("Cache-Control", "public, s-maxage=21600, stale-while-revalidate=86400")
        else:
            self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
