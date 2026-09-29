"""Vercel 서버리스 함수 — 실시간 키워드 조회.

브라우저에서 아무 키워드나 입력하면 /api/keyword?q=<kw> 로 호출된다.
서버(여기)에서 네이버 키워드툴·데이터랩·YouTube 를 실시간으로 긁어
프론트 키워드 카드가 기대하는 JSON 을 돌려준다. (API 키는 Vercel 환경변수)

필요한 Vercel 환경변수(서버측):
  DAOU_AD_ACCOUNTS  또는  NAVER1_CUSTOMER_ID/NAVER1_API_KEY/NAVER1_SECRET_KEY
  NAVER_DEV_CLIENT_ID / NAVER_DEV_CLIENT_SECRET   (DataLab·콘텐츠수, 선택)
  YOUTUBE_API_KEY                                  (영상 순위, 선택)
없거나 실패하면 추정치(_demo=true)로 폴백한다.
"""
import os
import sys
import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import config as C          # noqa: E402
import keywords_naver as KW  # noqa: E402
import sample as S           # noqa: E402


def _lookup(kw, logs):
    """(data, demo) 반환. 네이버 실데이터 성공 시 demo=False. 진단로그는 logs 에 누적."""
    acc = C.naver_account()
    if acc:
        try:
            d = KW.fetch_keyword(kw, acc, logs)
            if d:
                # YouTube 키 없으면 가짜 영상 대신 빈 목록(프론트가 안내문구 표시)
                if not os.environ.get("YOUTUBE_API_KEY"):
                    d["youtube"] = []
                return d, False
            logs.append("[lookup] fetch_keyword 결과 없음 → 추정치 폴백")
        except Exception as e:
            logs.append(f"[lookup] fetch_keyword 예외: {str(e)[:160]}")
    else:
        logs.append("[lookup] naver_account() 없음 → 네이버 키 미설정")
    return S.build_sample_keyword(kw), True


def _diag(kw, logs):
    """비밀값은 절대 노출하지 않고, 어느 단계에서 막혔는지 '유무/에러'만 반환."""
    raw = os.environ.get("DAOU_AD_ACCOUNTS")
    nfound = 0
    parse_err = None
    if raw:
        try:
            nfound = len(json.loads(raw).get("naver") or [])
        except Exception as e:
            parse_err = str(e)[:120]
    acc = C.naver_account()
    return {
        "kw": kw,
        "env_seen": {
            "NAVER1_CUSTOMER_ID": bool(os.environ.get("NAVER1_CUSTOMER_ID")),
            "NAVER1_API_KEY": bool(os.environ.get("NAVER1_API_KEY")),
            "NAVER1_SECRET_KEY": bool(os.environ.get("NAVER1_SECRET_KEY")),
            "DAOU_AD_ACCOUNTS": bool(raw),
            "DAOU_AD_ACCOUNTS_naver_count": nfound,
            "DAOU_AD_ACCOUNTS_parse_error": parse_err,
            "NAVER_DEV_CLIENT_ID": bool(os.environ.get("NAVER_DEV_CLIENT_ID")),
            "NAVER_DEV_CLIENT_SECRET": bool(os.environ.get("NAVER_DEV_CLIENT_SECRET")),
            "YOUTUBE_API_KEY": bool(os.environ.get("YOUTUBE_API_KEY")),
        },
        "naver_account_resolved": bool(acc),
        "logs": logs,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        q = (qs.get("q", [""])[0] or "").strip()
        # Vercel 서버리스가 쿼리스트링을 latin-1/surrogateescape 로 디코드해 한글이 깨지는 경우 UTF-8 복원.
        # 1) latin-1 mojibake  2) surrogateescape mojibake  3) 이미 정상(무해한 round-trip) 순으로 안전 처리.
        try:
            q = q.encode("latin-1").decode("utf-8")
        except UnicodeError:
            try:
                q = q.encode("utf-8", "surrogateescape").decode("utf-8")
            except UnicodeError:
                pass
        debug = (qs.get("debug", [""])[0] or "").strip() in ("1", "true", "yes")
        if not q:
            return self._send({"error": "q(키워드) 파라미터가 필요합니다"}, 400)
        logs = []
        try:
            data, demo = _lookup(q, logs)
        except Exception as e:
            return self._send({"error": str(e)[:200]}, 500, cache=False)
        if debug:
            # 진단은 절대 캐시 안 함 — 매번 실시간 상태를 봐야 함
            return self._send(_diag(q, logs), 200, cache=False)
        out = dict(data)
        out["kw"] = q
        out["_demo"] = demo
        # 실데이터만 1시간 캐시. 추정치(폴백)는 캐시 안 함 → 키 고치면 즉시 반영.
        self._send(out, 200, cache=not demo)

    def _send(self, obj, code, cache=True):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cache:
            # 같은 키워드 1시간 캐시(브라우저/CDN) — YouTube·네이버 쿼터 절약
            self.send_header("Cache-Control", "public, max-age=3600, s-maxage=3600")
        else:
            self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
