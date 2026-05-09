"""
Worker API — запускається на кожному воркер-сервері.
python worker_api.py

Змінні оточення:
  WORKER_SECRET  — секретний ключ (обов'язково)
  WORKER_PORT    — порт (за замовчуванням 8000)
"""
import asyncio
import os
import shutil
import sys
import zipfile

from fastapi import FastAPI, Header, HTTPException, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

WORKER_SECRET = os.getenv("WORKER_SECRET", "")
BOTS_DIR = "bots"

app = FastAPI()


def _check(x_worker_secret: str = Header("")):
    if WORKER_SECRET and x_worker_secret != WORKER_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health(x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    try:
        import psutil
        ram_free = int(psutil.virtual_memory().available / 1024 / 1024)
    except Exception:
        ram_free = 0
    bots_dir = BOTS_DIR
    bots = len(os.listdir(bots_dir)) if os.path.exists(bots_dir) else 0
    running = _count_running()
    return {"ok": True, "bots": bots, "running": running, "ram_free_mb": ram_free}


def _count_running() -> int:
    try:
        import psutil
        count = 0
        for p in psutil.process_iter(["cmdline"]):
            cmdline = " ".join(p.info.get("cmdline") or [])
            if "bots/" in cmdline and "python" in cmdline:
                count += 1
        return count
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
    env_file = os.path.join(bot_path, ".env")
    env = os.environ.copy()
    if os.path.exists(env_file):
        from dotenv import dotenv_values
        env.update(dotenv_values(env_file))
    log_path = os.path.join(bot_path, "bot.log")
    log_f = open(log_path, "a")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, entry,
        cwd=bot_path, env=env,
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
    bot_path = os.path.join(BOTS_DIR, bot_name)
    shutil.rmtree(bot_path, ignore_errors=True)
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
        for bot_name, proc in list(_procs.items()):
            if proc.returncode is not None:
                continue
            try:
                p = psutil.Process(proc.pid)
                result.append({
                    "name": bot_name,
                    "display": bot_name,
                    "cpu": round(p.cpu_percent(interval=0.1), 1),
                    "ram_mb": round(p.memory_info().rss / 1024 / 1024, 1),
                })
            except Exception:
                pass
        return result
    except Exception:
        return []


# ── Install packages ──────────────────────────────────────────────────────────
class InstallBody(BaseModel):
    packages: list[str]


@app.post("/install/{bot_name}")
async def install_packages(bot_name: str, body: InstallBody, x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    bot_path = os.path.join(BOTS_DIR, bot_name)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", *body.packages,
        cwd=bot_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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
    env_file = os.path.join(BOTS_DIR, bot_name, ".env")
    with open(env_file, "w", encoding="utf-8") as f:
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
    files = sorted([
        f for f in os.listdir(bot_path)
        if os.path.isfile(os.path.join(bot_path, f)) and f not in HIDDEN
    ])
    return {"files": files}


@app.get("/files/{bot_name}/{fname}")
async def download_file(bot_name: str, fname: str, x_worker_secret: str = Header("")):
    _check(x_worker_secret)
    file_path = os.path.join(BOTS_DIR, bot_name, fname)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404)
    with open(file_path, "rb") as f:
        data = f.read()
    return Response(content=data, media_type="application/octet-stream")


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
                    src = os.path.join(sub, item)
                    dst = os.path.join(bot_path, item)
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
        cwd=bot_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("WORKER_PORT", "8000"))
    print(f"Worker API starting on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
