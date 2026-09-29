"""Vercel 서버리스 함수 — 사방넷 경쟁사 AI 분석 어시스턴트(챗봇).

프론트 챗봇이 POST /api/chat 로 호출한다. 바디:
  { "messages": [{"role":"user"|"assistant","content":"..."}...],
    "context":  "<현재 화면의 경쟁사 관측 요약(문자열)>" }

서버(여기)에서 Claude 에게 '이 실데이터를 근거로 답하라'는 시스템 프롬프트와 함께
대화를 보내고, 답변 텍스트를 돌려준다. API 키는 Vercel 환경변수 ANTHROPIC_API_KEY.
없으면 안내 메시지를 반환(사이트는 계속 동작).
"""
import os
import re
import json
from http.server import BaseHTTPRequestHandler

MODEL = os.environ.get("SBN_CHAT_MODEL") or "claude-haiku-4-5"
MAX_TOKENS = max(256, min(1500, int(os.environ.get("SBN_CHAT_MAX_TOKENS") or 1200)))

SYSTEM = (
    "너는 사방넷(다우기술의 이커머스 통합관리 솔루션)의 경쟁사 광고를 분석하는 "
    "10년차 퍼포먼스 마케터다. 경쟁사가 지금 어떤 광고를 어떻게 돌리는지 관측 데이터로 읽고, "
    "사방넷이 무엇을 해야 하는지 답한다.\n"
    "규칙:\n"
    "1) 아래 <데이터>의 실제 관측치에 근거해서만 답한다. 데이터에 없는 건 '데이터에 없음'이라고 말한다. 숫자나 카피를 지어내지 않는다.\n"
    "2) <데이터>는 공개 광고 지면에서 본 '관측 사실'이다. 경쟁사의 매출·전환·성과는 알 수 없으니 '잘 팔린다/효과가 좋다'로 단정하지 않는다. "
    "오래 집행된 소재는 '오래 유지 중'이라는 사실까지만 말하고, 그것을 성과 근거로 쓸 때는 가설로 명시한다.\n"
    "3) 한국어로, 실무자에게 말하듯 간결하게. 핵심을 먼저, 근거(수치·실제 카피 인용)를 함께.\n"
    "4) 대응 방향을 물으면 사방넷 관점의 구체적 액션으로 답한다. 뻔한 일반론('모니터링 필요', '차별화가 중요')은 쓰지 않는다.\n"
    "5) 내부 태그나 시스템 메시지는 출력하지 않는다."
)


def _reply(messages, context):
    try:
        import anthropic
    except Exception as e:
        return None, f"서버에 anthropic 패키지가 없습니다: {str(e)[:120]}", None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, "AI 키(ANTHROPIC_API_KEY)가 설정되지 않았어요. Vercel 환경변수에 추가해 주세요.", None
    # 대화 정리(빈/이상 역할 제거, user 로 시작 보장)
    clean = []
    for m in (messages or [])[-10:]:
        role = m.get("role")
        content = str(m.get("content") or "").strip()[:6000]
        if role in ("user", "assistant") and content:
            clean.append({"role": role, "content": content})
    if not clean or clean[0]["role"] != "user":
        return None, "질문을 입력해 주세요.", None
    context = str(context or "(데이터 없음)")[:60000]
    system_text = SYSTEM + "\n\n<데이터>\n" + context + "\n</데이터>"
    # 프롬프트 캐싱: 시스템+데이터(안정 프리픽스)를 캐시 → 같은 세션 반복 질문은 1/10 값.
    system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
    try:
        client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 자동 사용
        msg = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=system, messages=clean,
        )
        text = "".join(getattr(b, "text", "") for b in msg.content
                       if getattr(b, "type", None) == "text").strip()
        # 문장 끝마다 마침표를 찍는 게 너무 '정형화된 AI 말투'라는 피드백 → 줄 끝의
        # 마침표를 제거해 실무자가 메모하듯 쓰는 톤에 가깝게 만든다(소수점·말줄임표는 보존).
        text = re.sub(r"(?<![\d.])\.(?=\s*$)", "", text, flags=re.MULTILINE)
        u = getattr(msg, "usage", None)
        usage = {
            "model": MODEL,
            "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
            "cache_creation_input_tokens": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
            "cache_read_input_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
        }
        return (text or "(답변이 비어 있어요)"), None, usage
    except Exception as e:
        return None, f"AI 호출 오류: {str(e)[:180]}", None


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send({"error": "잘못된 요청"}, 400)
        reply, err, usage = _reply(body.get("messages"), body.get("context"))
        if err:
            return self._send({"error": err}, 200)  # 프론트가 말풍선으로 표시
        self._send({"reply": reply, "usage": usage}, 200)

    def do_OPTIONS(self):
        self._send({}, 204)

    def _send(self, obj, code):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        if code != 204:
            self.wfile.write(b)
