# WakeWatcher.py: offline wake word ("hello aion") that launches Aion.
# Uses Vosk (offline speech recognition) + sounddevice. Optional; Aion runs without it.
#
# Build:
#   pyinstaller --noconsole --onefile ^
#     --collect-all vosk ^
#     --collect-binaries sounddevice ^
#     scriptspy\WakeWatcher.py
#
# Autostart:
#   .\dist\WakeWatcher.exe --install-startup

import os, sys, json, time, queue, ctypes, subprocess
from pathlib import Path
import sounddevice as sd
from vosk import Model, KaldiRecognizer

# ---------- .env (same file Aion reads; real environment variables win) ----------
def _load_dotenv():
    base = Path(sys.executable).parent.parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent
    env = base / ".env"
    if not env.exists():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

_load_dotenv()

# ---------- Config ----------
TRIGGER_PHRASES = [p.strip() for p in os.getenv(
    "AURORA_WAKE_PHRASES",
    "hello aion,hey aion,hi aion,hello ion,hey ion,hello eye on,hello aurora,hey aurora"
).split(",") if p.strip()]

COOLDOWN_SEC  = float(os.getenv("AURORA_WAKE_COOLDOWN", "6"))
SAMPLE_RATE   = 16000
BLOCK_MS      = 50
BLOCK_SAMPLES = int(SAMPLE_RATE * BLOCK_MS / 1000)
MIC_INDEX_ENV = os.getenv("AURORA_WAKE_INPUT_INDEX")
MIC_NAME_ENV  = os.getenv("AURORA_WAKE_INPUT_NAME","").lower()
DEBUG_ALL     = os.getenv("AURORA_WAKE_DEBUG","0") == "1"

# ---------- Paths & logging ----------
def exe_dir() -> Path:
    # Folder of the EXE when frozen, else folder of this .py
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent

APP_DIR  = exe_dir()
LOGS_DIR = (APP_DIR / "logs")
LOGS_DIR.mkdir(exist_ok=True)
LOG_FILE = LOGS_DIR / "wakewatcher.log"

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

def find_python() -> str:
    base = APP_DIR
    for c in (base.parent/"myenv"/"Scripts"/"python.exe",
              base/"myenv"/"Scripts"/"python.exe",
              (sys.executable if not getattr(sys,"frozen",False) else None),
              "python"):
        if c and Path(c).exists():
            return str(c)
    return "python"

# ---------- Aion launch ----------
def project_root() -> Path:
    # WakeWatcher lives in <root>\scriptspy or <root>\dist
    return APP_DIR.parent

def is_process_running(name_contains: str) -> bool:
    try:
        out = subprocess.run(["tasklist"], capture_output=True, text=True,
                             creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        return name_contains.lower() in out.stdout.lower()
    except Exception:
        return False

def launch_aion():
    """Start Aion. Order: scripts/start_aion.bat, then dist/Aurora.exe, then scriptspy/Aurora.py."""
    root = project_root()
    try:
        if is_process_running("Aurora.exe"):
            log("Aion already running; skip launch.")
            return True
        bat = root / "scripts" / "start_aion.bat"
        exe = root / "dist" / "Aurora.exe"
        py  = root / "scriptspy" / "Aurora.py"
        if bat.exists():
            cmd = ["cmd", "/c", str(bat)]
        elif exe.exists():
            cmd = [str(exe)]
        elif py.exists():
            cmd = [find_python(), str(py)]
        else:
            log("ERROR: no start_aion.bat, Aurora.exe, or Aurora.py found.")
            return False
        subprocess.Popen(cmd, cwd=str(root))
        log(f"Launched Aion: {' '.join(cmd)}")
        return True
    except Exception as e:
        log(f"Launch failed: {e}")
        return False

# ---------- Vosk model discovery ----------
def _resolve_model_dir(candidate: Path) -> Path|None:
    """Return a usable model dir if `candidate` points directly to a model, or
    contains a subfolder that is a model."""
    try:
        if candidate.is_dir():
            # Direct model?
            if (candidate/"am").exists() or (candidate/"conf").exists():
                return candidate
            # Or first subdir that looks like a vosk model
            subs = [d for d in candidate.iterdir()
                    if d.is_dir() and d.name.lower().startswith("vosk-model")]
            if subs:
                return subs[0]
    except Exception:
        pass
    return None

def find_vosk_model_dir() -> Path|None:
    tried = []

    # 0) Env override – absolute path to model folder
    env = os.getenv("AURORA_VOSK_MODEL")
    if env:
        p = _resolve_model_dir(Path(env))
        tried.append(env)
        if p and p.exists():
            log(f"Using model via AURORA_VOSK_MODEL: {p}")
            return p

    # 1) Preferred: project models folder …\Aurora\models\*
    proj_models_root = APP_DIR.parent / "models"
    tried.append(str(proj_models_root))
    p = _resolve_model_dir(proj_models_root)
    if p and p.exists():
        log(f"Using project models folder: {p}")
        return p

    # 2) Fallback: dist\models\* (portable builds)
    dist_models_root = APP_DIR / "models"
    tried.append(str(dist_models_root))
    p = _resolve_model_dir(dist_models_root)
    if p and p.exists():
        log(f"Using dist models folder: {p}")
        return p

    # 3) Log all attempts, then fail
    log("Vosk model not found. Tried: " + " | ".join(tried))
    return None

# ---------- Mic selection ----------
def pick_mic_index():
    devs = sd.query_devices()
    if MIC_INDEX_ENV:
        try:
            idx = int(MIC_INDEX_ENV)
            if devs[idx].get("max_input_channels",0)>0: return idx
        except Exception: pass
    if MIC_NAME_ENV:
        for i,d in enumerate(devs):
            if d.get("max_input_channels",0)>0 and MIC_NAME_ENV in d["name"].lower():
                return i
    try:
        d = sd.default.device[0]
        if d is not None and d >= 0: return int(d)
    except Exception: pass
    for i,d in enumerate(devs):
        if d.get("max_input_channels",0)>0: return i
    return None

# ---------- Startup registry ----------
def install_startup():
    try:
        import winreg
    except Exception:
        print("winreg unavailable."); return
    exe_path = Path(sys.executable) if getattr(sys,"frozen",False) else (APP_DIR / "WakeWatcher.py")
    cmd = f"\"{exe_path}\"" if getattr(sys,"frozen",False) else f"\"{find_python()}\" \"{exe_path}\""
    key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "AuroraWakeWatcher", 0, winreg.REG_SZ, cmd)
        print("Installed to startup."); log("Installed to startup.")
    except Exception as e:
        print(f"Startup install failed: {e}"); log(f"Startup install failed: {e}")

def remove_startup():
    try:
        import winreg
    except Exception:
        print("winreg unavailable."); return
    key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, "AuroraWakeWatcher")
        print("Removed from startup."); log("Removed from startup.")
    except FileNotFoundError:
        print("Startup entry not found."); log("Startup entry not found.")
    except Exception as e:
        print(f"Startup remove failed: {e}"); log(f"Startup remove failed: {e}")

# ---------- Listener ----------
def run_listener():
    model_dir = find_vosk_model_dir()
    if not model_dir or not model_dir.exists():
        msg = (
            "Vosk model not found.\n"
            "Expected in either:\n"
            f"  {APP_DIR.parent/'models'}\n"
            f"  {APP_DIR/'models'}\n"
            "Or set AURORA_VOSK_MODEL to the model folder."
        )
        log("ERROR: " + msg.replace("\n", " "))
        try: ctypes.windll.user32.MessageBoxW(0, msg, "Aurora WakeWatcher", 0x10)
        except Exception: pass
        return

    log(f"Loading Vosk model: {model_dir}")
    model = Model(str(model_dir))

    grammar = TRIGGER_PHRASES + ["[unk]"]
    rec = KaldiRecognizer(model, SAMPLE_RATE, json.dumps(grammar))
    rec.SetWords(False)

    mic_index = pick_mic_index()
    if mic_index is None:
        log("ERROR: No input device."); return

    log(f"WakeWatcher started on mic index {mic_index} @ {SAMPLE_RATE} Hz; triggers={TRIGGER_PHRASES}")
    last_trigger = 0.0
    q = queue.Queue(maxsize=12)

    def cb(indata, frames, time_info, status):
        try: q.put(bytes(indata))
        except Exception: pass

    with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK_SAMPLES,
                           dtype="int16", channels=1, device=mic_index, callback=cb):
        last_partial = ""
        while True:
            data = q.get()
            if not data: continue

            if rec.AcceptWaveform(data):
                try:
                    txt = (json.loads(rec.Result()).get("text") or "").strip()
                except Exception:
                    txt = ""
                if txt:
                    if DEBUG_ALL: log(f"final: {txt}")
                    if any(p in txt.lower() for p in [t.lower() for t in TRIGGER_PHRASES]):
                        now = time.time()
                        if now - last_trigger >= COOLDOWN_SEC:
                            log(f"WAKE (final): {txt}")
                            launch_aion()
                            last_trigger = now
            else:
                try:
                    ptxt = (json.loads(rec.PartialResult()).get("partial") or "").strip()
                except Exception:
                    ptxt = ""
                if DEBUG_ALL and ptxt and ptxt != last_partial:
                    log(f"partial: {ptxt}"); last_partial = ptxt
                if ptxt and any(p in ptxt.lower() for p in [t.lower() for t in TRIGGER_PHRASES]):
                    now = time.time()
                    if now - last_trigger >= COOLDOWN_SEC:
                        log(f"WAKE (partial): {ptxt}")
                        launch_aion()
                        last_trigger = now

# ---------- Single-instance guard ----------
def already_running() -> bool:
    try:
        CreateMutex = ctypes.windll.kernel32.CreateMutexW
        GetLastError = ctypes.windll.kernel32.GetLastError
        mutex = CreateMutex(None, False, "Global\\AuroraWakeWatcherMutex")
        if not mutex:
            return False
        return GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False

# ---------- Entry ----------
if __name__ == "__main__":
#    if already_running():
#        log("Another WakeWatcher instance detected; exiting.")
#        sys.exit(0)

    if "--install-startup" in sys.argv:
        install_startup(); sys.exit(0)
    if "--remove-startup" in sys.argv:
        remove_startup(); sys.exit(0)
    if "--test" in sys.argv:
        print("Launch test:", "OK" if launch_aion() else "FAIL"); sys.exit(0)

    try:
        run_listener()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"FATAL: {e}")
