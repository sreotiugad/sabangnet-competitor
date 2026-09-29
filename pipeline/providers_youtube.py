"""YouTube Data API v3 — 키워드 상위 영상(썸네일·조회수·길이).

필요 환경변수: YOUTUBE_API_KEY  (Google Cloud 에서 'YouTube Data API v3' 활성화 후 발급)
쿼터: search.list = 100units, videos.list = 1unit. 기본 10,000/일 → 검색 ~100회/일.
"""
import re
import os
import requests

SEARCH = "https://www.googleapis.com/youtube/v3/search"
VIDEOS = "https://www.googleapis.com/youtube/v3/videos"


def _api_key():
    return os.environ.get("YOUTUBE_API_KEY", "")


def _dur(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", str(iso or ""))
    if not m:
        return ""
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    if h:
        return f"{h}:{mi:02d}:{s:02d}"
    return f"{mi}:{s:02d}"


def _date(iso):
    s = str(iso or "")
    return s[:10].replace("-", ".") if len(s) >= 10 else ""


def fetch_videos(keyword, max_results=8, logs=None):
    """키워드 상위 영상 리스트. 키 없거나 실패 시 None."""
    logs = logs if logs is not None else []
    key = _api_key()
    if not key:
        return None
    try:
        # 검색 결과에 영상 아닌 항목이 섞여 걸러질 수 있어 넉넉히 받아 유효 영상으로 채운다
        # (search.list 쿼터는 개수와 무관하게 100units 고정 → 더 받아도 공짜).
        fetch_n = min(50, max_results + 6)
        r = requests.get(SEARCH, params={
            "part": "snippet", "q": keyword, "type": "video",
            "maxResults": fetch_n, "order": "relevance",
            "regionCode": "KR", "relevanceLanguage": "ko", "key": key,
        }, timeout=15)
        if r.status_code != 200:
            logs.append(f"[yt] {keyword} search status={r.status_code}")
            return None
        items = r.json().get("items", [])
        ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
        if not ids:
            return []
        stats = {}
        r2 = requests.get(VIDEOS, params={
            "part": "statistics,contentDetails", "id": ",".join(ids), "key": key,
        }, timeout=15)
        if r2.status_code == 200:
            for v in r2.json().get("items", []):
                stats[v["id"]] = v
        out = []
        for it in items:
            if len(out) >= max_results:
                break
            vid = it.get("id", {}).get("videoId")
            if not vid:
                continue
            sn = it.get("snippet", {})
            st = stats.get(vid, {})
            thumbs = sn.get("thumbnails", {})
            thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url")
            out.append({
                "title": sn.get("title", ""),
                "channel": sn.get("channelTitle", ""),
                "views": int(st.get("statistics", {}).get("viewCount", 0) or 0),
                "date": _date(sn.get("publishedAt")),
                "thumb": thumb,
                "url": f"https://youtu.be/{vid}",
                "dur": _dur(st.get("contentDetails", {}).get("duration")),
            })
        logs.append(f"[yt] {keyword} 영상 {len(out)}개")
        return out
    except Exception as e:
        logs.append(f"[yt] {keyword} 오류: {e}")
        return None
