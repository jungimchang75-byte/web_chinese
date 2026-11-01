"""교양중국어2 강의 사이트 서버 — 사이트 파일 + 실시간 퀴즈 + AI 문제 출제

실행:  python server.py            (기본 주소 http://localhost:8000)
환경변수
  PORT                  포트 번호 (Railway가 자동으로 넣어 줌)
  ANTHROPIC_API_KEY     AI 문제 출제용 Claude API 키 (없으면 '프롬프트 복사' 방식으로 안내)
  BAZE_API_KEY          (선택) 고려대학교 BAZE API Gateway 키. 화면에서 입력한 키가 있으면 그 키를 먼저 씀
  QUIZ_HOST_PASSWORD    설정하면 퀴즈 만들기·AI 출제에 이 비밀번호가 필요 (배포 시 꼭 설정)

퀴즈 방과 기록은 서버 메모리에만 있으므로, 끝나면 결과 CSV를 내려받아 두세요.
"""
import json
import os
import random
import secrets
import socket
import threading
import time
import urllib.error
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
        if n > 1_000_000:
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
