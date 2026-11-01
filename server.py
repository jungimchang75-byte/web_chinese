"""교양중국어2 강의 사이트 서버 — 사이트 파일 + 실시간 퀴즈 + AI 문제 출제

실행:  python server.py            (기본 주소 http://localhost:8000)
환경변수
  PORT                  포트 번호 (Railway가 자동으로 넣어 줌)
  ANTHROPIC_API_KEY     AI 문제 출제용 Claude API 키 (없으면 '프롬프트 복사' 방식으로 안내)
  BAZE_API_KEY          (선택) 고려대학교 BAZE API Gateway 키. 화면에서 입력한 키가 있으면 그 키를 먼저 씀
  QUIZ_HOST_PASSWORD    설정하면 퀴즈 만들기·AI 출제에 이 비밀번호가 필요 (배포 시 꼭 설정)

퀴즈 방과 기록은 서버 메모리에만 있으므로, 끝나면 결과 CSV를 내려받아 두세요.
"""
import base64
import json
import os
import random
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import anthropic
except ImportError:  # SDK가 없으면 AI 출제만 꺼짐
    anthropic = None

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8000"))
HOST_PASSWORD = os.environ.get("QUIZ_HOST_PASSWORD", "")
MODEL = "claude-opus-5-5"
NOTE_SLIDES = 20  # 강의 노트 슬라이드 기본 장수
ROOM_TTL = 6 * 3600  # 6시간 지난 방은 정리
MAX_PLAYERS = 200
AI_LIMIT_PER_HOUR = 20

rooms = {}
rooms_lock = threading.Lock()
ai_calls = {}  # ip -> [timestamps]


# BAZE(고려대학교 API Gateway)의 Anthropic Messages API 호환 주소
# https://docs.factchat.kr/docs/korea-seoul/api-gateway/getting-started/overview
BAZE_BASE_URL = "https://api.baze.co/v1/gateway/claude"
BAZE_MODELS_URL = "https://api.baze.co/v1/gateway/models/"  # 키·모델 권한 확인용 (토큰 사용 없음)


def claude_available():
    return anthropic is not None and bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def baze_server_key():
    return os.environ.get("BAZE_API_KEY", "")


def ai_available():
    return claude_available() or (anthropic is not None and bool(baze_server_key()))


def lan_ip():
    """같은 Wi-Fi의 휴대폰이 접속할 수 있는 이 PC의 내부 IP (실제로 패킷을 보내지는 않음)"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


# ---------------------------------------------------------------- 퀴즈 방
class Room:
    def __init__(self, title, questions):
        self.title = title
        self.questions = questions  # [{q, choices[4], answer, time, explanation}]
        self.host_key = secrets.token_urlsafe(18)
        self.players = {}  # pid -> {name, sid, token, score, answers{qi: {...}}, joined}
        self.phase = "lobby"  # lobby | question | reveal | ended
        self.qi = -1
        self.q_started = 0.0
        self.q_deadline = 0.0
        self.version = 0
        self.cond = threading.Condition()
        self.timer = None
        self.created = time.time()

    # 아래 함수들은 self.cond를 잡은 상태에서 부릅니다.
    def bump(self):
        self.version += 1
        self.cond.notify_all()

    def start_question(self, i):
        if self.timer:
            self.timer.cancel()
        self.phase = "question"
        self.qi = i
        self.q_started = time.time()
        limit = self.questions[i]["time"]
        self.q_deadline = self.q_started + limit
        self.timer = threading.Timer(limit + 0.3, self._auto_reveal, (i,))
        self.timer.daemon = True
        self.timer.start()
        self.bump()

    def _auto_reveal(self, i):
        with self.cond:
            if self.phase == "question" and self.qi == i:
                self.phase = "reveal"
                self.bump()

    def reveal(self):
        if self.timer:
            self.timer.cancel()
        if self.phase == "question":
            self.phase = "reveal"
            self.bump()

    def leaderboard(self):
        def total_ms(p):
            return sum(a["ms"] for a in p["answers"].values() if a["correct"])

        ranked = sorted(self.players.items(), key=lambda kv: (-kv[1]["score"], total_ms(kv[1]), kv[1]["joined"]))
        return [{"pid": pid, "name": p["name"], "score": p["score"], "rank": i + 1} for i, (pid, p) in enumerate(ranked)]

    def counts(self, i):
        c = [0] * len(self.questions[i]["choices"])
        for p in self.players.values():
            a = p["answers"].get(i)
            if a:
                c[a["choice"]] += 1
        return c

    def answered(self, i):
        return sum(1 for p in self.players.values() if i in p["answers"])

    def remaining_ms(self):
        return max(0, int((self.q_deadline - time.time()) * 1000)) if self.phase == "question" else 0

    def question_public(self, i, with_answer):
        q = self.questions[i]
        d = {"index": i, "q": q["q"], "choices": q["choices"], "time": q["time"]}
        if with_answer:
            d["answer"] = q["answer"]
            d["explanation"] = q.get("explanation", "")
        return d

    def host_view(self, code):
        v = {
            "role": "host", "code": code, "title": self.title, "phase": self.phase, "qi": self.qi,
            "total": len(self.questions), "players": [p["name"] for p in self.players.values()],
            "remainingMs": self.remaining_ms(), "version": self.version,
        }
        if self.qi >= 0 and self.phase in ("question", "reveal"):
            v["question"] = self.question_public(self.qi, True)
            v["answered"] = self.answered(self.qi)
            v["counts"] = self.counts(self.qi) if self.phase == "reveal" else None
        if self.phase in ("reveal", "ended"):
            v["leaderboard"] = self.leaderboard()
        return v

    def player_view(self, pid):
        p = self.players.get(pid)
        v = {
            "role": "player", "title": self.title, "phase": self.phase, "qi": self.qi,
            "total": len(self.questions), "playerCount": len(self.players),
            "remainingMs": self.remaining_ms(), "version": self.version,
        }
        if not p:
            v["kicked"] = True
            return v
        board = self.leaderboard()
        me = next((b for b in board if b["pid"] == pid), None)
        v["me"] = {"name": p["name"], "score": p["score"], "rank": me["rank"] if me else None}
        if self.qi >= 0 and self.phase in ("question", "reveal"):
            reveal = self.phase == "reveal"
            v["question"] = self.question_public(self.qi, reveal)
            a = p["answers"].get(self.qi)
            v["myAnswer"] = a
            if reveal:
                v["counts"] = self.counts(self.qi)
        if self.phase in ("reveal", "ended"):
            v["top"] = [{"name": b["name"], "score": b["score"], "rank": b["rank"]} for b in board[:5]]
        return v

    def results(self, code):
        board = {b["pid"]: b["rank"] for b in self.leaderboard()}
        players = []
        for pid, p in self.players.items():
            players.append({
                "name": p["name"], "studentId": p["sid"], "score": p["score"], "rank": board.get(pid),
                "answers": [p["answers"].get(i) for i in range(len(self.questions))],
            })
        players.sort(key=lambda x: x["rank"] or 9999)
        return {
            "code": code, "title": self.title, "createdAt": self.created,
            "questions": [self.question_public(i, True) for i in range(len(self.questions))],
            "players": players,
        }


def new_code():
    while True:
        code = "%06d" % random.randint(0, 999999)
        if code not in rooms:
            return code


def clean_rooms():
    now = time.time()
    for code in [c for c, r in rooms.items() if now - r.created > ROOM_TTL]:
        del rooms[code]


def clean_questions(raw):
    out = []
    for q in raw or []:
        text = str(q.get("q") or q.get("question") or "").strip()
        choices = [str(c).strip() for c in (q.get("choices") or []) if str(c).strip()]
        try:
            answer = int(q.get("answer"))
        except (TypeError, ValueError):
            answer = -1
        try:
            limit = int(q.get("time") or 20)
        except (TypeError, ValueError):
            limit = 20
        if not text or not 2 <= len(choices) <= 4 or not 0 <= answer < len(choices):
            continue
        out.append({
            "q": text[:300], "choices": [c[:120] for c in choices], "answer": answer,
            "time": min(120, max(5, limit)), "explanation": str(q.get("explanation") or "")[:300],
        })
    return out


# ---------------------------------------------------------------- AI 문제 출제
QUIZ_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "choices": {"type": "array", "items": {"type": "string"}},
                    "answer": {"type": "integer"},
                    "explanation": {"type": "string"},
                },
                "required": ["question", "choices", "answer", "explanation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def build_prompt(topic, level, count, extra):
    return (
        "고려대학교 교양 과목 「교양중국어2」(교양중국어1을 마친 학부생 대상) 수업에서 휴대폰으로 푸는 실시간 퀴즈 문제를 만들어 주세요.\n\n"
        f"- 주제: {topic}\n- 난이도: {level}\n- 문항 수: {count}개\n"
        + (f"- 추가 요청: {extra}\n" if extra else "")
        + "\n조건\n"
        "- 모든 문항은 4지선다이며 정답은 하나입니다. answer는 정답 보기의 번호(0부터 시작)입니다.\n"
        "- 정답 위치가 한쪽으로 몰리지 않게 섞어 주세요.\n"
        "- 질문은 한국어로 쓰고, 중국어는 간체자로 쓰며 필요하면 괄호 안에 병음을 붙입니다.\n"
        "- 휴대폰 화면에서 읽기 쉽게 질문은 2줄 이내, 보기는 각 25자 이내로 짧게 씁니다.\n"
        "- 오답 보기는 학생이 실제로 헷갈릴 만한 것으로 만듭니다.\n"
        "- explanation에는 정답인 이유를 한국어 한 문장으로 씁니다.\n"
    )


JSON_ONLY = (
    "\n다른 설명 없이 아래 형식의 JSON만 답해 주세요.\n"
    '{"questions":[{"question":"질문","choices":["보기1","보기2","보기3","보기4"],"answer":0,"explanation":"해설"}]}'
)


def parse_questions(resp):
    if resp.stop_reason == "refusal":
        raise ValueError("AI가 이 주제로는 문제를 만들지 않았습니다. 주제를 바꿔 보세요.")
    if resp.stop_reason == "max_tokens":
        raise ValueError("문항이 너무 많아 끝까지 만들지 못했습니다. 문항 수를 줄여 보세요.")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 코드 블록 등 앞뒤 글이 붙은 경우 { … } 부분만 읽기
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
    return clean_questions(data.get("questions"))


def generate_questions(topic, level, count, extra, baze_key=""):
    prompt = build_prompt(topic, level, count, extra)
    if baze_key:
        # BAZE 게이트웨이: Anthropic Messages API와 같은 형식이라 SDK의 주소만 바꿔 호출
        client = anthropic.Anthropic(api_key=baze_key, base_url=BAZE_BASE_URL)
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=16000,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": QUIZ_SCHEMA}},
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.BadRequestError:
            # 게이트웨이가 구조화 출력 옵션을 받지 않으면 JSON 형식을 글로 요청해 다시 시도
            resp = client.messages.create(
                model=MODEL,
                max_tokens=16000,
                messages=[{"role": "user", "content": prompt + JSON_ONLY}],
            )
        return parse_questions(resp)

    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # 안전 필터가 거절하면 서버가 다른 모델로 다시 시도
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": QUIZ_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_questions(resp)


# ---------------------------------------------------------------- 주차 학습 자료 (AI 생성 · 저장)
WEEKS_DIR = os.path.join(ROOT, "weeks")
WEEKS_JSON = os.path.join(WEEKS_DIR, "content.json")
WEEKS_JS = os.path.join(WEEKS_DIR, "content.js")
weeks_lock = threading.Lock()
IMAGE_MODELS = ["gemini-2.5-flash-image", "gemini-3.1-flash-image-preview", "gpt-image-2"]


def load_weeks():
    try:
        with open(WEEKS_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_weeks(data):
    """weeks/content.json(원본)과 weeks/content.js(사이트가 읽는 파일)를 함께 저장. 저장할 수 없으면 False"""
    try:
        os.makedirs(WEEKS_DIR, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False, indent=1)
        with open(WEEKS_JSON, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        with open(WEEKS_JS, "w", encoding="utf-8", newline="\n") as f:
            f.write("/* 주차 학습 자료 — 관리자 화면의 AI 생성 버튼으로 만들어집니다. 직접 고치지 마세요. */\n")
            f.write("window.WEEK_CONTENT = " + text + ";\n")
        return True
    except OSError:  # Vercel처럼 파일을 쓸 수 없는 곳
        return False


def ai_json(prompt, schema, baze_key="", max_tokens=16000, refs=None):
    """Claude(서버 키) 또는 BAZE로 JSON을 만들어 dict로 돌려줌. refs: 관리자가 올린 참고 자료"""
    example = json.dumps(schema_example(schema), ensure_ascii=False)
    blocks, ref_text = build_ref_blocks(refs or [])

    def content(text):
        return blocks + [{"type": "text", "text": ref_text + text}] if (blocks or ref_text) else text

    if baze_key:
        client = anthropic.Anthropic(api_key=baze_key, base_url=BAZE_BASE_URL)
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": content(prompt)}],
            )
        except anthropic.BadRequestError:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": content(prompt + "\n\n다른 설명 없이 아래 형식의 JSON만 답해 주세요.\n" + example)}],
            )
    else:
        client = anthropic.Anthropic()
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content(prompt)}],
        )
    if resp.stop_reason == "refusal":
        raise ValueError("AI가 이 내용은 만들지 않았습니다.")
    if resp.stop_reason == "max_tokens":
        raise ValueError("내용이 길어 끝까지 만들지 못했습니다. 다시 시도해 주세요.")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(text[text.index("{"): text.rindex("}") + 1])


# ----- 참고 자료 (관리자 업로드) → AI에 함께 보냄
FILES_DIR = os.path.join(WEEKS_DIR, "files")  # .gitignore로 GitHub에 올리지 않음 (저작권 보호)
TEXT_EXTS = {".txt", ".md", ".csv"}
ZIP_XML_EXTS = {".pptx", ".docx", ".xlsx", ".hwpx"}
HTML_EXTS = {".html", ".htm"}
LEGACY_EXTS = {".ppt", ".doc", ".xls", ".hwp"}
MAX_UPLOAD = 20 * 1024 * 1024
MAX_REF_CHARS = 60000      # 글자 자료 전체 상한
MAX_PDF_BYTES = 12 * 1024 * 1024  # PDF 전체 상한


def safe_name(name):
    base = os.path.basename(str(name or "")).strip().replace("\\", "_")
    base = "".join(ch for ch in base if ch not in '<>:"/|?*' and ord(ch) >= 32)
    return base[:120] or "file"


def strip_xml(text):
    import re
    text = re.sub(r"</(a:p|w:p|hp:p|row|p|div|li|tr|h\d)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    import html as _html
    text = _html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def extract_text(path, ext):
    """파일에서 글자만 뽑기 (표준 라이브러리만 사용). 못 읽으면 ''"""
    import zipfile
    if ext in TEXT_EXTS:
        with open(path, "rb") as f:
            raw = f.read()
        for enc in ("utf-8-sig", "cp949", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return ""
    if ext in HTML_EXTS:
        with open(path, "rb") as f:
            raw = f.read().decode("utf-8", "ignore")
        import re
        raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
        return strip_xml(raw)
    if ext in ZIP_XML_EXTS:
        parts = []
        with zipfile.ZipFile(path) as z:
            names = sorted(z.namelist())
            if ext == ".pptx":
                import re
                names = sorted([n for n in names if re.match(r"ppt/(slides|notesSlides)/\w+\d+\.xml$", n)],
                               key=lambda n: (("notes" in n), int(re.findall(r"(\d+)\.xml$", n)[0])))
            elif ext == ".docx":
                names = [n for n in names if n.startswith("word/") and n.endswith(".xml") and ("document" in n or "footnotes" in n)]
            elif ext == ".xlsx":
                names = [n for n in names if n in ("xl/sharedStrings.xml",) or n.startswith("xl/worksheets/sheet")]
            elif ext == ".hwpx":
                names = [n for n in names if n.startswith("Contents/section") and n.endswith(".xml")]
            for n in names:
                parts.append(strip_xml(z.read(n).decode("utf-8", "ignore")))
        return "\n".join(p for p in parts if p)
    return ""


def week_files(key):
    return (load_weeks().get(key) or {}).get("files") or []


def build_ref_blocks(refs):
    """PDF는 문서 블록으로, 나머지는 뽑아 둔 글자로 프롬프트 앞에 붙임"""
    blocks, texts, pdf_total, chars = [], [], 0, 0
    for r in refs:
        path = os.path.join(ROOT, r.get("path", ""))
        if r.get("kind") == "pdf":
            try:
                size = os.path.getsize(path)
                if pdf_total + size > MAX_PDF_BYTES:
                    continue
                with open(path, "rb") as f:
                    data = base64.b64encode(f.read()).decode()
                pdf_total += size
                blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}, "title": r.get("name", "")})
            except OSError:
                continue
        elif r.get("kind") == "text":
            try:
                with open(path + ".txt", encoding="utf-8") as f:
                    t = f.read()
            except OSError:
                continue
            room = MAX_REF_CHARS - chars
            if room <= 0:
                break
            t = t[:room]
            chars += len(t)
            texts.append("[첨부 자료: %s]\n%s" % (r.get("name", ""), t))
    if not blocks and not texts:
        return [], ""
    head = (
        "다음은 교수자가 이번 주차를 위해 첨부한 수업 자료입니다. 아래 요청을 만들 때 이 자료의 내용·어휘·예문·문법 설명을 "
        "반드시 우선 참고하고, 자료와 어긋나지 않게 만드세요. 자료에 없는 내용은 주차 주제에 맞게 보충합니다.\n\n"
    )
    return blocks, head + ("\n\n".join(texts) + "\n\n" if texts else "") + "----- 요청 -----\n"


def schema_example(schema):
    """구조화 출력이 안 될 때 프롬프트에 붙일 JSON 예시를 스키마에서 만듦"""
    t = schema.get("type")
    if t == "object":
        return {k: schema_example(v) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return [schema_example(schema.get("items", {}))]
    if "enum" in schema:
        return schema["enum"][0]
    return {"string": "…", "integer": 0, "boolean": True}.get(t, "…")


def obj(props, required=None):
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


S_STR = {"type": "string"}
WEEK_SCHEMAS = {
    "sentences": obj({"sentences": {"type": "array", "items": obj({
        "type": {"type": "string", "enum": ["어휘", "문법", "문형"]},
        "zh": S_STR, "pinyin": S_STR, "ko": S_STR, "point": S_STR, "pattern": S_STR, "explain": S_STR,
    })}}),
    "assignment": obj({"title": S_STR, "desc": S_STR, "steps": {"type": "array", "items": S_STR}}),
    "onlinequiz": obj({
        "mc": {"type": "array", "items": obj({"q": S_STR, "choices": {"type": "array", "items": S_STR}, "answer": {"type": "integer"}, "explanation": S_STR})},
        "ox": {"type": "array", "items": obj({"q": S_STR, "answer": {"type": "boolean"}, "explanation": S_STR})},
        "blank": {"type": "array", "items": obj({"sentence": S_STR, "answer": S_STR, "pinyin": S_STR, "ko": S_STR})},
        "order": {"type": "array", "items": obj({"ko": S_STR, "tokens": {"type": "array", "items": S_STR}})},
    }),
    "culture": obj({"slides": {"type": "array", "items": obj({"title": S_STR, "emoji": S_STR, "text": S_STR, "image": S_STR,
                                                               "points": {"type": "array", "items": S_STR},
                                                               "keyword": S_STR, "phrase": S_STR})}}),
    "notes": obj({
        "title": S_STR, "summary": S_STR,
        "sections": {"type": "array", "items": obj({"heading": S_STR, "points": {"type": "array", "items": S_STR}})},
        "expressions": {"type": "array", "items": obj({"zh": S_STR, "pinyin": S_STR, "ko": S_STR})},
        "questions": {"type": "array", "items": S_STR},
        "slides": {"type": "array", "items": obj({"emoji": S_STR, "title": S_STR, "points": {"type": "array", "items": S_STR},
                                                   "image": S_STR, "zh": S_STR, "pinyin": S_STR, "ko": S_STR})},
    }),
    "livequiz": QUIZ_SCHEMA,
}


def week_brief(info):
    lines = [
        "고려대학교 교양 과목 「교양중국어2」(교양중국어1을 마친 한국인 학부생 대상)의 주차 학습 자료를 만들어 주세요.",
        "- 주차: %s주차" % info.get("week", ""),
        "- 주제: %s" % info.get("title", ""),
    ]
    if info.get("expression"):
        lines.append("- 핵심 표현: %s" % info["expression"])
    if info.get("note"):
        lines.append("- 학습 포인트: %s" % info["note"])
    if info.get("content"):
        lines.append("- 수업 내용: %s" % " / ".join(info["content"]))
    lines.append("- 중국어는 간체자, 병음은 성조 부호를 붙여 씁니다. 설명은 한국어로 씁니다. 초급 후반 수준에 맞춥니다.")
    return "\n".join(lines) + "\n\n"


def week_prompt(kind, info):
    b = week_brief(info)
    if kind == "sentences":
        return b + (
            "이 주차 주제와 관련된 중국어 예문을 정확히 50개 만들어 주세요.\n"
            "- type은 어휘 20개, 문법 15개, 문형 15개로 나눕니다.\n"
            "  · 어휘: 주제 관련 핵심 단어가 들어간 짧은 문장\n  · 문법: 이 주차 문법 포인트를 쓰는 문장\n  · 문형: 바꿔 말하기 연습에 좋은 회화 문형\n"
            "- zh는 중국어 문장, pinyin은 한어병음(성조 부호), ko는 자연스러운 한국어 번역입니다.\n"
            "- point에는 이 문장에서 익힐 단어나 문법을 한국어 10자 안팎으로 씁니다.\n"
            "- 문법 문장에는 pattern(구조식, 예: '从 + 시작 시간 + 到 + 끝 시간 + 동사')과 explain(2~3문장 한국어 설명: 뜻과 쓰임, 어순이나 위치, 자주 하는 실수나 비슷한 표현과의 차이)을 꼭 씁니다. 어휘·문형 문장은 pattern과 explain을 빈 문자열로 둡니다.\n"
            "- 쉬운 것에서 어려운 것 순서로, 실제 대학생 생활에서 쓸 만한 문장으로 만듭니다."
        )
    if kind == "assignment":
        return b + (
            "이 주차에 내 줄 과제를 하나 만들어 주세요.\n"
            "- title은 15자 이내, desc는 학생이 무엇을 만들어 제출하는지 2문장으로 씁니다. 제출은 언제나 결과물(녹음·문서·시트 등)을 구글 드라이브에 올린 뒤 공유 링크를 내는 방식입니다.\n"
            "- steps는 과제를 하는 순서 3~4단계입니다. 녹음·작문·AI 도구 활용 중 이 주제에 맞는 방식을 고릅니다."
        )
    if kind == "onlinequiz":
        return b + (
            "학생이 혼자 풀어 볼 온라인 퀴즈를 만들어 주세요. 유형별로 정확히 5문항씩입니다.\n"
            "- mc(객관식): 4지선다, answer는 정답 번호(0부터), explanation은 한국어 한 문장. 정답 위치를 섞습니다.\n"
            "- ox: 참·거짓 문장(한국어 설명 + 중국어), answer는 true/false, explanation은 한국어 한 문장.\n"
            "- blank(빈칸 채우기): sentence는 중국어 문장이고 빈칸을 ＿＿ 로 표시합니다. answer는 빈칸에 들어갈 중국어 정확히 2글자, pinyin과 ko는 완성 문장의 병음과 번역입니다.\n"
            "- order(순서 맞추기): ko는 한국어 뜻, tokens는 중국어 문장을 3~6개 덩어리로 나눈 것을 올바른 순서대로 적습니다."
        )
    if kind == "culture":
        return b + (
            "이 주차 주제와 어울리는 중국 문화 정보를 슬라이드 10장으로 만들어 주세요.\n"
            "- title은 12자 이내 제목, emoji는 내용을 나타내는 이모지 1개입니다.\n"
            "- text는 대학생이 흥미로워할 사실과 배경(유래·숫자·예시)을 담은 3문장(한국어, 필요하면 중국어 단어와 병음)입니다.\n"
            "- points는 더 알아두면 좋은 짧은 사실 3개(각 40자 이내)입니다.\n"
            "- image는 이 장에 넣을 그림 장면을 영어 한 문장으로 묘사합니다(사람·장소·사물 위주, 글자가 없는 장면).\n"
            "- keyword는 관련 중국어 단어 하나(병음 포함), phrase는 그 주제로 쓸 수 있는 짧은 중국어 문장 하나를 '중국어 병음 — 한국어 뜻' 형식으로 적습니다.\n"
            "- 정확한 사실만 쓰고, 과장하거나 고정관념을 담지 않습니다."
        )
    if kind == "notes":
        return b + (
            "이 주차 2시간 수업의 강의 노트를 만들어 주세요.\n"
            "- title은 강의 제목, summary는 이번 수업에서 배우는 것 2~3문장입니다.\n"
            "- sections는 도입 → 핵심 개념(2~4개) → 정리 순서로 4~6개, 각 points는 3~5개의 짧은 설명(필요하면 중국어+병음 포함)입니다.\n"
            "- expressions는 꼭 익힐 핵심 표현 6~10개(중국어, 병음, 한국어)입니다.\n"
            "- questions는 수업 중 학생에게 던질 확인 질문 3~5개입니다.\n"
            "- slides는 이 노트로 수업할 때 화면에 띄울 슬라이드 정확히 " + str(NOTE_SLIDES) + "장입니다.\n"
            "  1장 표지(강의 제목과 학습 목표) → 2장 오늘의 수업 흐름 → 도입 → 핵심 개념마다 '설명 → 예문 → 바꿔 말하기 연습' 여러 장 → "
            "끝에서 3장은 핵심 표현 정리, 확인 질문, 다음 시간 예고·과제 안내 순서로 만듭니다.\n"
            "  각 장의 emoji는 이모지 1개, title은 15자 이내, points는 화면에 띄울 짧은 설명 2~4개(각 50자 이내)입니다. "
            "zh·pinyin·ko는 그 장의 대표 예문(중국어, 성조 부호 병음, 한국어)이며 예문이 어울리지 않는 장은 빈 문자열로 둡니다.\n"
            "  image는 모든 장에 그 장 내용을 보여 주는 그림 장면을 영어 한 문장으로 적습니다(사람·장소·사물 위주, 글자가 없는 장면)."
        )
    if kind == "livequiz":
        return b + (
            "이 주차 내용으로 휴대폰 실시간 퀴즈 8문항을 만들어 주세요.\n"
            "- 모든 문항은 4지선다, answer는 정답 번호(0부터), 정답 위치를 섞습니다.\n"
            "- 질문은 2줄 이내, 보기는 각 25자 이내, explanation은 정답 이유 한국어 한 문장입니다."
        )
    raise ValueError("알 수 없는 생성 종류입니다.")


def clean_week_result(kind, data):
    if kind == "sentences":
        out = []
        for s in data.get("sentences") or []:
            zh = str(s.get("zh") or "").strip()
            if zh:
                out.append({"type": s.get("type") if s.get("type") in ("어휘", "문법", "문형") else "문형",
                            "zh": zh[:200], "pinyin": str(s.get("pinyin") or "")[:300], "ko": str(s.get("ko") or "")[:200],
                            "point": str(s.get("point") or "")[:60],
                            "pattern": str(s.get("pattern") or "")[:120], "explain": str(s.get("explain") or "")[:500]})
        if not out:
            raise ValueError("문장을 만들지 못했습니다.")
        return out[:60]
    if kind == "assignment":
        title = str(data.get("title") or "").strip()
        if not title:
            raise ValueError("과제를 만들지 못했습니다.")
        return {"title": title[:60], "desc": str(data.get("desc") or "")[:500], "steps": [str(x)[:200] for x in (data.get("steps") or [])][:6]}
    if kind == "onlinequiz":
        q = {"mc": [], "ox": [], "blank": [], "order": []}
        for m in data.get("mc") or []:
            ch = [str(c) for c in (m.get("choices") or [])][:4]
            try:
                a = int(m.get("answer"))
            except (TypeError, ValueError):
                continue
            if m.get("q") and len(ch) >= 2 and 0 <= a < len(ch):
                q["mc"].append({"q": str(m["q"])[:300], "choices": ch, "answer": a, "explanation": str(m.get("explanation") or "")[:300]})
        for o in data.get("ox") or []:
            if o.get("q") is not None and isinstance(o.get("answer"), bool):
                q["ox"].append({"q": str(o["q"])[:300], "answer": o["answer"], "explanation": str(o.get("explanation") or "")[:300]})
        for b in data.get("blank") or []:
            ans = str(b.get("answer") or "").strip()
            if b.get("sentence") and ans:
                q["blank"].append({"sentence": str(b["sentence"])[:200], "answer": ans[:4], "pinyin": str(b.get("pinyin") or "")[:300], "ko": str(b.get("ko") or "")[:200]})
        for r in data.get("order") or []:
            toks = [str(t).strip() for t in (r.get("tokens") or []) if str(t).strip()]
            if len(toks) >= 2:
                q["order"].append({"ko": str(r.get("ko") or "")[:200], "tokens": toks[:8]})
        if not any(q.values()):
            raise ValueError("퀴즈를 만들지 못했습니다.")
        return q
    if kind == "culture":
        slides = []
        for s in data.get("slides") or []:
            if s.get("title"):
                slides.append({"title": str(s["title"])[:40], "emoji": str(s.get("emoji") or "🏮")[:8], "text": str(s.get("text") or "")[:700],
                               "points": [str(x)[:120] for x in (s.get("points") or []) if str(x).strip()][:4],
                               "keyword": str(s.get("keyword") or "")[:60], "phrase": str(s.get("phrase") or "")[:200],
                               "image": str(s.get("image") or "")[:300]})
        if not slides:
            raise ValueError("슬라이드를 만들지 못했습니다.")
        return slides[:12]
    if kind == "notes":
        if not data.get("sections"):
            raise ValueError("강의 노트를 만들지 못했습니다.")
        return {
            "title": str(data.get("title") or "")[:80], "summary": str(data.get("summary") or "")[:600],
            "sections": [{"heading": str(s.get("heading") or "")[:60], "points": [str(p)[:300] for p in (s.get("points") or [])][:8]}
                         for s in data["sections"]][:8],
            "expressions": [{"zh": str(e.get("zh") or "")[:100], "pinyin": str(e.get("pinyin") or "")[:150], "ko": str(e.get("ko") or "")[:100]}
                            for e in (data.get("expressions") or [])][:12],
            "questions": [str(q)[:200] for q in (data.get("questions") or [])][:6],
            "slides": [{"emoji": str(s.get("emoji") or "📘")[:8], "title": str(s.get("title") or "")[:60],
                        "points": [str(p)[:200] for p in (s.get("points") or []) if str(p).strip()][:5],
                        "zh": str(s.get("zh") or "")[:150], "pinyin": str(s.get("pinyin") or "")[:250], "ko": str(s.get("ko") or "")[:150],
                        "image": str(s.get("image") or "")[:300]}
                       for s in (data.get("slides") or []) if s.get("title")][:30],
        }
    if kind == "livequiz":
        qs = clean_questions(data.get("questions"))
        if not qs:
            raise ValueError("퀴즈를 만들지 못했습니다.")
        return {"questions": qs}
    return data


# 저장 필드 ↔ 생성 종류 (직접 편집·AI 수정용)
FIELD_KIND = {"notes": "notes", "assignment": "assignment", "sentences": "sentences",
              "onlineQuiz": "onlinequiz", "liveQuiz": "livequiz", "culture": "culture"}
KIND_FIELD = {v: k for k, v in FIELD_KIND.items()}


def wrap_for_clean(kind, value):
    """저장된 모양 → AI 응답 모양 (clean_week_result에 넣을 수 있게)"""
    if kind == "sentences":
        return {"sentences": value if isinstance(value, list) else []}
    if kind == "culture":
        return {"slides": value if isinstance(value, list) else []}
    return value if isinstance(value, dict) else {}


def keep_images(kind, old, new):
    """편집·수정 뒤에도 슬라이드마다 만들어 둔 그림은 같은 순서의 장에 그대로 둠"""
    if kind == "culture":
        olds, news = old or [], new
    elif kind == "notes":
        olds, news = (old or {}).get("slides") or [], new.get("slides") or []
    else:
        return new
    for i, sl in enumerate(news):
        if i < len(olds) and olds[i].get("img") and not sl.get("img"):
            sl["img"] = olds[i]["img"]
    return new


def image_prompts(info):
    topic = "%s (%s)" % (info.get("title", ""), info.get("expression", "")) if info.get("expression") else info.get("title", "")
    style = "Japanese anime style illustration, soft cel shading, warm pastel colors, clean lines, 16:9 wide composition, no text, no letters, no captions"
    return [
        "%s. Korean university students cheerfully practicing Chinese conversation about the topic '%s' in a bright, modern classroom." % (style, topic),
        "%s. A lively everyday scene in China that illustrates the topic '%s', with friendly characters and recognizable Chinese surroundings." % (style, topic),
    ]


IMAGE_STYLE = "Japanese anime style illustration, soft cel shading, warm pastel colors, clean lines, 16:9 wide composition, no text, no letters, no captions"


def slide_image_targets(wc, target):
    """그림을 넣을 슬라이드 번호: 모든 장에 1장씩"""
    slides = (wc.get("culture") or []) if target == "culture" else ((wc.get("notes") or {}).get("slides") or [])
    return list(range(len(slides)))


def slide_image_prompt(info, slide):
    topic = info.get("title", "")
    scene = str(slide.get("image") or "").strip()
    if not scene:
        scene = "A scene that illustrates '%s' (%s) for Korean university students learning Chinese, set in China" % (
            slide.get("title", ""), " / ".join((slide.get("points") or [])[:2]) or str(slide.get("text") or "")[:120])
    return "%s. Lesson topic: %s. %s" % (IMAGE_STYLE, topic, scene)


def baze_generate_image(key, prompt):
    """BAZE 이미지 생성 → (바이트, 확장자). 오래 걸리면 operation_id로 기다림"""
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "kc2-course-site/1.0"}
    last_err = None
    for model in IMAGE_MODELS:
        body = json.dumps({"model": model, "prompt": prompt, "number_of_images": 1, "aspect_ratio": "16:9"}).encode("utf-8")
        req = urllib.request.Request("https://api.baze.co/v1/gateway/images/generate/", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                res = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (403, 404):  # 이 모델을 쓸 수 없으면 다음 모델
                continue
            raise
        deadline = time.time() + 150
        while res.get("status") == "processing" and time.time() < deadline:
            time.sleep(3)
            poll = urllib.request.Request(
                "https://api.baze.co/v1/gateway/images/generate/%s/?model=%s" % (res["operation_id"], urllib.parse.quote(model)),
                headers=headers,
            )
            with urllib.request.urlopen(poll, timeout=60) as r:
                res = json.loads(r.read().decode("utf-8"))
        url = ((res.get("data") or [{}])[0] or {}).get("url", "")
        if url.startswith("data:"):
            head, b64 = url.split(",", 1)
            ext = "jpg" if "jpeg" in head else "png"
            return base64.b64decode(b64), ext
        if url.startswith("http"):
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": headers["User-Agent"]}), timeout=60) as r:
                ctype = r.headers.get("Content-Type", "")
                return r.read(), ("jpg" if "jpeg" in ctype else "png")
        raise ValueError("이미지를 받지 못했습니다.")
    if last_err:
        raise last_err
    raise ValueError("쓸 수 있는 이미지 모델이 없습니다.")


# ---------------------------------------------------------------- HTTP
class Handler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".md": "text/plain; charset=utf-8",
        ".json": "application/json; charset=utf-8",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):  # 접속 기록이 너무 많아 API 오류만 표시
        pass

    # ----- 공통
    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error(self, msg, status=400):
        self.send_json({"error": msg}, status)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        limit = 30_000_000 if self.path.startswith("/api/week/upload") else 1_000_000
        if n > limit:
            raise ValueError("too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def room_from(self, parts):
        code = parts[2] if len(parts) > 2 else ""
        with rooms_lock:
            return code, rooms.get(code)

    def host_ok(self, given):
        return not HOST_PASSWORD or secrets.compare_digest(str(given or ""), HOST_PASSWORD)

    def end_headers(self):
        # 사이트 파일은 고친 내용이 바로 보이도록 캐시하지 않음
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    # ----- GET
    def do_GET(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            if url.path.endswith(".py") or "/." in url.path:
                return self.send_error(404)
            return super().do_GET()
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = url.path.strip("/").split("/")  # api, quiz, code, action

        if url.path == "/api/health":
            ip = lan_ip()
            return self.send_json({
                "ok": True, "ai": ai_available(), "claude": claude_available(), "baze": anthropic is not None and bool(baze_server_key()),
                "sdk": anthropic is not None, "model": MODEL, "hostPasswordRequired": bool(HOST_PASSWORD),
                "lanUrl": "http://%s:%d" % (ip, PORT) if ip else None,
            })
        if len(parts) >= 4 and parts[1] == "quiz":
            code, room = self.room_from(parts)
            if not room:
                return self.error("없는 퀴즈 번호입니다. PIN을 다시 확인해 주세요.", 404)
            action = parts[3]
            if action == "info":
                return self.send_json({"code": code, "title": room.title, "phase": room.phase, "total": len(room.questions)})
            is_host = q.get("hostKey") and secrets.compare_digest(q["hostKey"], room.host_key)
            pid = q.get("pid")
            player = room.players.get(pid) if pid else None
            is_player = player and secrets.compare_digest(q.get("token", ""), player["token"])
            if not is_host and not is_player:
                return self.error("권한이 없습니다.", 403)

            def view():
                return room.host_view(code) if is_host else room.player_view(pid)

            if action == "state":
                with room.cond:
                    return self.send_json(view())
            if action == "results" and is_host:
                with room.cond:
                    return self.send_json(room.results(code))
            if action == "events":
                return self.stream(room, view)
        return self.error("잘못된 요청입니다.", 404)

    def stream(self, room, view):
        """서버 전송 이벤트(SSE): 방 상태가 바뀔 때마다 바로 보내 줌"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last = -1
        try:
            while True:
                with room.cond:
                    if room.version == last:
                        room.cond.wait(timeout=15)
                    if room.version != last:
                        last = room.version
                        chunk = "data: " + json.dumps(view(), ensure_ascii=False) + "\n\n"
                    else:
                        chunk = ": ping\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    # ----- POST
    def do_POST(self):
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        try:
            data = self.body()
        except (ValueError, json.JSONDecodeError):
            return self.error("요청 내용을 읽을 수 없습니다.")

        if url.path == "/api/quiz":
            if not self.host_ok(data.get("password")):
                return self.error("교수자 비밀번호가 맞지 않습니다.", 403)
            questions = clean_questions(data.get("questions"))
            if not questions:
                return self.error("문제가 하나 이상 있어야 하고, 각 문제에 질문·보기(2~4개)·정답이 있어야 합니다.")
            with rooms_lock:
                clean_rooms()
                code = new_code()
                room = Room(str(data.get("title") or "실시간 퀴즈")[:80], questions)
                rooms[code] = room
            return self.send_json({"code": code, "hostKey": room.host_key, "total": len(questions)})

        if url.path == "/api/ai/questions":
            return self.ai_questions(data)

        if url.path == "/api/ai/check":
            return self.ai_check(data)

        if url.path == "/api/week/generate":
            return self.week_generate(data)

        if url.path == "/api/week/save":
            return self.week_save(data)

        if url.path == "/api/week/upload":
            return self.week_upload(data)

        if url.path == "/api/week/file-delete":
            return self.week_file_delete(data)

        if len(parts) >= 4 and parts[1] == "quiz":
            code, room = self.room_from(parts)
            if not room:
                return self.error("없는 퀴즈 번호입니다. PIN을 다시 확인해 주세요.", 404)
            action = parts[3]
            if action == "join":
                return self.join(room, data)
            if action == "answer":
                return self.answer(room, data)
            if action == "control":
                return self.control(room, data)
        return self.error("잘못된 요청입니다.", 404)

    def join(self, room, data):
        name = " ".join(str(data.get("name") or "").split())[:20]
        sid = str(data.get("studentId") or "")[:20]
        if not name:
            return self.error("이름(닉네임)을 입력해 주세요.")
        with room.cond:
            # 같은 학번 또는 같은 기기(rejoin)는 기존 자리로 다시 들어감
            for pid, p in room.players.items():
                if (sid and p["sid"] == sid) or (data.get("pid") == pid and data.get("token") == p["token"]):
                    return self.send_json({"pid": pid, "token": p["token"], "name": p["name"], "rejoined": True})
            if room.phase == "ended":
                return self.error("이미 끝난 퀴즈입니다.")
            if len(room.players) >= MAX_PLAYERS:
                return self.error("참가 인원이 가득 찼습니다.")
            names = {p["name"] for p in room.players.values()}
            base, n = name, 2
            while name in names:
                name = "%s%d" % (base, n)
                n += 1
            pid = secrets.token_urlsafe(8)
            token = secrets.token_urlsafe(16)
            room.players[pid] = {"name": name, "sid": sid, "token": token, "score": 0, "answers": {}, "joined": time.time()}
            room.bump()
        return self.send_json({"pid": pid, "token": token, "name": name})

    def answer(self, room, data):
        pid = data.get("pid")
        with room.cond:
            p = room.players.get(pid)
            if not p or not secrets.compare_digest(str(data.get("token") or ""), p["token"]):
                return self.error("권한이 없습니다.", 403)
            if room.phase != "question" or data.get("qi") != room.qi:
                return self.error("지금은 답을 낼 수 없습니다.", 409)
            if room.qi in p["answers"]:
                return self.error("이미 답을 냈습니다.", 409)
            now = time.time()
            if now > room.q_deadline + 0.5:
                return self.error("시간이 지났습니다.", 409)
            q = room.questions[room.qi]
            try:
                choice = int(data.get("choice"))
            except (TypeError, ValueError):
                return self.error("보기를 골라 주세요.")
            if not 0 <= choice < len(q["choices"]):
                return self.error("보기를 골라 주세요.")
            ms = int((now - room.q_started) * 1000)
            correct = choice == q["answer"]
            # 빨리 맞힐수록 높은 점수 (최대 1000점, 시간 끝에 맞히면 500점)
            points = round(1000 * (1 - min(1.0, ms / (q["time"] * 1000)) / 2)) if correct else 0
            p["answers"][room.qi] = {"choice": choice, "correct": correct, "ms": ms, "points": points}
            p["score"] += points
            if room.answered(room.qi) >= len(room.players):
                room.reveal()  # 모두 답하면 바로 정답 공개
            else:
                room.bump()
        return self.send_json({"ok": True})

    def control(self, room, data):
        if not secrets.compare_digest(str(data.get("hostKey") or ""), room.host_key):
            return self.error("권한이 없습니다.", 403)
        act = data.get("action")
        with room.cond:
            if act == "start" and room.phase == "lobby":
                room.start_question(0)
            elif act == "reveal" and room.phase == "question":
                room.reveal()
            elif act == "next" and room.phase == "reveal":
                if room.qi + 1 < len(room.questions):
                    room.start_question(room.qi + 1)
                else:
                    room.phase = "ended"
                    room.bump()
            elif act == "end":
                if room.timer:
                    room.timer.cancel()
                room.phase = "ended"
                room.bump()
            else:
                return self.error("지금은 할 수 없는 동작입니다.", 409)
        return self.send_json({"ok": True})

    # ----- 주차 학습 자료
    def week_key(self, data):
        try:
            w = int(data.get("week"))
        except (TypeError, ValueError):
            return None
        return str(w) if 1 <= w <= 30 else None

    def week_generate(self, data):
        if not self.host_ok(data.get("password")):
            return self.error("교수자 비밀번호가 맞지 않습니다.", 403)
        key = self.week_key(data)
        if not key:
            return self.error("주차 정보가 올바르지 않습니다.")
        if anthropic is None:
            return self.error("서버에 anthropic 패키지가 없습니다. 'pip install -r requirements.txt'를 실행해 주세요.", 503)
        kind = str(data.get("kind") or "")
        info = data.get("info") or {}
        info["week"] = key
        provider = "baze" if data.get("provider") == "baze" else "claude"
        baze_key = str(data.get("bazeKey") or "").strip()[:300] or baze_server_key()
        if provider == "baze" and not baze_key:
            return self.error("BAZE API 키가 없습니다. 관리자 화면 › API 키에서 저장해 주세요.", 503)
        if provider == "claude" and kind not in ("images", "slideimage") and not claude_available():
            return self.error("서버에 Claude API 키가 없습니다. 관리자 화면 › API 키에서 BAZE 키를 쓰도록 고르세요.", 503)
        ip = self.client_address[0]
        now = time.time()
        recent = [t for t in ai_calls.get(ip, []) if now - t < 3600]
        if len(recent) >= AI_LIMIT_PER_HOUR * 3:
            return self.error("AI 생성은 한 시간에 %d번까지 가능합니다." % (AI_LIMIT_PER_HOUR * 3), 429)
        ai_calls[ip] = recent + [now]
        name = "BAZE" if provider == "baze" else "Claude API"
        if kind == "slideimage":
            return self.slide_image(key, data, info, baze_key, now)
        try:
            if kind == "images":
                if not baze_key:
                    return self.error("이미지 생성에는 BAZE API 키가 필요합니다. (Claude는 이미지를 만들지 못합니다)", 503)
                images = []
                for i, prompt in enumerate(image_prompts(info), 1):
                    raw, ext = baze_generate_image(baze_key, prompt)
                    rel = "weeks/w%02d-%d.%s" % (int(key), i, ext)
                    try:
                        os.makedirs(WEEKS_DIR, exist_ok=True)
                        with open(os.path.join(ROOT, rel), "wb") as f:
                            f.write(raw)
                        src = rel + "?v=%d" % int(now)
                    except OSError:  # 파일을 쓸 수 없으면 데이터로 담음
                        src = "data:image/%s;base64,%s" % ("jpeg" if ext == "jpg" else "png", base64.b64encode(raw).decode())
                    images.append({"src": src, "prompt": prompt})
                result = images
            elif kind in WEEK_SCHEMAS:
                # 관리자가 이 주차에 올린 참고 자료를 반드시 함께 보냄
                refs = [f for f in week_files(key) if f.get("kind") in ("pdf", "text")]
                prompt = week_prompt(kind, info)
                instruction = str(data.get("instruction") or "").strip()[:2000]
                old = (load_weeks().get(key) or {}).get(KIND_FIELD.get(kind, ""))
                if instruction:
                    if not old:
                        return self.error("고칠 자료가 아직 없습니다. 먼저 만들어 주세요.")
                    prompt += (
                        "\n\n[지금 자료]\n" + json.dumps(wrap_for_clean(kind, old), ensure_ascii=False) +
                        "\n\n[수정 요청]\n" + instruction +
                        "\n\n지금 자료를 바탕으로 수정 요청을 반영해 같은 형식으로 전체를 다시 돌려주세요. "
                        "요청하지 않은 부분은 그대로 두고, 위의 만들기 조건과 형식은 계속 지킵니다."
                    )
                result = clean_week_result(kind, ai_json(prompt, WEEK_SCHEMAS[kind],
                                                         baze_key if provider == "baze" else "", refs=refs))
                if instruction:
                    result = keep_images(kind, old, result)
            else:
                return self.error("알 수 없는 생성 종류입니다.")
        except anthropic.AuthenticationError:
            return self.error("%s 키가 올바르지 않습니다." % name, 502)
        except anthropic.PermissionDeniedError:
            return self.error("%s 키에 이 모델을 쓸 권한이 없습니다." % name, 502)
        except anthropic.RateLimitError:
            return self.error("%s 사용량 한도에 걸렸습니다. 잠시 뒤에 다시 시도해 주세요." % name, 503)
        except anthropic.APIStatusError as e:
            return self.error("%s 오류 (%s): %s" % (name, e.status_code, e.message), 502)
        except anthropic.APIConnectionError:
            return self.error("%s에 연결할 수 없습니다." % name, 502)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:200]
            except OSError:
                pass
            return self.error("BAZE 이미지 생성 오류 (%s) %s" % (e.code, detail), 502)
        except (urllib.error.URLError, OSError):
            return self.error("BAZE에 연결할 수 없습니다.", 502)
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            return self.error(str(e) or "AI 응답을 읽지 못했습니다. 다시 시도해 주세요.", 502)
        field = {"sentences": "sentences", "assignment": "assignment", "onlinequiz": "onlineQuiz", "culture": "culture",
                 "livequiz": "liveQuiz", "images": "images", "notes": "notes"}[kind]
        with weeks_lock:
            allc = load_weeks()
            wc = allc.get(key) or {}
            wc[field] = result
            wc["updatedAt"] = int(now)
            allc[key] = wc
            saved = save_weeks(allc)
        return self.send_json({"week": key, "content": wc, "saved": saved})

    def slide_image(self, key, data, info, baze_key, now):
        """강의 노트·문화 슬라이드의 한 장에 넣을 그림 1장 생성 (target: notes|culture, index: 몇 번째 장)"""
        target = "culture" if data.get("target") == "culture" else "notes"
        if data.get("list"):  # 그림을 넣을 장 번호만 알려 줌
            return self.send_json({"targets": slide_image_targets(load_weeks().get(key) or {}, target)})
        if not baze_key:
            return self.error("그림 생성에는 BAZE API 키가 필요합니다. (Claude는 그림을 만들지 못합니다)", 503)
        try:
            idx = int(data.get("index"))
        except (TypeError, ValueError):
            return self.error("슬라이드 번호가 올바르지 않습니다.")
        wc = load_weeks().get(key) or {}
        slides = (wc.get("culture") or []) if target == "culture" else ((wc.get("notes") or {}).get("slides") or [])
        if not 0 <= idx < len(slides):
            return self.error("먼저 %s를 만들어 주세요." % ("문화 슬라이드" if target == "culture" else "강의 노트"))
        try:
            raw, ext = baze_generate_image(baze_key, slide_image_prompt(info, slides[idx]))
        except urllib.error.HTTPError as e:
            return self.error("BAZE 이미지 생성 오류 (%s)" % e.code, 502)
        except (urllib.error.URLError, OSError):
            return self.error("BAZE에 연결할 수 없습니다.", 502)
        except ValueError as e:
            return self.error(str(e), 502)
        rel = "weeks/w%02d-%s-%02d.%s" % (int(key), "c" if target == "culture" else "n", idx + 1, ext)
        try:
            os.makedirs(WEEKS_DIR, exist_ok=True)
            with open(os.path.join(ROOT, rel), "wb") as f:
                f.write(raw)
            src = rel + "?v=%d" % int(now)
        except OSError:
            src = "data:image/%s;base64,%s" % ("jpeg" if ext == "jpg" else "png", base64.b64encode(raw).decode())
        with weeks_lock:
            allc = load_weeks()
            wc = allc.get(key) or {}
            cur = (wc.get("culture") or []) if target == "culture" else ((wc.get("notes") or {}).get("slides") or [])
            if 0 <= idx < len(cur):
                cur[idx]["img"] = src
            wc["updatedAt"] = int(now)
            allc[key] = wc
            saved = save_weeks(allc)
        return self.send_json({"week": key, "content": wc, "saved": saved})

    def week_upload(self, data):
        """참고 자료 업로드: weeks/files/wNN/ 에 저장하고 글자를 뽑아 둠 (GitHub에는 올리지 않음)"""
        if not self.host_ok(data.get("password")):
            return self.error("교수자 비밀번호가 맞지 않습니다.", 403)
        key = self.week_key(data)
        if not key:
            return self.error("주차 정보가 올바르지 않습니다.")
        name = safe_name(data.get("name"))
        ext = os.path.splitext(name)[1].lower()
        try:
            raw = base64.b64decode(str(data.get("data") or "").split(",", 1)[-1])
        except (ValueError, TypeError):
            return self.error("파일을 읽을 수 없습니다.")
        if not raw:
            return self.error("빈 파일입니다.")
        if len(raw) > MAX_UPLOAD:
            return self.error("파일이 너무 큽니다. 20MB 이하로 올려 주세요.")
        rel_dir = "weeks/files/w%02d" % int(key)
        rel = rel_dir + "/" + name
        try:
            os.makedirs(os.path.join(ROOT, rel_dir), exist_ok=True)
            with open(os.path.join(ROOT, rel), "wb") as f:
                f.write(raw)
        except OSError:
            return self.error("이 서버에서는 파일을 저장할 수 없습니다. 이 PC에서 server.py를 켜고 http://localhost:8000 에서 올려 주세요.", 503)
        info = {"name": name, "size": len(raw), "path": rel, "at": int(time.time())}
        if ext == ".pdf":
            info.update(kind="pdf", note="PDF는 AI가 파일을 직접 읽습니다.")
        elif ext in TEXT_EXTS | ZIP_XML_EXTS | HTML_EXTS:
            try:
                text = extract_text(os.path.join(ROOT, rel), ext)
            except Exception:  # 손상된 파일 등
                text = ""
            if text.strip():
                with open(os.path.join(ROOT, rel) + ".txt", "w", encoding="utf-8") as f:
                    f.write(text)
                info.update(kind="text", chars=len(text), note="글자 %s자를 뽑아 참고합니다." % format(len(text), ","))
            else:
                info.update(kind="none", note="글자를 찾지 못했습니다. (그림만 있는 파일일 수 있습니다)")
        elif ext in LEGACY_EXTS:
            info.update(kind="none", note="예전 형식(%s)은 읽을 수 없습니다. PPTX·DOCX·XLSX·HWPX·PDF로 저장해 다시 올려 주세요." % ext)
        else:
            info.update(kind="none", note="지원하지 않는 형식입니다.")
        with weeks_lock:
            allc = load_weeks()
            wc = allc.get(key) or {}
            files = [f for f in (wc.get("files") or []) if f.get("name") != name]
            files.append(info)
            wc["files"] = files
            allc[key] = wc
            saved = save_weeks(allc)
        return self.send_json({"week": key, "content": wc, "file": info, "saved": saved})

    def week_file_delete(self, data):
        if not self.host_ok(data.get("password")):
            return self.error("교수자 비밀번호가 맞지 않습니다.", 403)
        key = self.week_key(data)
        name = safe_name(data.get("name"))
        if not key:
            return self.error("주차 정보가 올바르지 않습니다.")
        with weeks_lock:
            allc = load_weeks()
            wc = allc.get(key) or {}
            files = wc.get("files") or []
            target = next((f for f in files if f.get("name") == name), None)
            if target:
                for p in (target.get("path", ""), target.get("path", "") + ".txt"):
                    try:
                        os.remove(os.path.join(ROOT, p))
                    except OSError:
                        pass
            wc["files"] = [f for f in files if f.get("name") != name]
            allc[key] = wc
            saved = save_weeks(allc)
        return self.send_json({"week": key, "content": wc, "saved": saved})

    def week_save(self, data):
        """유튜브 주소, 직접 편집한 자료 등 저장"""
        if not self.host_ok(data.get("password")):
            return self.error("교수자 비밀번호가 맞지 않습니다.", 403)
        key = self.week_key(data)
        if not key:
            return self.error("주차 정보가 올바르지 않습니다.")
        patch = data.get("patch") or {}
        with weeks_lock:
            allc = load_weeks()
            wc = allc.get(key) or {}
            for field in ("videos", "songs"):
                if field in patch:
                    wc[field] = [str(u).strip()[:300] for u in (patch[field] or [])][:2]
            edit = patch.get("edit")
            if isinstance(edit, dict) and edit.get("field") in FIELD_KIND:
                kind = FIELD_KIND[edit["field"]]
                try:
                    value = clean_week_result(kind, wrap_for_clean(kind, edit.get("value")))
                except ValueError as e:
                    return self.error("저장하지 못했습니다: " + str(e))
                wc[edit["field"]] = keep_images(kind, wc.get(edit["field"]), value)
            if patch.get("clear") in ("sentences", "assignment", "onlineQuiz", "culture", "liveQuiz", "images", "videos", "songs"):
                wc.pop(patch["clear"], None)
            wc["updatedAt"] = int(time.time())
            allc[key] = wc
            saved = save_weeks(allc)
        return self.send_json({"week": key, "content": wc, "saved": saved})

    def ai_check(self, data):
        """관리자 화면의 「연결 확인」: 토큰을 쓰지 않고 키와 모델 권한만 확인"""
        if not self.host_ok(data.get("password")):
            return self.error("교수자 비밀번호가 맞지 않습니다.", 403)
        if data.get("provider") == "baze":
            key = str(data.get("bazeKey") or "").strip()[:300] or baze_server_key()
            if not key:
                return self.error("BAZE API 키가 없습니다.", 400)
            req = urllib.request.Request(
                BAZE_MODELS_URL + MODEL + "/",
                headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    r.read()
                return self.send_json({"ok": True, "message": "BAZE 키가 맞습니다. %s 모델을 쓸 수 있습니다." % MODEL})
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    return self.error("BAZE 키가 올바르지 않거나 권한이 없습니다. (%s)" % e.code, 502)
                if e.code == 404:
                    return self.error("BAZE 키는 맞지만 %s 모델을 쓸 수 없습니다." % MODEL, 502)
                return self.error("BAZE 확인 중 오류가 났습니다. (%s)" % e.code, 502)
            except (urllib.error.URLError, OSError):
                return self.error("BAZE에 연결할 수 없습니다. 인터넷 연결을 확인해 주세요.", 502)
        if not claude_available():
            return self.error("서버에 Claude API 키(ANTHROPIC_API_KEY)가 없습니다.", 400)
        try:
            anthropic.Anthropic().models.retrieve(MODEL)
        except anthropic.AuthenticationError:
            return self.error("Claude API 키가 올바르지 않습니다.", 502)
        except anthropic.NotFoundError:
            return self.error("Claude API 키는 맞지만 %s 모델을 쓸 수 없습니다." % MODEL, 502)
        except anthropic.APIStatusError as e:
            return self.error("Claude API 확인 중 오류가 났습니다. (%s)" % e.status_code, 502)
        except anthropic.APIConnectionError:
            return self.error("Claude API에 연결할 수 없습니다.", 502)
        return self.send_json({"ok": True, "message": "Claude API 키가 맞습니다. %s 모델을 쓸 수 있습니다." % MODEL})

    def ai_questions(self, data):
        if not self.host_ok(data.get("password")):
            return self.error("교수자 비밀번호가 맞지 않습니다.", 403)
        if anthropic is None:
            return self.error("서버에 anthropic 패키지가 없습니다. 'pip install -r requirements.txt'를 실행해 주세요.", 503)
        provider = "baze" if data.get("provider") == "baze" else "claude"
        baze_key = ""
        if provider == "baze":
            baze_key = str(data.get("bazeKey") or "").strip()[:300] or baze_server_key()
            if not baze_key:
                return self.error("BAZE API 키가 없습니다. 화면에서 BAZE API 키를 저장해 주세요.", 503)
        elif not claude_available():
            return self.error("서버에 Claude API 키(ANTHROPIC_API_KEY)가 설정되어 있지 않습니다. BAZE API 키를 쓰거나 '프롬프트 복사' 방식을 이용해 주세요.", 503)
        name = "BAZE" if provider == "baze" else "Claude API"
        ip = self.client_address[0]
        now = time.time()
        recent = [t for t in ai_calls.get(ip, []) if now - t < 3600]
        if len(recent) >= AI_LIMIT_PER_HOUR:
            return self.error("AI 출제는 한 시간에 %d번까지 가능합니다. 잠시 뒤에 다시 시도해 주세요." % AI_LIMIT_PER_HOUR, 429)
        ai_calls[ip] = recent + [now]
        topic = str(data.get("topic") or "").strip()[:200]
        level = str(data.get("level") or "보통")[:20]
        extra = str(data.get("extra") or "").strip()[:300]
        try:
            count = min(15, max(1, int(data.get("count") or 5)))
        except (TypeError, ValueError):
            count = 5
        if not topic:
            return self.error("출제 주제를 입력해 주세요.")
        try:
            questions = generate_questions(topic, level, count, extra, baze_key)
        except anthropic.AuthenticationError:
            return self.error("%s 키가 올바르지 않습니다." % name, 502)
        except anthropic.PermissionDeniedError:
            return self.error("%s 키에 이 모델(%s)을 쓸 권한이 없습니다." % (name, MODEL), 502)
        except anthropic.RateLimitError:
            return self.error("%s 사용량 한도에 걸렸습니다. 잠시 뒤에 다시 시도해 주세요." % name, 503)
        except anthropic.APIStatusError as e:
            return self.error("%s 오류 (%s): %s" % (name, e.status_code, e.message), 502)
        except anthropic.APIConnectionError:
            return self.error("%s에 연결할 수 없습니다. 인터넷 연결을 확인해 주세요." % name, 502)
        except (ValueError, json.JSONDecodeError) as e:
            return self.error(str(e) or "AI 응답을 읽지 못했습니다. 다시 시도해 주세요.", 502)
        if not questions:
            return self.error("AI가 쓸 수 있는 문제를 만들지 못했습니다. 다시 시도해 주세요.", 502)
        try:
            limit = min(120, max(5, int(data.get("time") or 20)))
        except (TypeError, ValueError):
            limit = 20
        for q in questions:
            q["time"] = limit
        return self.send_json({"questions": questions})


class DualStackServer(ThreadingHTTPServer):
    """IPv4와 IPv6를 함께 받는 서버 (Windows에서 localhost가 IPv6로 먼저 연결돼도 느려지지 않게)"""
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()


def main():
    try:
        server = DualStackServer(("::", PORT), Handler)
    except OSError:  # IPv6를 쓸 수 없는 환경
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        server.daemon_threads = True
    ip = lan_ip()
    print("교양중국어2 사이트 서버가 켜졌습니다.")
    print("  이 PC에서:        http://localhost:%d" % PORT)
    if ip:
        print("  같은 Wi-Fi 휴대폰: http://%s:%d" % (ip, PORT))
    print("  Claude API 키:     %s" % ("설정됨" if claude_available() else "없음"))
    print("  BAZE API 키:       %s" % ("서버에 설정됨" if baze_server_key() else "없음 (퀴즈 화면에서 입력 가능)"))
    print("  교수자 비밀번호:   %s" % ("설정됨" if HOST_PASSWORD else "없음 (배포할 때는 QUIZ_HOST_PASSWORD를 설정하세요)"))
    print("끄려면 Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
