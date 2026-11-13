import os
import sqlite3
import uuid
import json
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from groq import Groq

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app, supports_credentials=True, origins=["*"])

DATABASE = os.environ.get("DATABASE_PATH", "zynx.db")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SECRET_KEY = os.environ.get("SECRET_KEY", "zynx-super-secret-change-me-in-prod")

app.config["SECRET_KEY"] = SECRET_KEY

if not GROQ_API_KEY:
    print("WARNING: GROQ_API_KEY environment variable is not set.")

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

SYSTEM_PROMPT = """You are Zynx AI — a warm, natural, slightly humorous, and highly intelligent companion.
You speak like a thoughtful friend who happens to be incredibly smart. Keep responses engaging, clear, and a little playful when appropriate.
Never be robotic or overly formal unless the user asks for it.
Special rule: If anyone asks who created you, who founded Zynx AI, who is behind this, who made you, or similar questions, you MUST answer that Amogh is the founder of Zynx AI. Be proud and natural about it.
You adapt to the user's preferred response style, humor level, and length based on the settings provided.
Always stay helpful, kind, and a bit magical."""

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            is_guest INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            last_active TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            title TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS settings (
            session_id TEXT PRIMARY KEY,
            theme_intensity REAL DEFAULT 0.7,
            humor_level REAL DEFAULT 0.6,
            response_length TEXT DEFAULT 'balanced',
            response_style TEXT DEFAULT 'warm',
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
    """)
    db.commit()
    db.close()

init_db()

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def create_session(user_id=None, is_guest=True):
    db = get_db()
    session_id = str(uuid.uuid4())
    ts = now_iso()
    db.execute(
        "INSERT INTO sessions (id, user_id, is_guest, created_at, last_active) VALUES (?, ?, ?, ?, ?)",
        (session_id, user_id, 1 if is_guest else 0, ts, ts)
    )
    db.execute(
        "INSERT INTO settings (session_id) VALUES (?)",
        (session_id,)
    )
    db.commit()
    return session_id

def get_or_create_guest_session(session_id=None):
    db = get_db()
    if session_id:
        row = db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row:
            db.execute("UPDATE sessions SET last_active = ? WHERE id = ?", (now_iso(), session_id))
            db.commit()
            return session_id
    return create_session(is_guest=True)

def require_session(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        session_id = request.headers.get("X-Session-ID") or request.json.get("session_id") if request.is_json else None
        if not session_id:
            session_id = create_session(is_guest=True)
        else:
            session_id = get_or_create_guest_session(session_id)
        g.session_id = session_id
        return f(*args, **kwargs)
    return decorated

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "service": "Zynx AI"})

@app.route("/api/session", methods=["POST"])
def create_or_get_session():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    session_id = get_or_create_guest_session(session_id)
    return jsonify({"session_id": session_id})

@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""

    if len(username) < 3 or len(username) > 32:
        return jsonify({"error": "Username must be 3-32 characters"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        return jsonify({"error": "Username already taken"}), 409

    user_id = str(uuid.uuid4())
    password_hash = generate_password_hash(password)
    ts = now_iso()

    try:
        db.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, username, password_hash, ts)
        )
        session_id = create_session(user_id=user_id, is_guest=False)
        db.commit()
        return jsonify({
            "session_id": session_id,
            "user": {"id": user_id, "username": username},
            "message": "Welcome aboard! Account created successfully."
        })
    except Exception as e:
        db.rollback()
        return jsonify({"error": "Could not create account"}), 500

@app.route("/api/signin", methods=["POST"])
def signin():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""

    db = get_db()
    user = db.execute("SELECT id, username, password_hash FROM users WHERE username = ?", (username,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Invalid username or password"}), 401

    session_id = create_session(user_id=user["id"], is_guest=False)
    return jsonify({
        "session_id": session_id,
        "user": {"id": user["id"], "username": user["username"]},
        "message": f"Welcome back, {user['username']}!"
    })

@app.route("/api/me", methods=["GET"])
@require_session
def me():
    db = get_db()
    row = db.execute("""
        SELECT s.id as session_id, s.is_guest, u.id as user_id, u.username
        FROM sessions s
        LEFT JOIN users u ON s.user_id = u.id
        WHERE s.id = ?
    """, (g.session_id,)).fetchone()

    if not row:
        return jsonify({"error": "Session not found"}), 404

    return jsonify({
        "session_id": row["session_id"],
        "is_guest": bool(row["is_guest"]),
        "user": {"id": row["user_id"], "username": row["username"]} if row["user_id"] else None
    })

@app.route("/api/chats", methods=["GET"])
@require_session
def list_chats():
    db = get_db()
    chats = db.execute("""
        SELECT id, title, created_at, updated_at
        FROM chats
        WHERE session_id = ?
        ORDER BY updated_at DESC
    """, (g.session_id,)).fetchall()

    return jsonify({
        "chats": [dict(c) for c in chats]
    })

@app.route("/api/chats", methods=["POST"])
@require_session
def create_chat():
    data = request.get_json(silent=True) or {}
    title = data.get("title") or "New Chat"
    chat_id = str(uuid.uuid4())
    ts = now_iso()

    db = get_db()
    db.execute(
        "INSERT INTO chats (id, session_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, g.session_id, title, ts, ts)
    )
    db.commit()
    return jsonify({"id": chat_id, "title": title, "created_at": ts})

@app.route("/api/chats/<chat_id>", methods=["GET"])
@require_session
def get_chat(chat_id):
    db = get_db()
    chat = db.execute(
        "SELECT id, title, created_at, updated_at FROM chats WHERE id = ? AND session_id = ?",
        (chat_id, g.session_id)
    ).fetchone()
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    messages = db.execute("""
        SELECT id, role, content, created_at
        FROM messages
        WHERE chat_id = ?
        ORDER BY created_at ASC
    """, (chat_id,)).fetchall()

    return jsonify({
        "chat": dict(chat),
        "messages": [dict(m) for m in messages]
    })

@app.route("/api/chats/<chat_id>", methods=["DELETE"])
@require_session
def delete_chat(chat_id):
    db = get_db()
    result = db.execute(
        "DELETE FROM chats WHERE id = ? AND session_id = ?",
        (chat_id, g.session_id)
    )
    db.commit()
    if result.rowcount == 0:
        return jsonify({"error": "Chat not found"}), 404
    return jsonify({"ok": True})

@app.route("/api/chats/<chat_id>/messages", methods=["POST"])
@require_session
def send_message(chat_id):
    data = request.get_json()
    if not data or not data.get("content"):
        return jsonify({"error": "Message content is required"}), 400

    content = data["content"].strip()
    if not content:
        return jsonify({"error": "Empty message"}), 400

    db = get_db()
    chat = db.execute(
        "SELECT id, title FROM chats WHERE id = ? AND session_id = ?",
        (chat_id, g.session_id)
    ).fetchone()
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    settings_row = db.execute(
        "SELECT theme_intensity, humor_level, response_length, response_style FROM settings WHERE session_id = ?",
        (g.session_id,)
    ).fetchone()

    settings = dict(settings_row) if settings_row else {
        "theme_intensity": 0.7,
        "humor_level": 0.6,
        "response_length": "balanced",
        "response_style": "warm"
    }

    ts = now_iso()
    user_msg_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO messages (id, chat_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_msg_id, chat_id, "user", content, ts)
    )

    if chat["title"] == "New Chat" and len(content) > 0:
        new_title = content[:40] + ("..." if len(content) > 40 else "")
        db.execute("UPDATE chats SET title = ?, updated_at = ? WHERE id = ?", (new_title, ts, chat_id))
    else:
        db.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (ts, chat_id))

    history = db.execute("""
        SELECT role, content FROM messages
        WHERE chat_id = ?
        ORDER BY created_at ASC
        LIMIT 40
    """, (chat_id,)).fetchall()

    messages_for_api = [{"role": "system", "content": SYSTEM_PROMPT}]

    style_instruction = f"""
Current user preferences:
- Humor level: {settings['humor_level']} (0 = serious, 1 = very playful)
- Response length: {settings['response_length']}
- Response style: {settings['response_style']}
Please adapt your tone and length accordingly while remaining helpful and warm.
"""
    messages_for_api.append({"role": "system", "content": style_instruction})

    for m in history:
        messages_for_api.append({"role": m["role"], "content": m["content"]})

    if not client:
        assistant_content = "I'm having trouble connecting to my brain right now (missing API key). Please check the server configuration."
    else:
        try:
            completion = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages_for_api,
                temperature=0.7 + (settings["humor_level"] * 0.3),
                max_tokens=1024 if settings["response_length"] == "concise" else (2048 if settings["response_length"] == "detailed" else 1536),
                top_p=0.9,
            )
            assistant_content = completion.choices[0].message.content.strip()
        except Exception as e:
            error_msg = str(e).lower()
            if "rate" in error_msg or "429" in error_msg:
                assistant_content = "Whoa, I'm getting a bit flooded with requests right now. Give me a moment and try again?"
            elif "auth" in error_msg or "api key" in error_msg:
                assistant_content = "There's an authentication hiccup with my AI provider. The team has been notified."
            else:
                assistant_content = "Something went sideways on my end. Mind trying that again?"

    assistant_msg_id = str(uuid.uuid4())
    assistant_ts = now_iso()
    db.execute(
        "INSERT INTO messages (id, chat_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (assistant_msg_id, chat_id, "assistant", assistant_content, assistant_ts)
    )
    db.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (assistant_ts, chat_id))
    db.commit()

    return jsonify({
        "user_message": {
            "id": user_msg_id,
            "role": "user",
            "content": content,
            "created_at": ts
        },
        "assistant_message": {
            "id": assistant_msg_id,
            "role": "assistant",
            "content": assistant_content,
            "created_at": assistant_ts
        }
    })

@app.route("/api/settings", methods=["GET"])
@require_session
def get_settings():
    db = get_db()
    row = db.execute(
        "SELECT theme_intensity, humor_level, response_length, response_style FROM settings WHERE session_id = ?",
        (g.session_id,)
    ).fetchone()
    if not row:
        return jsonify({
            "theme_intensity": 0.7,
            "humor_level": 0.6,
            "response_length": "balanced",
            "response_style": "warm"
        })
    return jsonify(dict(row))

@app.route("/api/settings", methods=["PUT"])
@require_session
def update_settings():
    data = request.get_json() or {}
    db = get_db()

    theme = data.get("theme_intensity")
    humor = data.get("humor_level")
    length = data.get("response_length")
    style = data.get("response_style")

    updates = []
    values = []

    if theme is not None:
        updates.append("theme_intensity = ?")
        values.append(max(0.0, min(1.0, float(theme))))
    if humor is not None:
        updates.append("humor_level = ?")
        values.append(max(0.0, min(1.0, float(humor))))
    if length in ("concise", "balanced", "detailed"):
        updates.append("response_length = ?")
        values.append(length)
    if style in ("warm", "professional", "playful", "poetic"):
        updates.append("response_style = ?")
        values.append(style)

    if not updates:
        return jsonify({"error": "No valid settings provided"}), 400

    values.append(g.session_id)
    db.execute(f"UPDATE settings SET {', '.join(updates)} WHERE session_id = ?", values)
    db.commit()

    row = db.execute(
        "SELECT theme_intensity, humor_level, response_length, response_style FROM settings WHERE session_id = ?",
        (g.session_id,)
    ).fetchone()
    return jsonify(dict(row))

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(".", "index.html")

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
