"""Vercel 서버리스 함수 — 경쟁사 Google 광고 소재 온디맨드(실시간) 조회.

Apify(experthasan)에서 **ScrapeCreators**로 교체(2026-09-05). 이유: experthasan은
Apify 액터라 Meta와 같은 Apify 월 한도 지갑을 공유 → 구글($5/1000)이 그 지갑을
반복적으로 태웠다. ScrapeCreators는 완전히 별도 계정/과금이라 Apify 한도와 무관.

액터 대신 REST API:
  GET https://api.scrapecreators.com/v1/google/company/ads
    ?domain=<도메인>&region=KR&format=image&topic=all
  헤더: x-api-key: <SCRAPECREATORS_API_KEY>
  - format=image → 디스플레이 배너만(텍스트·검색 광고 클러터 원천 차단)
  - 기본 검색 1크레딧/요청(get_ad_details=25크레딧은 안 씀), 무료 100크레딧
  - 응답 ads[]: {advertiserId, creativeId, format, adUrl, advertiserName,
                 imageUrl(tpc.googlesyndication, image 광고만), firstShown, lastShown}

경쟁사 등록 정보의 홈페이지(home)/투명성센터 URL(google)에서 도메인·advertiser ID를
뽑아 조회한다. 프론트(index.html loadGoogle)는 "구글 광고 불러오기" 버튼을 눌렀을 때만
이 엔드포인트를 호출한다(유료 실행 최소화). 성공 응답은 CDN 24h 캐시.

  /api/competitor_google_ads?home=<홈페이지>&url=<투명성센터URL>&name=<표시명>
  (domain= 직접 지정도 가능. debug=1 이면 진단 로그 포함)
"""
import os
import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

API_URL = "https://api.scrapecreators.com/v1/google/company/ads"
ADV_URL = "https://api.scrapecreators.com/v1/google/adLibrary/advertisers/search"
MAX_ADS = 24
KST = timezone(timedelta(hours=9))
_now_kst = lambda: datetime.now(KST).strftime("%Y-%m-%d %H:%M")   # Vercel 서버는 UTC라 KST로 보정


def _adv_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("advertisers", "results", "data", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return v
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def _norm_name(s):
    return "".join((s or "").split()).lower()


def _adv_id(it):
    for k in ("advertiserId", "advertiser_id", "id", "advertiserID"):
        if it.get(k):
            return str(it[k])
    return None


def _resolve_advertiser(name, region, api_key, logs):
    """이름 → Google 광고주 검색 → advertiser_id. '브이티'처럼 흔한 이름은 후보가 여러 개라
    첫 결과가 엉뚱할 수 있으므로(예: (주)우전브이티) 이름 유사도로 랭킹해 최적 후보를 고른다."""
    try:
        r = requests.get(ADV_URL, params={"query": name, "region": region},
                         headers={"x-api-key": api_key}, timeout=30)
    except Exception as e:
        logs.append("[google] 광고주 검색 실패: %s" % str(e)[:120])
        return None
    if r.status_code >= 400:
        logs.append("[google] 광고주 검색 status=%s body=%s" % (r.status_code, r.text[:140]))
        return None
    try:
        data = r.json()
    except Exception:
        return None
    cands = []
    for it in _adv_items(data):
        aid = _adv_id(it)
        if aid:
            cands.append((aid, it.get("name") or it.get("advertiserName") or ""))
    if not cands:
        logs.append("[google] 광고주 검색 '%s' → 결과 없음" % name)
        return None, None
    q = _norm_name(name)

    def score(nm):
        n = _norm_name(nm)
        if n == q:
            return (0, len(n))          # 정확히 같은 이름 최우선
        if n.startswith(q) or q + "cosmetic" in n or "vt" == n or n.startswith("vt"):
            return (1, len(n))
        if q in n:
            return (2, len(n))          # 포함이면 짧은 이름 우선(유통사 접두어 붙은 긴 이름 후순위)
        return (9, len(n))

    cands.sort(key=lambda c: score(c[1]))
    logs.append("[google] 광고주 후보: " + " · ".join("%s(%s…)" % (c[1], c[0][:8]) for c in cands[:5]))
    best = cands[0]
    logs.append("[google] 선택 advertiser_id=%s (%s) — 부정확하면 투명성센터 URL 지정 권장" % (best[0], best[1]))
    return best[0], best[1]


def _fix_kr(s):
    try:
        return s.encode("latin-1").decode("utf-8")
    except UnicodeError:
        try:
            return s.encode("utf-8", "surrogateescape").decode("utf-8")
        except UnicodeError:
            return s


def _host_to_domain(host):
    host = (host or "").strip().lower()
    if not host:
        return None
    if "//" not in host and "/" in host:
        host = host.split("/", 1)[0]
    if "//" in host:
        host = urlparse(host if "://" in host else "http://" + host).netloc
    host = host.split("/")[0].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _resolve_target(domain, google_url, home):
    """(searchType, key) 결정. advertiser ID 우선, 없으면 도메인.
    반환 예: ('advertiser_id', 'AR123...') 또는 ('domain', 'nike.com')."""
    if google_url:
        try:
            u = urlparse(google_url)
            for seg in (u.path or "").split("/"):
                if seg.startswith("AR") and seg[2:].isdigit():
                    return ("advertiser_id", seg)
            q = parse_qs(u.query)
            if q.get("advertiserId"):
                return ("advertiser_id", q["advertiserId"][0].strip())
            if q.get("domain"):
                d = _host_to_domain(q["domain"][0])
                if d:
                    return ("domain", d)
        except Exception:
            pass
    d = _host_to_domain(domain)
    if d:
        return ("domain", d)
    d = _host_to_domain(home)
    if d:
        return ("domain", d)
    return (None, None)


def _parse_ts(s):
    """'2024-03-15' / ISO8601('...T..Z' / '+09:00') / unix → epoch초 (실패 시 None)."""
    if isinstance(s, (int, float)) and s > 0:
        return float(s)
    if not (isinstance(s, str) and s.strip()):
        return None
    t = s.strip().replace("T", " ")
    t = t.split(".")[0].split("+")[0].replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y"):
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _region_dates(ad):
    """일부 응답은 top-level firstShown 이 비고 실제 시작·종료일이 regionStats(지역별)에
    들어온다. regionStats/creativeRegions/regions 리스트를 훑어 first/last 로 보이는 값을
    모두 모아 (가장 이른 시작, 가장 늦은 종료)를 돌려준다."""
    firsts, lasts = [], []
    for coll in (ad.get("regionStats"), ad.get("creativeRegions"), ad.get("regions")):
        if not isinstance(coll, list):
            continue
        for it in coll:
            if not isinstance(it, dict):
                continue
            for k, v in it.items():
                kl = k.lower()
                if "first" in kl and "shown" in kl:
                    t = _parse_ts(v)
                    if t:
                        firsts.append(t)
                elif ("last" in kl and "shown" in kl) or kl in ("startdate", "enddate"):
                    t = _parse_ts(v)
                    if t:
                        lasts.append(t)
    return (min(firsts) if firsts else None, max(lasts) if lasts else None)


def _perf(ad):
    """성과 프록시: 얼마나 오래·최근까지 집행했는가(광고주는 안 먹히는 소재를 바로 끔).
    Google은 is_active가 없어 lastShown이 최근(≤10일)이면 활성으로 본다."""
    sd = _parse_ts(ad.get("firstShown") or ad.get("first_shown") or ad.get("firstShownDate")
                   or ad.get("startDate") or ad.get("start_date"))
    ls = _parse_ts(ad.get("lastShown") or ad.get("last_shown") or ad.get("lastShownDate")
                   or ad.get("endDate") or ad.get("end_date"))
    if sd is None or ls is None:          # top-level 이 비면 지역별(regionStats)에서 보강
        rf, rl = _region_dates(ad)
        sd = sd or rf
        ls = ls or rl
    days, act, since, last = None, False, None, None
    now = datetime.now(timezone.utc).timestamp()
    if sd:
        end = ls if (ls and ls > sd) else now
        days = int((end - sd) // 86400)
        since = datetime.fromtimestamp(sd, timezone.utc).strftime("%Y-%m-%d")
    # ⚠️ ScrapeCreators 구글은 firstShown 을 주지 않는 경우가 대부분(top-level·regionStats 모두 null)
    #    → 시작일이 없어 집행일수(days) 계산 불가. 확실한 건 lastShown(마지막 노출)뿐이라
    #    그걸 활성 판정과 표시(최근 노출일)에 쓴다. (억지 days 생성 금지)
    if ls:
        last = datetime.fromtimestamp(ls, timezone.utc).strftime("%Y-%m-%d")
        if (now - ls) <= 10 * 86400:
            act = True
    return {"days": days, "act": act, "since": since, "last": last, "lts": ls or 0}


def _looks_img(s):
    """구글 광고 이미지 URL 판별(랜딩 URL 오인 방지). 구글 디스플레이 크리에이티브는
    googlesyndication / googleusercontent / ggpht 호스트이거나 이미지 확장자."""
    if not isinstance(s, str) or not s.startswith("http"):
        return False
    sl = s.lower().split("?")[0]
    return ("googlesyndication" in sl or "googleusercontent" in sl or "ggpht" in sl
            or "gstatic" in sl or "ytimg" in sl        # ytimg = 유튜브 영상 썸네일
            or sl.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")))


def _deep_img(o, depth=0):
    """get_ad_details 응답 구조가 제각각이라(필드명·중첩 위치 변동) 객체 전체를 훑어
    이미지처럼 보이는 첫 URL을 찾는다."""
    if depth > 6:
        return None
    if isinstance(o, str):
        return o if _looks_img(o) else None
    if isinstance(o, dict):
        for v in o.values():
            r = _deep_img(v, depth + 1)
            if r:
                return r
    elif isinstance(o, list):
        for it in o:
            r = _deep_img(it, depth + 1)
            if r:
                return r
    return None


def _all_imgs(o, depth=0, acc=None, seen=None):
    """한 광고 객체 안의 '이미지처럼 보이는' URL을 전부(중복 제거) 수집.
    반응형 광고면 마케팅이미지·로고·합성 스크린샷이 여러 개 나올 수 있어 — 깨끗한 소재를
    고르려면 후보가 몇 개인지부터 봐야 한다(probe 진단용)."""
    if acc is None:
        acc, seen = [], set()
    if depth > 7 or len(acc) >= 12:
        return acc
    if isinstance(o, str):
        if _looks_img(o) and o not in seen:
            seen.add(o)
            acc.append(o)
    elif isinstance(o, dict):
        for v in o.values():
            _all_imgs(v, depth + 1, acc, seen)
    elif isinstance(o, list):
        for it in o:
            _all_imgs(it, depth + 1, acc, seen)
    return acc


def _normalize(ads):
    """ScrapeCreators ads[] → 프론트 da-item {u,t,type,성과}. 이미지 못 찾으면(텍스트 광고 등) 건너뜀."""
    out = []
    for ad in ads:
        img = ad.get("imageUrl") if _looks_img(ad.get("imageUrl")) else _deep_img(ad)
        if not img:
            continue
        f = (ad.get("format") or ad.get("adFormat") or ad.get("creativeFormat") or "").lower()
        ty = "video" if ("video" in f or "youtube" in f) else ("text" if "text" in f else "image")
        p = _perf(ad)
        out.append({"u": img, "t": ad.get("advertiserName") or ad.get("advertiser_name") or "", "type": ty,
                    "days": p["days"], "act": p["act"], "since": p["since"],
                    "last": p["last"], "lts": p["lts"]})
    return out


def collect(target, country="KR", max_ads=MAX_ADS, logs=None, probe=False, name=None):
    logs = logs if logs is not None else []
    api_key = os.environ.get("SCRAPECREATORS_API_KEY")
    if not api_key:
        logs.append("⚠️ [google] SCRAPECREATORS_API_KEY 없음 — 건너뜀")
        return None
    stype, key = target
    adv_name, adv_by = None, ("url" if stype == "advertiser_id" else None)
    # URL(투명성센터)로 advertiser_id 가 이미 잡혔으면 그걸 신뢰(정확). 없을 때만 이름 검색 폴백.
    if name and stype != "advertiser_id":
        aid, adv_name = _resolve_advertiser(name, country, api_key, logs)
        if aid:
            stype, key, adv_by = "advertiser_id", aid, "name"
    if not key:
        logs.append("⚠️ [google] 광고주/도메인을 확인할 수 없음 (이름 검색 실패 · 홈페이지 URL 등록 필요)")
        return None
    # ⚠️ 2025-11-10 ScrapeCreators 변경: get_ad_details 없이는 advertiserId·creativeId 만 오고
    # imageUrl 이 안 온다(=소재 0개로 보임). 소재를 받으려면 get_ad_details=true 필수(광고당 25크레딧).
    params = {"topic": "all", "region": country, "format": "image", "get_ad_details": "true"}
    params["advertiser_id" if stype == "advertiser_id" else "domain"] = key
    logs.append("[google] 조회 %s=%s (scrapecreators · get_ad_details) " % (stype, key))
    try:
        r = requests.get(API_URL, params=params,
                         headers={"x-api-key": api_key}, timeout=45)
    except Exception as e:
        logs.append("❌ [google] 요청 실패: %s" % str(e)[:200])
        return None
    if r.status_code >= 400:
        logs.append("❌ [google] status=%s body=%s" % (r.status_code, r.text[:200]))
        return None
    try:
        data = r.json()
    except Exception:
        logs.append("❌ [google] JSON 파싱 실패")
        return None
    ads = data.get("ads") or []
    logs.append("[google] ads=%d credits_remaining=%s" % (len(ads), data.get("credits_remaining")))
    images, seen = [], set()
    for im in _normalize(ads):
        if im["u"] in seen:
            continue
        seen.add(im["u"])
        images.append(im)
        if len(images) >= max_ads:
            break
    # 구글은 days(집행일수)가 없으니 활성 먼저 → 최근 노출(lts) 순으로 정렬(정직한 순서).
    images.sort(key=lambda im: (1 if im.get("act") else 0, im.get("days") or -1, im.get("lts") or 0), reverse=True)
    dl = [im["days"] for im in images if isinstance(im.get("days"), int)]
    perf_sum = {"maxDays": max(dl) if dl else None,
                "active": sum(1 for im in images if im.get("act")),
                "winners": sum(1 for d in dl if d >= 30)}
    logs.append("[google] 완료 %s=%s images=%d perf=%s" % (stype, key, len(images), perf_sum))
    if probe and ads:
        # 앞 3개 광고에 대해: 필드 키 · imageUrl · format · 발견된 모든 이미지 후보 URL.
        # 후보가 광고당 2개 이상이면 '깨끗한 asset vs 합성 스크린샷'을 구분해 뗄 여지가 있다.
        dump = [{"keys": list(ad.keys()),
                 "format": ad.get("format") or ad.get("adFormat") or ad.get("creativeFormat"),
                 "firstShown": ad.get("firstShown"), "lastShown": ad.get("lastShown"),
                 "regionStats": ad.get("regionStats"),
                 "allImgs": _all_imgs(ad)} for ad in ads[:2]]
        logs.append("PROBE:" + json.dumps(dump, ensure_ascii=False)[:1400])
    return {"target": "%s:%s" % (stype, key), "images": images,
            "count": len(images), "perf": perf_sum, "at": _now_kst(),
            "source": "scrapecreators_live", "precise": (adv_by == "url"),
            "advertiser": adv_name, "advBy": adv_by}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        gv = lambda k: _fix_kr((qs.get(k, [""])[0] or "").strip())
        name = gv("name")
        target = _resolve_target(gv("domain"), gv("url"), gv("home"))
        debug = gv("debug") in ("1", "true", "yes")
        probe = gv("probe") in ("1", "true", "yes")
        if not target[1] and not name:
            return self._send({"error": "도메인·홈페이지 URL 또는 경쟁사명이 필요합니다",
                               "images": []}, 400)
        logs = []
        try:
            res = collect(target, logs=logs, probe=probe, name=name)
        except Exception as e:
            return self._send({"error": str(e)[:200], "logs": logs}, 500)
        if res is None:
            return self._send({"error": "미설정 또는 수집 실패", "images": [],
                               "logs": logs}, 503)
        res["name"] = name
        if debug or probe:
            res["logs"] = logs
        self._send(res, 200)

    def _send(self, obj, code):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # 소재가 담긴 성공만 CDN 캐시(24h) → 반복 조회 유료 실행 방지. 실패·0건은 캐시 금지.
        if code == 200 and (obj.get("count") or 0) > 0:
            self.send_header("Cache-Control", "public, s-maxage=86400, stale-while-revalidate=172800")
        else:
            self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
