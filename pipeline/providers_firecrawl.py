"""Firecrawl(파이어크롤) → 네이버 검색결과(SERP) 실데이터 경쟁사 수집.

기존 광고 API(keywordstool/stats/datalab)로는 **얻을 수 없는** 두 가지를 채운다.
  1) 이 키워드로 실제 네이버에 노출되는 '파워링크' 광고주 + 광고 카피(순서대로)
  2) 자연검색 상위 경쟁 사이트(도메인)
헤드리스 브라우저로 JS 렌더 후 LLM 구조화 추출(formats=[{type:'json'}]).

키 없으면(=FIRECRAWL_API_KEY 미설정) None 반환 → 호출부는 모델 추정값을 유지.
어떤 예외(레이트리밋·타임아웃·파싱실패)도 삼켜서 None → 리포트는 절대 깨지지 않는다.

필요 환경변수: FIRECRAWL_API_KEY (firecrawl.dev · 무료 500크레딧/월, 1 scrape=1크레딧)
"""
import os
import time
import base64
import html
import requests

try:
    from bs4 import BeautifulSoup
except Exception:                            # 미설치 시 파워링크 DOM 파싱만 조용히 스킵
    BeautifulSoup = None

API = "https://api.firecrawl.dev/v2/scrape"


def _embed_images(ads, logs):
    """스크랩 직후(URL 가장 신선할 때) 각 광고 이미지를 서버에서 내려받아
    base64 data URI 로 박아넣는다 → fbcdn 서명 URL 만료·핫링크 문제 제거.
    다운로드 실패(403 등)한 소재는 image='' (프론트가 이미지 없는 카드로 처리)."""
    from concurrent.futures import ThreadPoolExecutor
    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
           "Accept": "image/avif,image/webp,image/apng,image/*,*/*"}

    region = os.environ.get("VERCEL_REGION") or os.environ.get("AWS_REGION") or "?"
    diag = []   # 실패 진단(앞 몇 개) — signed query 전체는 남기지 않음

    def dl(a):
        raw = a.get("image", "")
        if not raw.startswith("http"):
            return
        had_amp = "&amp;" in raw
        u = html.unescape(raw)                       # &amp; 등 HTML 엔티티 복원
        host = u.split("//")[-1].split("/")[0]
        a["image"] = u                               # 실패해도 '복원된' URL 을 클라이언트에 넘김
        try:
            r = requests.get(u, headers=hdr, timeout=9, allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 500:
                ct = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
                if not ct.startswith("image/"):
                    ct = "image/jpeg"
                a["image"] = "data:" + ct + ";base64," + base64.b64encode(r.content).decode()
            else:
                body = ""
                try:
                    body = (r.text or "")[:80].replace("\n", " ")
                except Exception:
                    pass
                diag.append(f"{r.status_code} {host[:22]} ct={(r.headers.get('Content-Type') or '')[:16]} "
                            f"redir={len(r.history)} amp={had_amp} body={body}")
        except Exception as e:
            diag.append(f"ERR {host[:22]} amp={had_amp} {str(e)[:45]}")

    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(dl, ads))
    except Exception:
        pass
    n = sum(1 for a in ads if a.get("image", "").startswith("data:"))
    logs.append(f"[img] region={region} embed={n}/{len(ads)}")
    for d in diag[:5]:
        logs.append("[img] " + d)
    return n

# '우리' 브랜드 판별용 힌트(서비스명 + 도메인 조각). 광고주/사이트가 우리면 us=True.
_OUR_DOMAINS = ("sabangnet", "daou", "ddnews", "ppurio", "bizmailer",
                "npax", "addcon", "adwel")


def _key():
    return os.environ.get("FIRECRAWL_API_KEY") or ""


def _is_us(text, our_names):
    t = str(text or "").lower()
    for n in our_names:
        n = str(n or "").strip()
        if n and n.lower() in t:
            return True
    for d in _OUR_DOMAINS:
        if d in t:
            return True
    return False


def _domain(url):
    s = str(url or "").split("//")[-1].split("/")[0].strip()
    return s[4:] if s.startswith("www.") else s


# ── 파워링크: LLM이 아니라 DOM에서 직접 파싱 ────────────────────────────────
# 원래 sa-collector-extension(별도 프로젝트, 크롬 확장으로 사람 속도 수집)의
# extension/parse-powerlink.js 셀렉터를 그대로 옮겼는데, 실측(2026-09-05, "닥터자르트")
# 해보니 그 파서가 전제한 onclick(`a=pwl_nop...`) 트래킹 자체가 지금 네이버 마크업엔
# 없다 — 광고 링크가 이제 `href="https://ader.naver.com/v1/<불투명 토큰>?..."` 리다이렉트
# 방식이라 그 확장의 rank/naverAdId 추출은 더 이상 안 된다. 대신 브랜드명·제목·설명·표시URL
# 셀렉터(a.site, a.lnk_head span.lnk_tit, .desc_area a.link_desc, a.lnk_url)는 실측
# 클래스맵에서 그대로 확인됐다 — 우리는 rank/adId가 필요 없으므로 그 필드만 쓴다.
# `li.lst` 컨테이너 자체가 이미 "이건 파워링크 광고다"라는 신뢰할 수 있는 신호라
# 트래킹 매칭을 광고 판별 조건으로 쓸 필요가 없다.
def _pl_text(el):
    return el.get_text(strip=True) if el else ""


def parse_powerlink_html(raw_html, logs=None):
    """네이버 SERP 원본 HTML → 파워링크 광고 목록. DOM 마크업이 없거나 BeautifulSoup
    미설치면 빈 목록(호출부는 organic 만으로도 계속 진행). 실패를 조용히 삼키지 않고
    로그에 원인(마크업 없음/파싱 0건)을 남긴다."""
    logs = logs if logs is not None else []
    if BeautifulSoup is None:
        logs.append("[powerlink] beautifulsoup4 미설치 — DOM 파싱 스킵")
        return []
    if not raw_html:
        logs.append("[powerlink] HTML 없음(firecrawl rawHtml/html 미반환)")
        return []
    soup = BeautifulSoup(raw_html, "html.parser")
    pl = soup.select_one("#power_link_body")
    if not pl:
        logs.append("[powerlink] #power_link_body 없음 — 파워링크 미노출 또는 마크업 변경")
        return []
    items = pl.select("ul.lst_type > li.lst")
    ads, malformed = [], 0
    classmap_sample = None
    for li in items:
        display_url = _pl_text(li.select_one("a.lnk_url")).rstrip("/")
        brand = _pl_text(li.select_one("a.site")) or display_url
        if not brand:
            malformed += 1
            # 진단용 — brand/display_url 둘 다 못 뽑은 첫 케이스의 클래스맵을 남긴다.
            # 다음에 네이버가 또 마크업을 바꾸면 여기서 바로 새 셀렉터를 읽을 수 있게.
            if classmap_sample is None:
                classmap_sample = [el.name + "." + ".".join(el.get("class"))
                                    for el in li.find_all(True) if el.get("class")][:40]
            continue
        title = " / ".join(t for t in (_pl_text(el) for el in li.select("a.lnk_head span.lnk_tit")) if t)
        desc = _pl_text(li.select_one(".desc_area a.link_desc"))
        ads.append({"brand": brand, "title": title, "desc": desc, "url": display_url})
    logs.append(f"[powerlink] DOM 파싱 광고 {len(ads)}건" + (f" · 버려짐 {malformed}건(마크업 일부 변경?)" if malformed else ""))
    if classmap_sample:
        logs.append("[powerlink] 진단 클래스맵: " + " | ".join(classmap_sample))
    return ads


_ORGANIC_SCHEMA = {
    "type": "object",
    "properties": {
        "organic": {
            "type": "array",
            "description": "자연검색(비광고) 상위 웹사이트 결과, 순서대로",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                },
            },
        },
    },
}

_ORGANIC_PROMPT = (
    "이 네이버 검색결과 페이지에서 광고가 아닌 일반 웹사이트/사이트 검색 결과의 title·url을 "
    "상위 순서대로 최대 8개 추출해줘. 블로그·카페·지식iN·뉴스 섹션은 제외하고 사이트/웹 결과만. "
    "없으면 빈 배열."
)


def serp_competitors(kw, our_names=None, logs=None, timeout=45):
    """키워드 하나의 네이버 SERP 경쟁사. 실패/키없음 → None.
    파워링크(ads)는 DOM 파싱, 자연검색(organic)은 LLM 추출 — 둘을 한 번의 스크랩으로."""
    logs = logs if logs is not None else []
    if not _key():
        return None
    our_names = our_names or []
    url = "https://search.naver.com/search.naver?query=" + requests.utils.quote(kw)
    body = {
        "url": url,
        "formats": ["rawHtml", {"type": "json", "schema": _ORGANIC_SCHEMA, "prompt": _ORGANIC_PROMPT}],
        "onlyMainContent": False,          # 파워링크는 본문 밖 영역이라 포함시켜야 함
        "waitFor": 3500,                    # JS 렌더 대기
        "location": {"country": "KR", "languages": ["ko"]},
        "blockAds": False,                  # SERP 광고는 우리가 원하는 데이터라 차단 X
    }
    try:
        r = requests.post(API, json=body, timeout=timeout,
                          headers={"Authorization": "Bearer " + _key(),
                                   "Content-Type": "application/json"})
        if r.status_code != 200:
            logs.append(f"[firecrawl] {kw} status={r.status_code} {r.text[:120]}")
            return None
        data = (r.json() or {}).get("data") or {}
        raw_html = data.get("rawHtml") or data.get("html") or ""
        j = data.get("json") or {}
    except Exception as e:
        logs.append(f"[firecrawl] {kw} 오류: {str(e)[:100]}")
        return None

    ads_raw = parse_powerlink_html(raw_html, logs)
    org_raw = j.get("organic") or []
    ads, organic = [], []
    seen = set()
    for a in ads_raw:
        name = str(a.get("brand", "")).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        copy = str(a.get("title", "")).strip() or str(a.get("desc", "")).strip()
        u = str(a.get("url", "")).strip()
        # us 판정은 광고주명·표시URL만 본다 — 광고 카피(copy)는 절대 포함하지 않는다.
        # 실측(아이소이 추적 중 G마켓): 리셀러가 "G마켓 아이소이 특가" 식으로 카피에
        # 추적 대상 이름을 넣는 건 흔하다 — 카피까지 훑으면 그 리셀러가 추적 대상
        # 본인인 것처럼 오판정된다. 광고주명이나 URL 자체가 일치할 때만 본인으로 본다.
        ads.append({"name": name, "copy": copy, "url": u,
                    "us": _is_us(name + " " + u, our_names)})
        if len(ads) >= 10:
            break
    dseen = set()
    for o in org_raw:
        u = str(o.get("url", "")).strip()
        dom = _domain(u)
        if not dom or dom in dseen:
            continue
        dseen.add(dom)
        # organic은 title 그대로 유지한다 — 한글 브랜드명은 도메인(영문)과 절대 겹치지
        # 않아서 domain만 보면 매칭 자체가 항상 실패한다(구글 자동 도메인 감지 기능이
        # 바로 이 title 매칭에 의존함). 리뷰·리셀러 제목도 오탐 소지는 있지만 그건
        # ads와 달리 아직 신고된 적 없어 건드리지 않는다(별개로 확인 필요).
        organic.append({"title": str(o.get("title", "")).strip(), "url": u, "domain": dom,
                        "us": _is_us(u + " " + str(o.get("title", "")), our_names)})
        if len(organic) >= 8:
            break
    if not ads and not organic:
        logs.append(f"[firecrawl] {kw} 추출 0건(빈 SERP?)")
        return None
    logs.append(f"[firecrawl] {kw} 광고 {len(ads)} · 자연검색 {len(organic)} 실수집")
    return {"ads": ads, "organic": organic}


_PROMO_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "페이지 대표 헤드라인"},
        "summary": {"type": "string", "description": "이 페이지가 지금 밀고 있는 핵심 메시지 한 줄"},
        "promos": {
            "type": "array",
            "description": "현재 진행 중인 프로모션/혜택",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "할인/무료체험/이벤트/증정/기타"},
                    "headline": {"type": "string", "description": "프로모션 제목"},
                    "detail": {"type": "string", "description": "혜택 상세"},
                    "price": {"type": "string", "description": "가격·할인율(있으면)"},
                    "cta": {"type": "string", "description": "행동유도 버튼 문구"},
                    "deadline": {"type": "string", "description": "종료일/기간(있으면)"},
                },
            },
        },
    },
}

_PROMO_PROMPT = (
    "이 페이지에서 지금 진행 중인 프로모션·혜택을 추출해줘. "
    "각 프로모션의 종류(할인/무료체험/이벤트/증정/기타), 제목(headline), 상세(detail), "
    "가격·할인율(price), 행동유도 문구(cta), 종료일/기간(deadline). "
    "그리고 페이지 대표 헤드라인(headline)과, 이 페이지가 지금 밀고 있는 핵심 메시지를 한 줄로(summary). "
    "프로모션이 없으면 promos는 빈 배열."
)


def scrape_promo(url, logs=None, timeout=45):
    """프로모션/랜딩 페이지 스냅샷. 현재 혜택·가격·CTA·헤드라인. 실패/키없음 → None."""
    logs = logs if logs is not None else []
    if not _key() or not url:
        return None
    body = {
        "url": url,
        "formats": [{"type": "json", "schema": _PROMO_SCHEMA, "prompt": _PROMO_PROMPT}],
        "onlyMainContent": True,
        "waitFor": 3000,
        "location": {"country": "KR", "languages": ["ko"]},
    }
    try:
        r = requests.post(API, json=body, timeout=timeout,
                          headers={"Authorization": "Bearer " + _key(),
                                   "Content-Type": "application/json"})
        if r.status_code != 200:
            logs.append(f"[firecrawl] promo {url} status={r.status_code} {r.text[:100]}")
            return None
        data = (r.json() or {}).get("data") or {}
        j = data.get("json") or {}
    except Exception as e:
        logs.append(f"[firecrawl] promo {url} 오류: {str(e)[:100]}")
        return None
    promos = []
    for p in (j.get("promos") or []):
        hd = str(p.get("headline", "")).strip()
        dt = str(p.get("detail", "")).strip()
        if not (hd or dt):
            continue
        promos.append({"kind": str(p.get("kind", "")).strip() or "기타",
                       "headline": hd, "detail": dt,
                       "price": str(p.get("price", "")).strip(),
                       "cta": str(p.get("cta", "")).strip(),
                       "deadline": str(p.get("deadline", "")).strip()})
        if len(promos) >= 8:
            break
    out = {"headline": str(j.get("headline", "")).strip(),
           "summary": str(j.get("summary", "")).strip(),
           "promos": promos}
    logs.append(f"[firecrawl] promo {url} · 프로모션 {len(promos)}건")
    return out


_ADS_SCHEMA = {
    "type": "object",
    "properties": {
        "ads": {
            "type": "array",
            "description": "이 광고주가 현재 게재/노출 중인 광고들",
            "items": {
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "광고 크리에이티브 대표 이미지의 실제 src URL(http로 시작)"},
                    "headline": {"type": "string", "description": "광고 제목/헤드라인"},
                    "body": {"type": "string", "description": "광고 본문/기본 텍스트"},
                    "cta": {"type": "string", "description": "행동유도 버튼 문구(예: 더 알아보기)"},
                    "platform": {"type": "string", "description": "게재 지면(Facebook/Instagram/검색/디스플레이/YouTube 등)"},
                    "started": {"type": "string", "description": "게재 시작일(있으면)"},
                    "status": {"type": "string", "description": "활성/비활성"},
                },
            },
        },
    },
}

_ADS_PROMPT = (
    "이 페이지는 광고 투명성 센터 또는 광고 라이브러리야. 이 광고주가 지금 게재 중인 광고들을 추출해줘. "
    "각 광고 카드에서 image 는 **실제 광고 크리에이티브(상품·캠페인 메인 이미지, 카드에서 가장 큰 이미지)의 <img> src**만 넣어. "
    "카드 상단의 **광고주 프로필 사진·로고·아바타(작은 원형/정사각 썸네일)는 절대 image 로 쓰지 마.** "
    "그 외 headline(헤드라인), body(본문 텍스트), cta(버튼 문구), platform(게재 지면), started(시작일), status(상태). "
    "http 로 시작하는 실제 URL만. 최대 12개. 페이지 UI 아이콘은 제외."
)


def scrape_ads(url, logs=None, timeout=27):
    """Meta 광고 라이브러리 / Google 광고 투명성센터 페이지 → 게재 광고 소재(이미지·카피).
    JS 무한스크롤·봇차단으로 실패할 수 있음 → 실패/키없음/URL없음 시 None.
    timeout 은 Vercel 함수 상한(30s)보다 짧게 잡아 504 대신 깔끔한 None 이 되게 한다.
    (콜드 스크랩이 잘려도 Firecrawl 이 서버측에 캐시 → 프론트 재시도 시 빠르게 성공)"""
    logs = logs if logs is not None else []
    if not _key() or not url:
        return None
    body = {
        "url": url,
        "formats": ["markdown", {"type": "json", "schema": _ADS_SCHEMA, "prompt": _ADS_PROMPT}],
        "onlyMainContent": False,
        "waitFor": 4000,               # 광고 라이브러리 로드 대기(30s 상한 고려해 단축)
        "location": {"country": "KR", "languages": ["ko"]},
    }
    try:
        r = requests.post(API, json=body, timeout=timeout,
                          headers={"Authorization": "Bearer " + _key(),
                                   "Content-Type": "application/json"})
        if r.status_code != 200:
            logs.append(f"[firecrawl] ads {url[:60]} status={r.status_code} {r.text[:100]}")
            return None
        data = (r.json() or {}).get("data") or {}
        j = data.get("json") or {}
        md = data.get("markdown") or ""
    except Exception as e:
        logs.append(f"[firecrawl] ads {url[:60]} 오류: {str(e)[:100]}")
        return None
    # LLM 환각 방지: 페이지에서 광고를 못 읽으면 example.com·placeholder 이미지나
    # 뻔한 문구를 지어낸다. 가짜 이미지는 버리고, 진짜 이미지가 하나도 없으면 실패로 본다.
    _FAKE = ("example.com", "example.org", "example.net", "placeholder", "lorempix",
             "via.placeholder", "yourdomain", "dummyimage", "/ad1.", "/ad2.", "/ad3.")
    # 프로필사진·로고·아바타 경로만 정밀 차단(광고 소재 아님).
    # ⚠️ 주의: 'tXX.YYYYY' 대분류로 통으로 막으면 실제 광고 소재까지 날아간다.
    #   예) t39.30808-1 = 프로필 썸네일(버림), t39.30808-6 = 실제 미디어(살림).
    #   그래서 프로필 variant(-1)와 작은 정사각 썸네일 크기만 필터링한다.
    _PROFILE = ("t51.2885-19", "t39.30808-1", "t1.6435-9", "/profile", "avatar", "logo",
                "s60x60", "p60x60", "s100x100", "p100x100", "p148x148", "s148x148")

    def _real_img(u):
        u = str(u or "").strip()
        if not u.startswith("http"):
            return ""
        lo = u.lower()
        if any(f in lo for f in _FAKE) or any(p in lo for p in _PROFILE):
            return ""
        return u

    ads = []
    for a in (j.get("ads") or []):
        img = _real_img(a.get("image"))
        hd = str(a.get("headline", "")).strip()
        bd = str(a.get("body", "")).strip()
        if not (img or hd or bd):
            continue
        ads.append({"image": img, "headline": hd, "body": bd,
                    "cta": str(a.get("cta", "")).strip(),
                    "platform": str(a.get("platform", "")).strip(),
                    "started": str(a.get("started", "")).strip(),
                    "status": str(a.get("status", "")).strip()})
        if len(ads) >= 12:
            break
    n_url = sum(1 for a in ads if a["image"].startswith("http"))
    logs.append(f"[firecrawl] ads {url[:50]} · 소재 {len(ads)}개(이미지URL {n_url}) · md {len(md)}자")
    if n_url == 0:                       # 이미지 URL 0개 = 렌더실패/환각 → 링크 폴백
        return None
    emb = _embed_images(ads, logs)       # 스크랩 즉시 다운로드→base64(되면), 실패시 원본URL 유지
    ads = [a for a in ads if a["image"].startswith(("data:", "http"))]
    if not ads:
        return None
    return {"ads": ads, "imgCount": len(ads), "embedded": emb}


def page_shot(url, logs=None, timeout=55, full=True):
    """광고 라이브러리 페이지를 통째로 스크린샷 → 광고 그리드 이미지 한 장.
    fbcdn 개별 핫링크(만료·403) 대신, Firecrawl 이 렌더한 화면을 이미지로 박제한다.
    반환 {'shot': <스크린샷 이미지 URL>} 또는 None."""
    logs = logs if logs is not None else []
    if not _key() or not url:
        return None
    body = {
        "url": url,
        "formats": [{"type": "screenshot", "fullPage": full}],
        "onlyMainContent": False,
        "waitFor": 4000,
        "location": {"country": "KR", "languages": ["ko"]},
    }
    try:
        r = requests.post(API, json=body, timeout=timeout,
                          headers={"Authorization": "Bearer " + _key(),
                                   "Content-Type": "application/json"})
        if r.status_code != 200:
            logs.append(f"[firecrawl] shot {url[:50]} status={r.status_code} {r.text[:100]}")
            return None
        data = (r.json() or {}).get("data") or {}
        shot = data.get("screenshot") or ""
    except Exception as e:
        logs.append(f"[firecrawl] shot {url[:50]} 오류: {str(e)[:100]}")
        return None
    if not shot:
        logs.append(f"[firecrawl] shot {url[:50]} 스크린샷 없음")
        return None
    logs.append(f"[firecrawl] shot {url[:50]} OK")
    return {"shot": shot}
