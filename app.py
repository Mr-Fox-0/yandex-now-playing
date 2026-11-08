import os
import threading
import time
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from yandex_music import Client
from yandex_music.ynison import YnisonClient
app = Flask(__name__, static_folder="static")
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)
lock = threading.RLock()

state = {
    "auth_status": "not_started",
    "auth_message": "Нажмите «Подключить Яндекс».",
    "verification_url": None,
    "user_code": None,
    "expires_at": None,
    "connected": False,
    "track": None,
    "last_update": None,
}
ynison_client = None
auth_thread = None
ACCESS_PIN = os.getenv("ACCESS_PIN", "").strip()


def authorized():
    if not ACCESS_PIN:
        return True
    supplied = request.headers.get("X-Access-Pin", "")
    return supplied == ACCESS_PIN


@app.before_request
def protect_api():
    if request.path in ("/", "/healthz") or request.path.startswith("/static/"):
        return None
    if not authorized():
        return jsonify({"error": "Неверный PIN"}), 401


def read_attr(obj, name, default=None):
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def cover_url(track):
    cover = read_attr(track, "cover")
    uri = read_attr(cover, "uri") if cover else None
    if not uri:
        uri = read_attr(track, "cover_uri")
    if not uri:
        return None
    uri = str(uri)
    if uri.startswith("//"):
        return "https:" + uri
    if uri.startswith("http://") or uri.startswith("https://"):
        return uri
    if "%%" in uri:
        uri = uri.replace("%%", "500x500")
    return "https://" + uri.lstrip("/")


def serialise_track(track):
    if not track:
        return None
    title = read_attr(track, "title") or read_attr(track, "name")
    if not title:
        return None
    return {
        "title": str(title),
        "cover": cover_url(track),
    }


def start_ynison(access_token):
    global ynison_client
    try:
        client = YnisonClient(access_token)
        ynison_client = client

        def on_state(_frame):
            playable = read_attr(client, "current_playable")
            item = serialise_track(playable)
            with lock:
                state["connected"] = True
                state["auth_status"] = "connected"
                state["auth_message"] = "Подключено к Ynison."
                state["track"] = item
                state["last_update"] = int(time.time())

        client.on_state(on_state)
        client.connect()
        with lock:
            state["connected"] = False
            state["auth_status"] = "disconnected"
            state["auth_message"] = "Соединение Ynison завершено."
    except Exception as exc:
        with lock:
            state["connected"] = False
            state["auth_status"] = "error"
            state["auth_message"] = "Ошибка Ynison: " + str(exc)[:240]


def auth_worker():
    try:
        client = Client()

        def on_code(code):
            with lock:
                state["verification_url"] = str(code.verification_url)
                state["user_code"] = str(code.user_code)
                state["expires_at"] = int(time.time()) + int(code.expires_in)
                state["auth_status"] = "waiting_for_approval"
                state["auth_message"] = "Откройте страницу Яндекса и подтвердите вход кодом."

        token = client.device_auth(on_code=on_code)
        access_token = getattr(token, "access_token", None)
        if not access_token:
            raise RuntimeError("Яндекс не вернул access token.")
        with lock:
            state["auth_status"] = "connecting"
            state["auth_message"] = "Авторизация подтверждена. Подключаем Ynison…"
            state["verification_url"] = None
            state["user_code"] = None
        start_ynison(access_token)
    except Exception as exc:
        with lock:
            state["auth_status"] = "error"
            state["auth_message"] = "Ошибка авторизации: " + str(exc)[:240]
            state["verification_url"] = None
            state["user_code"] = None


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/healthz")
def health():
    return "ok", 200


@app.post("/api/auth/start")
def auth_start():
    global auth_thread
    with lock:
        if auth_thread and auth_thread.is_alive():
            return jsonify({"ok": True, "message": "Авторизация уже запущена."})
        state.update({
            "auth_status": "starting",
            "auth_message": "Запрашиваем код у Яндекса…",
            "verification_url": None,
            "user_code": None,
            "expires_at": None,
            "connected": False,
            "track": None,
        })
        auth_thread = threading.Thread(target=auth_worker, daemon=True)
        auth_thread.start()
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    with lock:
        return jsonify(dict(state))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
