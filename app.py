import os
import time
import requests
from collections import defaultdict, deque
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# ------------------------------------------
# SETTINGS
# ------------------------------------------
# Set your key in the same cmd window before running:
#   set GROQ_API_KEY="gsk_yourkeyhere"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "gsk_yourkeyhere").strip()
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

MAX_MESSAGE_CHARS = 2000
MAX_HISTORY = 20
RATE_LIMIT = 20          # requests
RATE_WINDOW = 60         # per this many seconds, per visitor

SYSTEM_PROMPT = (
    "You are RAGEBOT AI, a witty, sarcastic, playful chatbot with a "
    "mischievous personality. You tease the user in a light, funny way, "
    "like a sarcastic friend, but you ALWAYS still give a genuinely "
    "helpful and correct answer. Never be hateful and never insult people "
    "for their race, religion, gender, body, or other personal traits. "
    "Drop the sarcasm completely if the user seems genuinely upset or asks "
    "about something serious. Keep replies concise unless asked for detail. "
    "Use the occasional emoji like 😈."
)

hits = defaultdict(deque)


def rate_limited(ip):
    now = time.time()
    q = hits[ip]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    if not GROQ_API_KEY:
        return jsonify({"reply": "Server has no GROQ_API_KEY set. Set it in cmd and restart."}), 500

    if rate_limited(request.remote_addr):
        return jsonify({"reply": "Slow down a little. Too many messages. 😴"}), 429

    data = request.get_json(silent=True) or {}
    raw = data.get("messages", [])

    # Clean and limit the history sent by the browser
    history = []
    for m in raw[-MAX_HISTORY:]:
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
        return jsonify({"reply": "Can't reach Groq right now. Check your internet."}), 502

    if r.status_code == 200:
        try:
            reply = r.json()["choices"][0]["message"]["content"].strip()
            return jsonify({"reply": reply})
        except (KeyError, IndexError, ValueError):
            return jsonify({"reply": "Groq sent something I couldn't read."}), 502

    print("Groq error:", r.status_code, r.text[:500])
    if r.status_code == 401:
        msg = "Groq rejected the API key. Check it."
    elif r.status_code == 429:
        msg = "Groq rate limit reached. Try again soon."
    else:
        msg = f"Groq error {r.status_code}. Check the cmd window for details."
    return jsonify({"reply": msg}), 502


if __name__ == "__main__":
    app.run(debug=True)
