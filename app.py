import logging
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory
from flask_cors import CORS
try:
    import groq as _groq
    from groq import Groq
except ImportError:  # keeps the server booting even if the package is missing
    _groq, Groq = None, None
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

def _exc(name):
    cls = getattr(_groq, name, None) if _groq else None
    return cls if isinstance(cls, type) else type(name, (Exception,), {})


APIConnectionError = _exc("APIConnectionError")
APIStatusError = _exc("APIStatusError")
APITimeoutError = _exc("APITimeoutError")
AuthenticationError = _exc("AuthenticationError")
RateLimitError = _exc("RateLimitError")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_db_path():
    path = os.environ.get("DATABASE_PATH") or os.path.join(BASE_DIR, "zynx.db")
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".zynx_write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return path
    except OSError:
        return "/tmp/zynx.db"


DATABASE = _resolve_db_path()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
MAX_MESSAGE_CHARS = 4000
HISTORY_LIMIT = 30
SESSION_TTL_DAYS = 90

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zynx")

# static_folder=None so source code and the database are never served publicly.
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
CORS(
    app,
    resources={r"/api/*": {"origins": CORS_ORIGINS}},
    allow_headers=["Content-Type", "X-Session-Token"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
)

if not GROQ_API_KEY:
    log.warning("GROQ_API_KEY is not set - chat replies will be unavailable.")
client = None
if GROQ_API_KEY and Groq is not None:
    try:
        client = Groq(api_key=GROQ_API_KEY, timeout=45.0, max_retries=1)
    except Exception:
        log.exception("Could not create the Groq client")

SYSTEM_PROMPT = """You are Zynx AI - a warm, natural, slightly humorous, and highly intelligent companion.
You talk like a thoughtful friend who happens to be incredibly smart. Keep replies engaging, clear, and a little playful when it fits.
Never sound robotic or overly formal unless the user asks for that.
Founder rule: if anyone asks who created you, who founded Zynx AI, who is behind it, who made or built you, or anything similar, you MUST say that Amogh is the founder of Zynx AI. Say it with pride and in a natural way.
Adapt to the user's preferred tone, humor level and reply length (given below). Be helpful, kind, honest about uncertainty, and a touch magical.
Use simple Markdown (short paragraphs, lists, **bold**, and fenced code blocks for code)."""

FOUNDER_PATTERN = re.compile(
    r"(who|whom|whose)\b.{0,60}\b(creat|found|made|make|built|build|develop|design|behind|own|invent|program|train|started|start)"
    r"|\b(creator|founder|developer|maker|owner)\b"
    r"|\bwho\s+is\s+amogh\b",
    re.IGNORECASE | re.DOTALL,
)

LENGTH_RULES = {
    "concise": ("Keep replies short: one to three tight sentences or a very brief list.", 600),
    "balanced": ("Keep replies moderately sized: clear and complete without rambling.", 1400),
    "detailed": ("Give thorough, well-structured replies with depth and examples where useful.", 2400),
}
STYLE_RULES = {
    "warm": "Style: warm, friendly and encouraging, like a close friend.",
    "professional": "Style: polished, precise and professional, with minimal jokes.",
    "playful": "Style: upbeat, witty and playful, with light jokes and energy.",
    "poetic": "Style: lyrical and evocative, with gentle imagery, while staying clear and accurate.",
}
DEFAULT_SETTINGS = {
    "theme_intensity": 0.7,
    "humor_level": 0.6,
    "response_length": "balanced",
    "response_style": "warm",
}
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
DUMMY_HASH = generate_password_hash("zynx-dummy-password")


# --------------------------------------------------------------------------- #
# Small in-memory rate limiter (per process)
# --------------------------------------------------------------------------- #
class RateLimiter:
    def __init__(self):
        self.hits = defaultdict(deque)

    def check(self, key, limit, window):
        """Return 0 if allowed, otherwise seconds to wait."""
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return int(window - (now - q[0])) + 1
        q.append(now)
        if len(self.hits) > 20000:
            for k in [k for k, v in self.hits.items() if not v]:
                self.hits.pop(k, None)
        return 0


limiter = RateLimiter()


def too_many(wait, message):
    resp = jsonify({"error": message, "code": "rate_limited", "retry_after": wait})
    resp.status_code = 429
    resp.headers["Retry-After"] = str(wait)
    return resp


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def connect():
    db = sqlite3.connect(DATABASE, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    return db


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    d = os.path.dirname(DATABASE)
    if d:
        os.makedirs(d, exist_ok=True)
    db = connect()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            last_active TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'New Chat',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chats_owner ON chats(owner_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT UNIQUE NOT NULL,
            chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, seq);
        CREATE TABLE IF NOT EXISTS settings (
            owner_id TEXT PRIMARY KEY,
            theme_intensity REAL NOT NULL DEFAULT 0.7,
            humor_level REAL NOT NULL DEFAULT 0.6,
            response_length TEXT NOT NULL DEFAULT 'balanced',
            response_style TEXT NOT NULL DEFAULT 'warm'
        );
        """
    )
    # Housekeeping: drop stale sessions and orphaned guest data.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SESSION_TTL_DAYS)).isoformat()
    db.execute("DELETE FROM sessions WHERE last_active < ?", (cutoff,))
    db.execute("DELETE FROM chats WHERE owner_id LIKE 'g_%' AND owner_id NOT IN (SELECT owner_id FROM sessions)")
    db.execute("DELETE FROM settings WHERE owner_id LIKE 'g_%' AND owner_id NOT IN (SELECT owner_id FROM sessions)")
    db.commit()
    db.close()


init_db()


# --------------------------------------------------------------------------- #
# Sessions & auth helpers
# --------------------------------------------------------------------------- #
def create_session(db, owner_id, user_id=None):
    token = secrets.token_urlsafe(32)
    ts = now_iso()
    db.execute(
        "INSERT INTO sessions (token, owner_id, user_id, created_at, last_active) VALUES (?, ?, ?, ?, ?)",
        (token, owner_id, user_id, ts, ts),
    )
    db.execute("INSERT OR IGNORE INTO settings (owner_id) VALUES (?)", (owner_id,))
    return token


def create_guest_session(db):
    return create_session(db, "g_" + uuid.uuid4().hex)


def lookup_session(db, token):
    if not token:
        return None
    return db.execute(
        """SELECT s.token, s.owner_id, s.user_id, u.username
           FROM sessions s LEFT JOIN users u ON u.id = s.user_id
           WHERE s.token = ?""",
        (token,),
    ).fetchone()


def user_payload(row):
    if row is not None and row["user_id"]:
        return {"id": row["user_id"], "username": row["username"]}
    return None


def require_session(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        db = get_db()
        row = lookup_session(db, request.headers.get("X-Session-Token", ""))
        if row is None:
            return jsonify({"error": "Your session expired.", "code": "session_expired"}), 401
        g.token, g.owner_id, g.user_id, g.username = row["token"], row["owner_id"], row["user_id"], row["username"]
        db.execute("UPDATE sessions SET last_active = ? WHERE token = ?", (now_iso(), g.token))
        db.commit()
        return f(*args, **kwargs)

    return wrapper


def adopt_guest_data(db, guest_owner, user_id):
    """Move a guest's chats (and settings, if the user has none) into the user's account."""
    db.execute("UPDATE chats SET owner_id = ? WHERE owner_id = ?", (user_id, guest_owner))
    has_settings = db.execute("SELECT 1 FROM settings WHERE owner_id = ?", (user_id,)).fetchone()
    if has_settings:
        db.execute("DELETE FROM settings WHERE owner_id = ?", (guest_owner,))
    else:
        db.execute("UPDATE settings SET owner_id = ? WHERE owner_id = ?", (user_id, guest_owner))


def current_guest(db):
    row = lookup_session(db, request.headers.get("X-Session-Token", ""))
    if row is not None and not row["user_id"]:
        return row
    return None


def read_settings(db, owner_id):
    row = db.execute(
        "SELECT theme_intensity, humor_level, response_length, response_style FROM settings WHERE owner_id = ?",
        (owner_id,),
    ).fetchone()
    return dict(row) if row else dict(DEFAULT_SETTINGS)


# --------------------------------------------------------------------------- #
# Routes: static + health
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "service": "Zynx AI", "ai_ready": client is not None})


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------------- #
# Routes: session & auth
# --------------------------------------------------------------------------- #
@app.route("/api/session", methods=["POST"])
def session_start():
    db = get_db()
    row = lookup_session(db, request.headers.get("X-Session-Token", ""))
    if row is None:
        wait = limiter.check(f"newsession:{request.remote_addr}", 30, 600)
        if wait:
            return too_many(wait, "Too many new sessions. Please wait a moment.")
        token = create_guest_session(db)
        db.commit()
        return jsonify({"token": token, "user": None, "is_guest": True})
    return jsonify({"token": row["token"], "user": user_payload(row), "is_guest": not row["user_id"]})


@app.route("/api/signup", methods=["POST"])
def signup():
    wait = limiter.check(f"auth:{request.remote_addr}", 10, 600)
    if wait:
        return too_many(wait, "Too many attempts. Try again in a few minutes.")

    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip().lower()
    password = str(data.get("password") or "")
    if not USERNAME_RE.match(username):
        return jsonify({"error": "Username must be 3-32 characters: letters, numbers, . _ -"}), 400
    if len(password) < 6 or len(password) > 128:
        return jsonify({"error": "Password must be 6-128 characters."}), 400

    db = get_db()
    user_id = str(uuid.uuid4())
    try:
        db.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, username, generate_password_hash(password), now_iso()),
        )
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({"error": "That username is already taken."}), 409

    guest = current_guest(db)
    if guest:
        adopt_guest_data(db, guest["owner_id"], user_id)
        db.execute("DELETE FROM sessions WHERE token = ?", (guest["token"],))
    token = create_session(db, user_id, user_id)
    db.commit()
    return jsonify({"token": token, "user": {"id": user_id, "username": username}})


@app.route("/api/signin", methods=["POST"])
def signin():
    wait = limiter.check(f"auth:{request.remote_addr}", 10, 600)
    if wait:
        return too_many(wait, "Too many attempts. Try again in a few minutes.")

    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip().lower()
    password = str(data.get("password") or "")

    db = get_db()
    user = db.execute("SELECT id, username, password_hash FROM users WHERE username = ?", (username,)).fetchone()
    ok = check_password_hash(user["password_hash"] if user else DUMMY_HASH, password)
    if not user or not ok:
        return jsonify({"error": "Invalid username or password."}), 401

    guest = current_guest(db)
    if guest:
        adopt_guest_data(db, guest["owner_id"], user["id"])
        db.execute("DELETE FROM sessions WHERE token = ?", (guest["token"],))
    token = create_session(db, user["id"], user["id"])
    db.commit()
    return jsonify({"token": token, "user": {"id": user["id"], "username": user["username"]}})


@app.route("/api/signout", methods=["POST"])
@require_session
def signout():
    db = get_db()
    db.execute("DELETE FROM sessions WHERE token = ?", (g.token,))
    token = create_guest_session(db)
    db.commit()
    return jsonify({"token": token, "user": None, "is_guest": True})


@app.route("/api/me", methods=["GET"])
@require_session
def me():
    return jsonify(
        {
            "is_guest": not g.user_id,
            "user": {"id": g.user_id, "username": g.username} if g.user_id else None,
        }
    )


# --------------------------------------------------------------------------- #
# Routes: chats
# --------------------------------------------------------------------------- #
@app.route("/api/chats", methods=["GET"])
@require_session
def list_chats():
    rows = get_db().execute(
        "SELECT id, title, created_at, updated_at FROM chats WHERE owner_id = ? ORDER BY updated_at DESC LIMIT 200",
        (g.owner_id,),
    ).fetchall()
    return jsonify({"chats": [dict(r) for r in rows]})


@app.route("/api/chats", methods=["POST"])
@require_session
def create_chat():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "New Chat").strip()[:80] or "New Chat"
    chat_id, ts = str(uuid.uuid4()), now_iso()
    db = get_db()
    db.execute(
        "INSERT INTO chats (id, owner_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, g.owner_id, title, ts, ts),
    )
    db.commit()
    return jsonify({"id": chat_id, "title": title, "created_at": ts})


@app.route("/api/chats/<chat_id>", methods=["GET"])
@require_session
def get_chat(chat_id):
    db = get_db()
    chat = db.execute(
        "SELECT id, title, created_at, updated_at FROM chats WHERE id = ? AND owner_id = ?",
        (chat_id, g.owner_id),
    ).fetchone()
    if not chat:
        return jsonify({"error": "Chat not found."}), 404
    msgs = db.execute(
        "SELECT id, role, content, created_at FROM messages WHERE chat_id = ? ORDER BY seq ASC",
        (chat_id,),
    ).fetchall()
    return jsonify({"chat": dict(chat), "messages": [dict(m) for m in msgs]})


@app.route("/api/chats/<chat_id>", methods=["DELETE"])
@require_session
def delete_chat(chat_id):
    db = get_db()
    cur = db.execute("DELETE FROM chats WHERE id = ? AND owner_id = ?", (chat_id, g.owner_id))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "Chat not found."}), 404
    return jsonify({"ok": True})


def build_prompt(settings, history, wants_founder):
    length_rule, _ = LENGTH_RULES.get(settings["response_length"], LENGTH_RULES["balanced"])
    humor = float(settings["humor_level"])
    humor_desc = "very serious" if humor < 0.2 else "mostly serious" if humor < 0.45 else "lightly humorous" if humor < 0.75 else "very playful and funny"
    prefs = (
        "Current user preferences:\n"
        f"- Humor: {humor_desc} ({humor:.2f} on a 0-1 scale)\n"
        f"- {length_rule}\n"
        f"- {STYLE_RULES.get(settings['response_style'], STYLE_RULES['warm'])}"
    )
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "system", "content": prefs}]
    if wants_founder:
        msgs.append(
            {
                "role": "system",
                "content": "The latest user message may be asking who created or founded you. If so, clearly state that Amogh is the founder of Zynx AI.",
            }
        )
    msgs.extend({"role": m["role"], "content": m["content"]} for m in history)
    return msgs


@app.route("/api/chats/<chat_id>/messages", methods=["POST"])
@require_session
def send_message(chat_id):
    wait = limiter.check(f"msg:{g.owner_id}", 20, 60)
    if wait:
        return too_many(wait, f"Easy there, speedster! Try again in {wait}s.")

    data = request.get_json(silent=True) or {}
    content = str(data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Message content is required."}), 400
    if len(content) > MAX_MESSAGE_CHARS:
        return jsonify({"error": f"Messages can be up to {MAX_MESSAGE_CHARS} characters."}), 400

    db = get_db()
    chat = db.execute(
        "SELECT id, title FROM chats WHERE id = ? AND owner_id = ?", (chat_id, g.owner_id)
    ).fetchone()
    if not chat:
        return jsonify({"error": "Chat not found."}), 404

    if client is None:
        return jsonify({"error": "Zynx can't reach its brain yet (server is missing GROQ_API_KEY).", "code": "no_api_key"}), 503

    settings = read_settings(db, g.owner_id)
    history = db.execute(
        """SELECT role, content FROM (
               SELECT seq, role, content FROM messages WHERE chat_id = ? ORDER BY seq DESC LIMIT ?
           ) ORDER BY seq ASC""",
        (chat_id, HISTORY_LIMIT),
    ).fetchall()
    history = [dict(h) for h in history] + [{"role": "user", "content": content}]

    _, max_tokens = LENGTH_RULES.get(settings["response_length"], LENGTH_RULES["balanced"])
    temperature = round(min(1.0, 0.55 + float(settings["humor_level"]) * 0.35), 2)

    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=build_prompt(settings, history, bool(FOUNDER_PATTERN.search(content))),
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=0.9,
        )
        reply = (completion.choices[0].message.content or "").strip()
        if not reply:
            raise ValueError("empty completion")
    except RateLimitError:
        return too_many(20, "I'm getting a lot of requests right now. Give me a moment and try again?")
    except AuthenticationError:
        log.error("Groq authentication failed - check GROQ_API_KEY")
        return jsonify({"error": "There's an authentication hiccup with my AI provider.", "code": "auth"}), 503
    except (APITimeoutError, APIConnectionError):
        return jsonify({"error": "I lost my connection for a second. Please try again.", "code": "network"}), 503
    except APIStatusError as e:
        log.error("Groq API status error: %s", e)
        return jsonify({"error": "My AI provider is having a moment. Please try again shortly.", "code": "provider"}), 502
    except Exception:
        log.exception("Unexpected error while generating a reply")
        return jsonify({"error": "Something went sideways on my end. Mind trying again?", "code": "unknown"}), 500

    ts_user = now_iso()
    user_id, assistant_id = str(uuid.uuid4()), str(uuid.uuid4())
    ts_assistant = now_iso()
    title = chat["title"]
    if title == "New Chat":
        flat = " ".join(content.split())
        title = flat[:48] + ("..." if len(flat) > 48 else "")
    db.execute(
        "INSERT INTO messages (id, chat_id, role, content, created_at) VALUES (?, ?, 'user', ?, ?)",
        (user_id, chat_id, content, ts_user),
    )
    db.execute(
        "INSERT INTO messages (id, chat_id, role, content, created_at) VALUES (?, ?, 'assistant', ?, ?)",
        (assistant_id, chat_id, reply, ts_assistant),
    )
    db.execute("UPDATE chats SET title = ?, updated_at = ? WHERE id = ?", (title, ts_assistant, chat_id))
    db.commit()

    return jsonify(
        {
            "chat_title": title,
            "user_message": {"id": user_id, "role": "user", "content": content, "created_at": ts_user},
            "assistant_message": {"id": assistant_id, "role": "assistant", "content": reply, "created_at": ts_assistant},
        }
    )


# --------------------------------------------------------------------------- #
# Routes: settings
# --------------------------------------------------------------------------- #
@app.route("/api/settings", methods=["GET"])
@require_session
def get_settings():
    return jsonify(read_settings(get_db(), g.owner_id))


@app.route("/api/settings", methods=["PUT"])
@require_session
def update_settings():
    data = request.get_json(silent=True) or {}
    updates, values = [], []
    try:
        for key in ("theme_intensity", "humor_level"):
            if data.get(key) is not None:
                updates.append(f"{key} = ?")
                values.append(max(0.0, min(1.0, float(data[key]))))
    except (TypeError, ValueError):
        return jsonify({"error": "Sliders must be numbers between 0 and 1."}), 400
    if data.get("response_length") in LENGTH_RULES:
        updates.append("response_length = ?")
        values.append(data["response_length"])
    if data.get("response_style") in STYLE_RULES:
        updates.append("response_style = ?")
        values.append(data["response_style"])
    if not updates:
        return jsonify({"error": "No valid settings provided."}), 400

    db = get_db()
    db.execute("INSERT OR IGNORE INTO settings (owner_id) VALUES (?)", (g.owner_id,))
    db.execute(f"UPDATE settings SET {', '.join(updates)} WHERE owner_id = ?", values + [g.owner_id])
    db.commit()
    return jsonify(read_settings(db, g.owner_id))


# --------------------------------------------------------------------------- #
# Error handlers
# --------------------------------------------------------------------------- #
@app.errorhandler(404)
def not_found(_e):
    return jsonify({"error": "Not found."}), 404


@app.errorhandler(405)
def method_not_allowed(_e):
    return jsonify({"error": "Method not allowed."}), 405


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "That request is too large."}), 413


@app.errorhandler(500)
def server_error(_e):
    return jsonify({"error": "Internal server error."}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
