import asyncio
import threading
import urllib.request

from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = "7654368497:AAHME3-IAMPukJ6lzcFaVE8uT3JWXElMnbI"
PORT = 8000

_server_started = False
_server_error = ""


class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true, "worker": "test"}')

    def log_message(self, *args):
        pass


def _start_http():
    global _server_started, _server_error
    try:
        srv = HTTPServer(("0.0.0.0", PORT), PingHandler)
        _server_started = True
        srv.serve_forever()
    except Exception as e:
        _server_error = str(e)


def _get_public_ip() -> str:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
            return r.read().decode()
    except Exception:
        return "не вдалось визначити"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ip = _get_public_ip()
    if _server_started:
        status = f"✅ HTTP-сервер запущено на порту {PORT}"
    elif _server_error:
        status = f"❌ Помилка запуску HTTP: {_server_error}"
    else:
        status = "⏳ HTTP-сервер ще стартує..."

    await update.message.reply_text(
        f"🖥 <b>Тест воркера</b>\n\n"
        f"Публічний IP: <code>{ip}</code>\n"
        f"Порт: <code>{PORT}</code>\n"
        f"{status}\n\n"
        f"URL для перевірки:\n"
        f"<code>http://{ip}:{PORT}</code>",
        parse_mode="HTML",
    )


def main():
    t = threading.Thread(target=_start_http, daemon=True)
    t.start()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    print(f"Test worker bot started, HTTP on :{PORT}")
    app.run_polling()


if __name__ == "__main__":
    main()
