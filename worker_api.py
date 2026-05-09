"""
Worker API + Telegram Bot
- FastAPI HTTP сервер на WORKER_PORT (default 8000)
- Telegram бот: /start → показує IP, порт і WORKER_SECRET для додавання в адмін-панель

Змінні оточення (.env):
  BOT_TOKEN      — токен Telegram бота (для /start команди)
  WORKER_SECRET  — секретний ключ (обов'язково)
  WORKER_PORT    — порт (за замовчуванням 8000)
"""
import asyncio
import os
import secrets
import shutil
import sys
import threading
import urllib.request
import zipfile

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

load_dotenv()

WORKER_PORT = int(os.getenv("WORKER_PORT", "8000"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOTS_DIR = "bots"
ENV_FILE = ".env"


def _ensure_secret() -> str:
    secret = os.getenv("WORKER_SECRET", "").strip()
    if secret:
        return secret
    secret = secrets.token_hex(16)
    with open(ENV_FILE, "a") as f:
        f.write(f"\nWORKER_SECRET={secret}\n")
    os.environ["WORKER_SECRET"] = secret
    print(f"[worker] Generated WORKER_SECRET={secret}")
    return secret


WORKER_SECRET = _ensure_secret()

app = FastAPI()


def _check(x_worker_secret: str = Header("")):
    if WORKER_SECRET and x_worker_secret != WORKER_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")


def _get_public_ip() -> str:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
            return r.read().decode()
    except Exception:
        return "невідомо"


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health(x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    try:
        import psutil
        ram_free = int(psutil.virtual_memory().available / 1024 / 1024)
    except Exception:
        ram_free = 0
    bots = len(os.listdir(BOTS_DIR)) if os.path.exists(BOTS_DIR) else 0
    return {"ok": True, "bots": bots, "running": _count_running(), "ram_free_mb": ram_free}


def _count_running() -> int:
    try:
        import psutil
        return sum(
            1 for p in psutil.process_iter(["cmdline"])
            if "bots/" in " ".join(p.info.get("cmdline") or []) and "python" in " ".join(p.info.get("cmdline") or [])
        )
    except Exception:
        return 0


# ── Deploy ZIP ────────────────────────────────────────────────────────────────
@app.post("/deploy")
async def deploy(
    bot_name: str = Form(...),
    display_name: str = Form(""),
    owner_id: int = Form(0),
    file: UploadFile = File(...),
    x_worker_secret: str = Header(""),
):
    _check(x_worker_secret)
    bot_path = os.path.join(BOTS_DIR, bot_name)
    os.makedirs(bot_path, exist_ok=True)
    zip_temp = os.path.join(bot_path, "_upload.zip")
    try:
        data = await file.read()
        with open(zip_temp, "wb") as f:
            f.write(data)
        with zipfile.ZipFile(zip_temp) as zf:
            for member in zf.namelist():
                if ".." in member or os.path.isabs(member):
                    shutil.rmtree(bot_path, ignore_errors=True)
                    return JSONResponse({"ok": False, "entry": "Небезпечний шлях у ZIP"})
            zf.extractall(bot_path)
    except zipfile.BadZipFile:
        shutil.rmtree(bot_path, ignore_errors=True)
        return JSONResponse({"ok": False, "entry": "Поганий ZIP"})
    finally:
        if os.path.exists(zip_temp):
            os.remove(zip_temp)
    entry = _find_entry(bot_path)
    if not entry:
        shutil.rmtree(bot_path, ignore_errors=True)
        return JSONResponse({"ok": False, "entry": "Не знайдено main.py або bot.py"})
    await _pip_install(bot_path)
    return JSONResponse({"ok": True, "entry": entry})


# ── Deploy Git ────────────────────────────────────────────────────────────────
class GitDeploy(BaseModel):
    bot_name: str
    git_url: str
    display_name: str = ""
    owner_id: int = 0


@app.post("/deploy_git")
async def deploy_git(body: GitDeploy, x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    bot_path = os.path.join(BOTS_DIR, body.bot_name)
    try:
        import git
        git.Repo.clone_from(body.git_url, bot_path, depth=1)
    except Exception as e:
        shutil.rmtree(bot_path, ignore_errors=True)
        return JSONResponse({"ok": False, "entry": str(e)[:300]})
    entry = _find_entry(bot_path)
    if not entry:
        shutil.rmtree(bot_path, ignore_errors=True)
        return JSONResponse({"ok": False, "entry": "Не знайдено main.py або bot.py"})
    await _pip_install(bot_path)
    return JSONResponse({"ok": True, "entry": entry})


# ── Start / Stop ──────────────────────────────────────────────────────────────
_procs: dict[str, asyncio.subprocess.Process] = {}


@app.post("/start/{bot_name}")
async def start_bot(bot_name: str, x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    if bot_name in _procs and _procs[bot_name].returncode is None:
        return {"ok": True, "msg": "Вже запущено"}
    bot_path = os.path.join(BOTS_DIR, bot_name)
    entry = _find_entry(bot_path)
    if not entry:
        return {"ok": False, "msg": "Точку входу не знайдено"}
    env = os.environ.copy()
    env_file = os.path.join(bot_path, ".env")
    if os.path.exists(env_file):
        from dotenv import dotenv_values
        env.update(dotenv_values(env_file))
    log_f = open(os.path.join(bot_path, "bot.log"), "a")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, entry, cwd=bot_path, env=env,
        stdout=log_f, stderr=log_f,
    )
    _procs[bot_name] = proc
    return {"ok": True, "msg": f"Запущено (PID {proc.pid})"}


@app.post("/stop/{bot_name}")
async def stop_bot(bot_name: str, x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    proc = _procs.get(bot_name)
    if not proc or proc.returncode is not None:
        return {"ok": True, "msg": "Вже зупинено"}
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
    _procs.pop(bot_name, None)
    return {"ok": True, "msg": "Зупинено"}


# ── Delete ────────────────────────────────────────────────────────────────────
@app.delete("/bots/{bot_name}")
async def delete_bot(bot_name: str, x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    proc = _procs.get(bot_name)
    if proc and proc.returncode is None:
        proc.terminate()
        _procs.pop(bot_name, None)
    shutil.rmtree(os.path.join(BOTS_DIR, bot_name), ignore_errors=True)
    return {"ok": True, "msg": "Видалено"}


# ── Logs ──────────────────────────────────────────────────────────────────────
@app.get("/logs/{bot_name}")
async def get_logs(bot_name: str, n: int = 30, x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    log_path = os.path.join(BOTS_DIR, bot_name, "bot.log")
    if not os.path.exists(log_path):
        return {"logs": ""}
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return {"logs": "".join(lines[-n:])}


# ── Resources ─────────────────────────────────────────────────────────────────
@app.get("/resources")
async def resources(x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    try:
        import psutil
        result = []
        for name, proc in list(_procs.items()):
            if proc.returncode is not None:
                continue
            try:
                p = psutil.Process(proc.pid)
                result.append({
                    "name": name, "display": name,
                    "cpu": round(p.cpu_percent(interval=0.1), 1),
                    "ram_mb": round(p.memory_info().rss / 1024 / 1024, 1),
                })
            except Exception:
                pass
        return result
    except Exception:
        return []


# ── Install ───────────────────────────────────────────────────────────────────
class InstallBody(BaseModel):
    packages: list[str]


@app.post("/install/{bot_name}")
async def install_packages(bot_name: str, body: InstallBody, x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", *body.packages,
        cwd=os.path.join(BOTS_DIR, bot_name),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        return {"ok": False, "msg": stderr.decode(errors="replace")[-500:]}
    return {"ok": True, "msg": f"Встановлено: {' '.join(body.packages)}"}


# ── Config ────────────────────────────────────────────────────────────────────
@app.get("/config/{bot_name}")
async def get_config(bot_name: str, x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    env_file = os.path.join(BOTS_DIR, bot_name, ".env")
    if not os.path.exists(env_file):
        return {"content": ""}
    with open(env_file, encoding="utf-8") as f:
        return {"content": f.read().strip()}


class ConfigBody(BaseModel):
    content: str


@app.post("/config/{bot_name}")
async def save_config(bot_name: str, body: ConfigBody, x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    with open(os.path.join(BOTS_DIR, bot_name, ".env"), "w", encoding="utf-8") as f:
        f.write(body.content + "\n")
    return {"ok": True}


# ── Files ─────────────────────────────────────────────────────────────────────
HIDDEN = {".env", ".git"}


@app.get("/files/{bot_name}")
async def list_files(bot_name: str, x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    bot_path = os.path.join(BOTS_DIR, bot_name)
    if not os.path.exists(bot_path):
        return {"files": []}
    return {"files": sorted(
        f for f in os.listdir(bot_path)
        if os.path.isfile(os.path.join(bot_path, f)) and f not in HIDDEN
    )}


@app.get("/files/{bot_name}/{fname}")
async def download_file(bot_name: str, fname: str, x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    file_path = os.path.join(BOTS_DIR, bot_name, fname)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404)
    with open(file_path, "rb") as f:
        return Response(content=f.read(), media_type="application/octet-stream")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _find_entry(bot_path: str) -> str | None:
    for name in ("main.py", "bot.py"):
        if os.path.exists(os.path.join(bot_path, name)):
            return name
    subdirs = [d for d in os.listdir(bot_path)
               if os.path.isdir(os.path.join(bot_path, d)) and d not in ("venv", ".git")]
    if len(subdirs) == 1:
        sub = os.path.join(bot_path, subdirs[0])
        for name in ("main.py", "bot.py"):
            if os.path.exists(os.path.join(sub, name)):
                for item in os.listdir(sub):
                    src, dst = os.path.join(sub, item), os.path.join(bot_path, item)
                    if not os.path.exists(dst):
                        shutil.move(src, dst)
                shutil.rmtree(sub, ignore_errors=True)
                return name
    return None


async def _pip_install(bot_path: str):
    req = os.path.join(bot_path, "requirements.txt")
    if not os.path.exists(req):
        return
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "-r", req,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


# ── Telegram Bot (/start → показує IP + секрет) ───────────────────────────────
def _run_telegram_bot():
    if not BOT_TOKEN:
        return

    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        ip = _get_public_ip()
        await update.message.reply_text(
            f"🖥 <b>Worker API</b>\n\n"
            f"IP: <code>{ip}</code>\n"
            f"Port: <code>{WORKER_PORT}</code>\n"
            f"URL: <code>http://{ip}:{WORKER_PORT}</code>\n\n"
            f"🔑 Secret: <code>{WORKER_SECRET}</code>\n\n"
            f"Додайте цей воркер в адмін-панелі головного бота:\n"
            f"🛠 Адмін → 🖥 Воркеры → ➕ Добавити воркер",
            parse_mode="HTML",
        )

    async def _run():
        tg_app = Application.builder().token(BOT_TOKEN).build()
        tg_app.add_handler(CommandHandler("start", start_cmd))
        async with tg_app:
            await tg_app.start()
            await tg_app.updater.start_polling()
            await asyncio.Event().wait()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    t = threading.Thread(target=_run_telegram_bot, daemon=True)
    t.start()

    print(f"Worker API starting on port {WORKER_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=WORKER_PORT)
