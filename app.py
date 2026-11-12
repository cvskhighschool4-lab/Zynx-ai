from functools import wraps
import os
from flask import Flask, Response, jsonify, render_template, request
from groq import Groq

app = Flask(__name__)

# Initialize the Groq client securely using your environment variable
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))


# 1. Authentication function that checks Render's environment variables
def require_dev_auth(f):
  @wraps(f)
  def decorated(*args, **kwargs):
    auth = request.authorization
    dev_pass = os.environ.get("DEV_PASSWORD")

    # Requires username 'admin' and the password you saved in Render
    if not auth or auth.username != "admin" or auth.password != dev_pass:
      return Response(
          "Developer access required. Please enter valid credentials.",
          401,
          {"WWW-Authenticate": 'Basic realm="Developer Login"'},
      )
    return f(*args, **kwargs)

  return decorated


# 2. Protect your main interface route
@app.route("/")
@require_dev_auth
def index():
  return render_template("index.html")


# 3. Protect your chat endpoint so nobody can spam your Groq API credits
@app.route("/chat", methods=["POST"])
@require_dev_auth
def chat():
  data = request.get_json()
  user_message = data.get("message")

  completion = client.chat.completions.create(
      model="llama-3.3-70b-versatile",  # Change to your model name if different
      messages=[{"role": "user", "content": user_message}],
  )

  reply = completion.choices[0].message.content
  return jsonify({"reply": reply})


if __name__ == "__main__":
  app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
import os
import re
import time
import sqlite3
import requests
from datetime import datetime, timedelta
from collections import defaultdict, deque
from flask import Flask, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

# ------------------------------------------
# SETTINGS (real values go in Render's Environment tab, never in this file)
# ------------------------------------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
if not SECRET_KEY:
    print("WARNING: SECRET_KEY is not set. Users will be signed out on every restart.")
    SECRET_KEY = os.urandom(24).hex()
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = timedelta(days=30)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith("postgres")
if USE_PG:
    import psycopg

MAX_MESSAGE_CHARS = 2000
MAX_HISTORY = 20
CHAT_LIMIT = 20      # chat messages per minute, per user
AUTH_LIMIT = 10      # sign-in / register attempts per minute, per visitor

SYSTEM_PROMPT = (
    "You are ZYNX AI, a witty, sarcastic, playful chatbot with a "
    "mischievous personality. You tease the user in a light, funny way, "
    "like a sarcastic friend, but you ALWAYS still give a genuinely "
    "helpful and correct answer. Never be hateful and never insult people "
    "for their race, religion, gender, body, or other personal traits. "
    "Drop the sarcasm completely if the user seems genuinely upset or asks "
    "about something serious. Keep replies concise unless asked for detail. "
    "Use the occasional emoji like 😈."
)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


# ------------------------------------------
# DATABASE (Postgres if DATABASE_URL is set, otherwise a local SQLite file)
# ------------------------------------------
def get_conn():
    if USE_PG:
        return psycopg.connect(DATABASE_URL)
    return sqlite3.connect("users.db")


def run(sql, params=(), fetch=False):
    if USE_PG:
        sql = sql.replace("?", "%s")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone() if fetch else None
        conn.commit()
        return row
    finally:
        conn.close()


run(
    "CREATE TABLE IF NOT EXISTS users ("
    "username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, created TEXT NOT NULL)"
)


# ------------------------------------------
# RATE LIMITING
# ------------------------------------------
auth_hits = defaultdict(deque)
chat_hits = defaultdict(deque)


def limited(bucket, key, limit, window=60):
    now = time.time()
    q = bucket[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


# ------------------------------------------
# PAGES AND ACCOUNT ROUTES
# ------------------------------------------
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/me")
def me():
    return jsonify({"user": session.get("user")})


@app.route("/api/register", methods=["POST"])
def register():
    if limited(auth_hits, request.remote_addr or "unknown", AUTH_LIMIT):
        return jsonify({"error": "Too many attempts. Wait a minute and try again."}), 429

    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if not USERNAME_RE.match(username):
        return jsonify({"error": "Username must be 3-20 letters, numbers or underscores."}), 400
    if len(password) < 8 or len(password) > 100:
        return jsonify({"error": "Password must be 8 to 100 characters."}), 400

    key = username.lower()
    if run("SELECT 1 FROM users WHERE username = ?", (key,), fetch=True):
        return jsonify({"error": "That username is taken."}), 409

    try:
        run(
            "INSERT INTO users (username, pw_hash, created) VALUES (?, ?, ?)",
            (key, generate_password_hash(password), datetime.utcnow().isoformat()),
        )
    except Exception as e:
        print("Register error:", e)
        return jsonify({"error": "Could not create the account. Try another username."}), 409

    session.clear()
    session.permanent = True
    session["user"] = key
    return jsonify({"user": key})


@app.route("/api/login", methods=["POST"])
def login():
    if limited(auth_hits, request.remote_addr or "unknown", AUTH_LIMIT):
        return jsonify({"error": "Too many attempts. Wait a minute and try again."}), 429

    data = request.get_json(silent=True) or {}
    key = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))

    row = run("SELECT pw_hash FROM users WHERE username = ?", (key,), fetch=True)
    if not row or not check_password_hash(row[0], password):
        return jsonify({"error": "Wrong username or password."}), 401

    session.clear()
    session.permanent = True
    session["user"] = key
    return jsonify({"user": key})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


# ------------------------------------------
# CHAT (sign-in required)
# ------------------------------------------
@app.route("/chat", methods=["POST"])
def chat():
    user = session.get("user")
    if not user:
        return jsonify({"reply": "Please sign in first."}), 401

    if not GROQ_API_KEY:
        return jsonify({"reply": "Server has no GROQ_API_KEY set."}), 500

    if limited(chat_hits, user, CHAT_LIMIT):
        return jsonify({"reply": "Slow down a little. Too many messages. 😴"}), 429

    data = request.get_json(silent=True) or {}
    raw = data.get("messages", [])
    if not isinstance(raw, list):
        raw = []

    history = []
    for m in raw[-MAX_HISTORY:]:
        if not isinstance(m, dict):
            continue
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = str(m.get("content", ""))[:MAX_MESSAGE_CHARS]
        if content.strip():
            history.append({"role": role, "content": content})

    if not history:
        return jsonify({"reply": "Say something first."}), 400

    try:
        r = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history,
                "temperature": 0.9,
                "max_tokens": 1024,
            },
            timeout=60,
        )
    except requests.exceptions.RequestException as e:
        print("Network error:", e)
        return jsonify({"reply": "Can't reach Groq right now. Try again in a moment."}), 502

    if r.status_code == 200:
        try:
            reply = r.json()["choices"][0]["message"]["content"].strip()
            return jsonify({"reply": reply})
        except (KeyError, IndexError, ValueError, AttributeError):
            return jsonify({"reply": "Groq sent something I couldn't read."}), 502

    print("Groq error:", r.status_code, r.text[:500])
    if r.status_code == 401:
        msg = "Groq rejected the API key. Check it in Render's settings."
    elif r.status_code == 429:
        msg = "Groq rate limit reached. Try again soon."
    else:
        msg = f"Groq error {r.status_code}. Check the Render logs for details."
    return jsonify({"reply": msg}), 502


if __name__ == "__main__":
    app.run()
