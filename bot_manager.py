import asyncio
import os
import shutil
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

from registry import RegistryManager

BOTS_DIR = "bots"


@dataclass
class BotProcess:
    process: subprocess.Popen | None = None
    log_buffer: deque = field(default_factory=lambda: deque(maxlen=200))
    reader_thread: threading.Thread | None = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None


class BotManager:
    def __init__(self, registry: RegistryManager):
        self._registry = registry
        self._processes: dict[str, BotProcess] = {}
        self._auto_restart: set[str] = set()
        self._tg_bot = None

    def set_telegram_bot(self, bot):
        self._tg_bot = bot

    def is_running(self, name: str) -> bool:
        bp = self._processes.get(name)
        if bp is not None:
            return bp.is_running()
        bot = self._registry.get_bot(name)
        if bot and bot.get("worker_id"):
            return bot.get("status") == "running"
        return False

    def get_logs(self, name: str, n: int = 30) -> str:
        bp = self._processes.get(name)
        if bp is None:
            return "(нет логов)"
        lines = list(bp.log_buffer)
        return "\n".join(lines[-n:]) if lines else "(пусто)"

    def get_resources(self, name: str) -> dict:
        if not _PSUTIL:
            return {"cpu": 0.0, "ram_mb": 0.0}
        bp = self._processes.get(name)
        if bp is None or not bp.is_running():
            return {"cpu": 0.0, "ram_mb": 0.0}
        try:
            proc = psutil.Process(bp.process.pid)
            return {
                "cpu": proc.cpu_percent(interval=0.1),
                "ram_mb": round(proc.memory_info().rss / 1024 / 1024, 1),
            }
        except psutil.NoSuchProcess:
            return {"cpu": 0.0, "ram_mb": 0.0}

    def get_all_resources(self) -> list[dict]:
        result = []
        for name, bp in self._processes.items():
            if bp.is_running():
                res = self.get_resources(name)
                bot = self._registry.get_bot(name)
                display = bot.get("display_name", name) if bot else name
                result.append({"name": name, "display": display, **res})
        return result

    @staticmethod
    def _venv_python(bot_path: str) -> str:
        scripts = "Scripts" if os.name == "nt" else "bin"
        exe = "python.exe" if os.name == "nt" else "python"
        return os.path.join(bot_path, "venv", scripts, exe)

    @staticmethod
    def _venv_pip(bot_path: str) -> str:
        scripts = "Scripts" if os.name == "nt" else "bin"
        exe = "pip.exe" if os.name == "nt" else "pip"
        return os.path.join(bot_path, "venv", scripts, exe)

    def _provision_blocking(self, name: str, bot_path: str) -> tuple[bool, str]:
        venv_dir = os.path.join(bot_path, "venv")
        try:
            subprocess.run([sys.executable, "-m", "venv", venv_dir],
                           check=True, capture_output=True)
            pip = self._venv_pip(bot_path)
            subprocess.run([pip, "install", "--upgrade", "pip"],
                           check=True, capture_output=True, timeout=120)
            req_file = os.path.join(bot_path, "requirements.txt")
            if os.path.exists(req_file):
                subprocess.run([pip, "install", "-r", req_file],
                               check=True, capture_output=True, timeout=300)
            self._registry.update_bot(name, provisioned=True)
            return True, "Зависимости установлены"
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b"").decode("utf-8", errors="replace")
            return False, f"Ошибка установки: {err[:500]}"
        except Exception as e:
            return False, str(e)

    async def provision_bot(self, name: str, bot_path: str) -> tuple[bool, str]:
        return await asyncio.to_thread(self._provision_blocking, name, bot_path)

    def _install_packages_blocking(self, bot_path: str, packages: list[str]) -> tuple[bool, str]:
        pip = self._venv_pip(bot_path)
        if not os.path.exists(pip):
            return False, "venv не найден — переустановите бота"
        try:
            result = subprocess.run([pip, "install"] + packages,
                                    capture_output=True, timeout=300)
            out = result.stdout.decode("utf-8", errors="replace")
            err = result.stderr.decode("utf-8", errors="replace")
            if result.returncode != 0:
                return False, (err or out)[:600]
            return True, f"Установлено: {', '.join(packages)}"
        except Exception as e:
            return False, str(e)

    async def install_packages(self, bot_path: str, packages: list[str]) -> tuple[bool, str]:
        return await asyncio.to_thread(self._install_packages_blocking, bot_path, packages)

    def start_bot(self, name: str) -> tuple[bool, str]:
        if self.is_running(name):
            return False, "Бот уже запущен"
        bot = self._registry.get_bot(name)
        if not bot:
            return False, "Бот не найден"
        bot_path = bot["path"]
        python_exe = self._venv_python(bot_path)
        if not os.path.exists(python_exe):
            python_exe = sys.executable
        entry_abs = os.path.join(bot_path, bot["entry_point"])
        try:
            env = os.environ.copy()
            env_file = os.path.join(bot_path, ".env")
            if os.path.exists(env_file):
                with open(env_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, _, v = line.partition("=")
                            env[k.strip()] = v.strip()
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            process = subprocess.Popen(
                [python_exe, entry_abs],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=bot_path,
                env=env,
                **kwargs,
            )
            bp = BotProcess(process=process)
            self._processes[name] = bp
            self._start_log_reader(bp)
            self._registry.update_bot(name, status="running", pid=process.pid)
            return True, f"Бот запущен (PID {process.pid})"
        except Exception as e:
            return False, str(e)

    def _start_log_reader(self, bp: BotProcess):
        def reader():
            for raw in iter(bp.process.stdout.readline, b""):
                bp.log_buffer.append(raw.decode("utf-8", errors="replace").rstrip())
        bp.reader_thread = threading.Thread(target=reader, daemon=True)
        bp.reader_thread.start()

    def schedule_watch(self, name: str):
        self._auto_restart.add(name)
        asyncio.get_event_loop().create_task(self._watcher(name))

    async def _watcher(self, name: str):
        await asyncio.sleep(20)
        while name in self._auto_restart:
            bp = self._processes.get(name)
            if bp and not bp.is_running():
                bot_info = self._registry.get_bot(name)
                if not bot_info or bot_info.get("status") != "running":
                    break
                await asyncio.sleep(30)
                if name not in self._auto_restart:
                    break
                ok, _ = self.start_bot(name)
                if ok and self._tg_bot:
                    owner_id = bot_info.get("owner_id")
                    if owner_id:
                        try:
                            display = bot_info.get("display_name", name)
                            await self._tg_bot.send_message(
                                chat_id=owner_id,
                                text=f"🔄 Бот <b>{display}</b> упал и был автоматически перезапущен.",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                return
            await asyncio.sleep(15)

    def stop_bot(self, name: str) -> tuple[bool, str]:
        self._auto_restart.discard(name)
        bp = self._processes.get(name)
        if bp is None or not bp.is_running():
            self._registry.update_bot(name, status="stopped", pid=None)
            return False, "Бот не запущен"
        try:
            bp.process.terminate()
            try:
                bp.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bp.process.kill()
            self._registry.update_bot(name, status="stopped", pid=None)
            return True, "Бот остановлен"
        except Exception as e:
            return False, str(e)

    def delete_bot(self, name: str) -> tuple[bool, str]:
        self._auto_restart.discard(name)
        self.stop_bot(name)
        self._processes.pop(name, None)
        bot = self._registry.get_bot(name)
        if bot:
            try:
                if os.path.exists(bot["path"]):
                    shutil.rmtree(bot["path"])
            except Exception as e:
                return False, f"Ошибка удаления файлов: {e}"
        self._registry.remove_bot(name)
        return True, "Бот удалён"

    def stop_all(self):
        for name in list(self._processes.keys()):
            self.stop_bot(name)
