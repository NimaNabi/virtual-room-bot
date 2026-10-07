"""Virtual Room Bot — native manager for Windows (also works on macOS/Linux without Docker).

Everything lives inside the bot folder:
    runtime/   private Python, Java and Lavalink (downloaded by Setup, never installed system-wide)
    data/      database, server configuration, backups (survives updates)
    logs/      bot.log, lavalink.log
    .env       your settings and secrets (never shared)

Usage:  python installer/vrb.py <command>
    menu | configure | start | stop | restart | status | logs [N] | backup | diagnostics | invite | token |
    update [--check] [--yes] | rollback [--yes] | autostart on|off
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME, DATA, LOGS = ROOT / "runtime", ROOT / "data", ROOT / "logs"
ENV = ROOT / ".env"
PIDS = DATA / "native-pids.json"
SUP = DATA / "supervisor.json"          # supervisor state: running / stopped / gave up, restarts, last reason
SUP_STOP = DATA / "supervisor.stop"     # an intentional Stop: the supervisor must not restart anything
CRASH_WINDOW, CRASH_LIMIT = 600, 5      # more than 5 unexpected exits within 10 minutes -> stop trying
BACKOFF_MAX = 300
WIN = os.name == "nt"
REPO = "NimaNabi/virtual-room-bot"
INVITE_PERMISSIONS = 1376838348023
KEEP = {"data", "runtime", "logs", ".env", "previous", ".git"}   # never replaced by an update
UA = {"User-Agent": "virtual-room-bot-installer"}


# ---------------------------------------------------------------- small helpers
def say(msg: str = "") -> None:
    print(msg, flush=True)


def read_env(path: Path | None = None) -> dict[str, str]:
    path = path or ENV
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def set_env_value(key: str, value: str, path: Path | None = None) -> None:
    """Set KEY=value in .env, keeping every other line and comment exactly as it is."""
    path = path or ENV
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done = False
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={value}"
            done = True
    if not done:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_env() -> None:
    if not ENV.exists():
        shutil.copyfile(ROOT / ".env.example", ENV)
    if not read_env().get("LAVALINK_PASSWORD") or read_env().get("LAVALINK_PASSWORD") == "change-me":
        set_env_value("LAVALINK_PASSWORD", secrets.token_urlsafe(32))


def python_exe() -> str:
    cand = RUNTIME / "python" / ("python.exe" if WIN else "bin/python3")
    return str(cand) if cand.exists() else sys.executable


def java_exe() -> Path:
    return RUNTIME / "java" / "bin" / ("java.exe" if WIN else "java")


def bot_env() -> dict[str, str]:
    env = {**os.environ, **read_env()}
    # native layout (Docker uses its own paths; these only apply here)
    env.update(DATA_DIR=str(DATA), CONFIG_PATH=str(DATA / "server.yaml"), LAVALINK_URI="http://127.0.0.1:2333",
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if not music_on(env):
        env.pop("LAVALINK_URI", None)
    return env


def music_on(env: dict | None = None) -> bool:
    env = env or read_env()
    return "music" in env.get("COMPOSE_PROFILES", "music") and java_exe().exists() \
        and (RUNTIME / "lavalink" / "Lavalink.jar").exists()


# ---------------------------------------------------------------- processes
def alive(pid: int | None) -> bool:
    if not pid:
        return False
    if WIN:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259     # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def our_processes() -> list[dict]:
    """Processes started from THIS folder's private runtime (python/java under runtime/), found by executable path.
    More reliable than remembered PIDs: Windows launchers may hand off to a child process."""
    if not WIN:
        return []
    rt = str(RUNTIME).replace("'", "''")
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith('" + rt +
          "\', [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { '{0}|{1}|{2}' -f $_.ProcessId, $_.Name, $_.CommandLine }")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                       creationflags=0x08000000)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) != os.getpid():
            out.append({"pid": int(parts[0]), "name": parts[1].lower(), "cmd": parts[2]})
    return out


def role(proc: dict) -> str | None:
    if "vrb.py" in proc["cmd"] and " supervise" in proc["cmd"]:
        return "supervisor"
    if "-m vrbot" in proc["cmd"] and "installer" not in proc["cmd"]:
        return "bot"
    if proc["name"].startswith("java") and "Lavalink.jar" in proc["cmd"]:
        return "lavalink"
    return None


def running(which: str) -> bool:
    if WIN:
        return any(role(p) == which for p in our_processes())
    return alive(pids().get(which))


def pids() -> dict:
    try:
        return json.loads(PIDS.read_text())
    except (OSError, ValueError):
        return {}


def spawn(name: str, args: list[str], cwd: Path, env: dict) -> int:
    LOGS.mkdir(exist_ok=True)
    log = open(LOGS / f"{name}.log", "ab")
    kw: dict = {"cwd": str(cwd), "env": env, "stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(args, **kw).pid


def rotate_logs(limit: int = 10 * 1024 * 1024) -> None:
    for f in LOGS.glob("*.log") if LOGS.exists() else []:
        if f.stat().st_size > limit:
            old = f.with_suffix(".log.1")
            old.unlink(missing_ok=True)
            f.rename(old)


def heartbeat() -> dict:
    try:
        return json.loads((DATA / "heartbeat.json").read_text())
    except (OSError, ValueError):
        return {}


def lavalink_ok(timeout: float = 3) -> bool:
    try:
        req = urllib.request.Request("http://127.0.0.1:2333/version",
                                     headers={"Authorization": read_env().get("LAVALINK_PASSWORD", "")})
        return urllib.request.urlopen(req, timeout=timeout).status == 200
    except (urllib.error.URLError, OSError):
        return False


def sup_state() -> dict:
    try:
        return json.loads(SUP.read_text())
    except (OSError, ValueError):
        return {}


def save_sup(**kw) -> None:
    st = {**sup_state(), **kw}
    SUP.write_text(json.dumps(st))


def start(quiet: bool = False) -> int:
    """Start the background supervisor (it starts the music service and the bot and restarts them after a crash)."""
    ensure_env()
    DATA.mkdir(exist_ok=True)
    if running("supervisor") or (not WIN and alive(sup_state().get("pid"))):
        if not quiet:
            say("The bot is already running.")
        return 0
    rotate_logs()
    for f in (SUP_STOP, DATA / "stop.request"):
        f.unlink(missing_ok=True)
    py = python_exe()
    if WIN and Path(py).with_name("pythonw.exe").exists():
        py = str(Path(py).with_name("pythonw.exe"))   # no console window
    spawn("supervisor", [py, str(Path(__file__).resolve()), "supervise"], ROOT, {**os.environ, "PYTHONUTF8": "1"})
    if not quiet:
        say("Starting the bot…")
        say(describe(wait_state(75)))
    return 0


def start_children(p: dict) -> dict:
    """Start whatever of Lavalink and the bot isn't running (used by the supervisor)."""
    env = bot_env()
    if music_on() and not running("lavalink"):
        LOGS.mkdir(exist_ok=True)
        ll = RUNTIME / "lavalink"
        shutil.copyfile(ROOT / "lavalink" / "application.yml", ll / "application.yml")
        jenv = {**os.environ, "LAVALINK_SERVER_PASSWORD": env.get("LAVALINK_PASSWORD", ""),
                "SERVER_ADDRESS": "127.0.0.1", "_JAVA_OPTIONS": "-Xmx320m -XX:+UseSerialGC"}
        p["lavalink"] = spawn("lavalink", [str(java_exe()), "-jar", "Lavalink.jar"], ll, jenv)
        for _ in range(60):                 # the bot connects to music on its own once it is up
            if lavalink_ok(1):
                break
            time.sleep(1)
    return p


def supervise() -> int:
    """Runs in the background: keeps the bot (and the music service) running.

    * unexpected exit -> restart after a back-off of 5 s, 10 s, 20 s … up to 5 min (reset after 10 min of stable running)
    * more than 5 unexpected exits within 10 minutes -> give up and record why (no endless crash loop)
    * an intentional Stop (supervisor.stop) -> no restart
    """
    DATA.mkdir(exist_ok=True)
    save_sup(pid=os.getpid(), state="running", started=time.time())
    crashes: list[float] = []
    delay = 5
    while not SUP_STOP.exists():
        p = start_children(pids())
        env = bot_env()
        t0 = time.time()
        proc = subprocess.Popen([python_exe(), "-m", "vrbot"], cwd=str(ROOT), env=env,
                                stdout=open(LOGS / "bot.log", "ab"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                **({"creationflags": 0x08000000} if WIN else {}))
        p["bot"] = proc.pid
        PIDS.write_text(json.dumps(p))
        while proc.poll() is None:
            if SUP_STOP.exists():
                break
            if p.get("lavalink") and not alive(p["lavalink"]) and not SUP_STOP.exists():   # music service died
                p = start_children(p)
                PIDS.write_text(json.dumps({**p, "bot": proc.pid}))
            time.sleep(2)
        if SUP_STOP.exists():
            break
        code = proc.wait()
        code = code - (1 << 32) if code >= (1 << 31) else code   # Windows reports -1 as 4294967295
        ran = time.time() - t0
        reason = "watchdog restart (bot was stuck)" if code == 3 else f"bot exited unexpectedly (exit code {code})"
        now = time.time()
        crashes = [c for c in crashes if now - c < CRASH_WINDOW] + [now]
        delay = 5 if ran > CRASH_WINDOW else min(delay * 2 if len(crashes) > 1 else 5, BACKOFF_MAX)
        save_sup(restarts=sup_state().get("restarts", 0) + 1, last_exit=now, last_reason=reason)
        if len(crashes) > CRASH_LIMIT:
            save_sup(state="gave up", last_reason=f"{reason}; {len(crashes)} crashes within 10 minutes — not restarting")
            break
        for _ in range(delay):
            if SUP_STOP.exists():
                break
            time.sleep(1)
    if sup_state().get("state") != "gave up":
        save_sup(state="stopped")
    for proc_ in our_processes():
        if role(proc_) == "lavalink":
            kill(proc_["pid"])
    return 0


def wait_state(seconds: int) -> str:
    t0 = time.time()
    while time.time() - t0 < seconds:
        hb = heartbeat()
        if hb.get("ts", 0) >= t0 - 1 and hb.get("state") in ("ready", "no_server", "no_token"):
            return hb["state"]
        if not running("bot") and not running("supervisor") and time.time() - t0 > 10:
            return "crashed"
        time.sleep(2)
    return heartbeat().get("state", "starting") if running("bot") else "crashed"


def describe(state: str) -> str:
    return {
        "ready": "✅ The bot is online and in your server.",
        "no_server": "✅ The bot is online, but not in a server yet. Use the invite link (menu → Invite link).",
        "no_token": "⚠️ No bot token yet. Choose 'Change bot token' in the menu.",
        "connecting": "⏳ Still connecting to Discord…",
        "crashed": "❌ The bot stopped. Open 'View logs' to see why.",
        "stopping": "⏹️ Stopping…",
    }.get(state, f"⏳ {state}")


def stop(quiet: bool = False) -> int:
    p = pids()
    DATA.mkdir(exist_ok=True)
    SUP_STOP.write_text("stop")                       # intentional: the supervisor must not restart the bot
    if running("bot") and heartbeat().get("state") != "no_token":
        (DATA / "stop.request").write_text("stop")     # clean shutdown: the bot records it and exits by itself
        for _ in range(20):
            if not running("bot"):
                break
            time.sleep(1)
    for proc in our_processes():                      # anything of ours still running (bot, music server)
        if role(proc):
            kill(proc["pid"])
    for pid in p.values():
        if alive(pid):
            kill(pid)
    for _ in range(10):                               # wait until they are really gone
        if not any(role(x) for x in our_processes()):
            break
        time.sleep(0.5)
    (DATA / "stop.request").unlink(missing_ok=True)
    PIDS.unlink(missing_ok=True)
    if sup_state().get("state") == "running":
        save_sup(state="stopped")
    if not quiet:
        say("⏹️ Stopped.")
    return 0


def kill(pid: int) -> None:
    if WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def status() -> int:
    hb = heartbeat()
    is_running = running("bot")
    say(f"Version:  {(ROOT / 'VERSION').read_text().strip() if (ROOT / 'VERSION').exists() else '?'}")
    say(f"Bot:      {'running' if is_running else 'stopped'}"
        + (f" · {describe(hb.get('state', 'connecting'))}" if is_running else ""))
    if is_running and hb.get("guild"):
        say(f"Server:   {hb['guild']} · latency {hb.get('latency_ms')} ms")
    say(f"Music:    {'running' if lavalink_ok() else 'not running' if music_on() else 'off'}")
    sup = sup_state()
    if sup:
        say(f"Recovery: {sup.get('state', '?')} · {sup.get('restarts', 0)} automatic restart(s)"
            + (f" · last: {sup.get('last_reason')}" if sup.get("last_reason") else ""))
    say(f"Data:     {DATA}")
    say("Note: the bot is online only while this computer is on and connected to the internet.")
    return 0 if is_running else 1


def logs(n: int = 40) -> int:
    f = LOGS / "bot.log"
    if not f.exists():
        say("No logs yet.")
        return 0
    for raw in f.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
        try:
            d = json.loads(raw)
            say(f"{d.get('ts', '')[11:19]} {d.get('level', ''):<7} {d.get('msg', '')}")
        except ValueError:
            say(raw)
    say(f"\nFull logs: {LOGS}")
    return 0


def diagnostics() -> int:
    """Safe report for a GitHub issue: no token, keys, message text or member data. Saved to data/diagnostics.txt."""
    log = LOGS / "bot.log"
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:] if log.exists() else []
    sys.path.insert(0, str(ROOT))
    from vrbot.diagnostics import report, sanitize
    env = bot_env()
    old = dict(os.environ)
    os.environ.update(env)
    try:
        sup = sup_state()
        extra = {"Install": "Windows (native)" if WIN else "native", "Java": "present" if java_exe().exists() else "missing",
                 "Music service": "running" if lavalink_ok() else "not running",
                 "Recovery": f"{sup.get('state', 'n/a')}, {sup.get('restarts', 0)} restarts, last: {sup.get('last_reason', '-')}"}
        ll = LOGS / "lavalink.log"
        if ll.exists():
            errs = [x for x in ll.read_text(encoding="utf-8", errors="replace").splitlines()[-400:] if " ERROR " in x or "Exception" in x]
            extra["Music service errors"] = sanitize(" | ".join(errs[-3:]))[:600] or "none"
        text = report(DATA, ROOT, lines, extra)
    finally:
        os.environ.clear()
        os.environ.update(old)
    (DATA / "diagnostics.txt").write_text(text, encoding="utf-8")
    say(text)
    say(f"\nSaved to {DATA / 'diagnostics.txt'} — attach it to your GitHub issue.")
    return 0


def backup() -> int:
    r = subprocess.run([python_exe(), "-m", "vrbot.cli", "backup"], cwd=ROOT, env=bot_env())
    return r.returncode


# ---------------------------------------------------------------- Discord token + invite
def discord_get(path: str, token: str) -> dict:
    req = urllib.request.Request(f"https://discord.com/api/v10{path}", headers={"Authorization": f"Bot {token}", **UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def invite_url(app_id: str) -> str:
    return (f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions={INVITE_PERMISSIONS}"
            "&integration_type=0&scope=bot+applications.commands")


def ask_token() -> tuple[str, dict] | None:
    say("Paste your bot token (Developer Portal → your application → Bot → Reset Token).")
    say("It stays hidden while you paste (right-click or Ctrl+V), then press Enter.")
    for _ in range(3):
        tok = getpass.getpass("Paste the bot token here, then Enter: ").strip()
        if not tok:
            return None
        try:
            me = discord_get("/users/@me", tok)
            app = discord_get("/oauth2/applications/@me", tok)
            say(f"✅ Token works: the bot is called {me.get('username')}.")
            return tok, app
        except urllib.error.HTTPError as e:
            say("❌ Discord did not accept that token. Copy it again (Reset Token) and retry." if e.code == 401
                else f"❌ Discord answered HTTP {e.code}. Try again.")
        except urllib.error.URLError:
            say("❌ Could not reach Discord. Check the internet connection.")
    return None


def members_intent_on(app: dict) -> bool:
    flags = int(app.get("flags") or 0)
    return bool(flags & (1 << 14) or flags & (1 << 15))     # GATEWAY_GUILD_MEMBERS (+ _LIMITED)


def token_flow() -> str | None:
    got = ask_token()
    if not got:
        return None
    tok, app = got
    set_env_value("DISCORD_TOKEN", tok)
    while not members_intent_on(app):
        say("\n⚠️ One switch is missing: Developer Portal → your application → Bot →")
        say("   turn ON 'Server Members Intent' and click Save Changes.")
        input("   Press Enter when done… ")
        try:
            app = discord_get("/oauth2/applications/@me", tok)
        except urllib.error.URLError:
            pass
    return str(app.get("id"))


def invite() -> int:
    """Print (and open) the invite link for the configured bot token."""
    tok = read_env().get("DISCORD_TOKEN")
    if not tok:
        say("No bot token yet: run 'configure' (or Setup.cmd) first.")
        return 1
    try:
        app_id = str(discord_get("/oauth2/applications/@me", tok).get("id"))
    except urllib.error.URLError:
        say("Could not ask Discord for the application ID (token invalid or no internet).")
        return 1
    url = invite_url(app_id)
    DATA.mkdir(exist_ok=True)
    (DATA / "invite-url.txt").write_text(url)
    say(url)
    return 0


# ---------------------------------------------------------------- autostart (current user, no admin)
def startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut(path: Path, target: str, args: str, workdir: Path, desc: str) -> None:
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{path}');$s.TargetPath='{target}';"
          f"$s.Arguments='{args}';$s.WorkingDirectory='{workdir}';$s.WindowStyle=7;$s.Description='{desc}';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], check=True)


def autostart(on: bool) -> int:
    if not WIN:
        say("Autostart helper is for Windows. On Linux/macOS use Docker (restart: unless-stopped) or your init system.")
        return 1
    lnk = startup_dir() / "Virtual Room Bot.lnk"
    if on:
        pyw = str(Path(python_exe()).with_name("pythonw.exe"))
        shortcut(lnk, pyw, f'"{Path(__file__)}" start --quiet', ROOT, "Start Virtual Room Bot when you sign in")
        say("✅ The bot will start automatically when you sign in to Windows.")
    else:
        lnk.unlink(missing_ok=True)
        say("Autostart is off.")
    return 0


def desktop_shortcut() -> None:
    if WIN:
        desk = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        if desk.exists():
            shortcut(desk / "Virtual Room Bot.lnk", str(ROOT / "VirtualRoomBot.cmd"), "", ROOT, "Manage Virtual Room Bot")


# ---------------------------------------------------------------- first run
def configure() -> int:
    """After Setup installed the runtimes: settings, token, start, invite. Safe to run again."""
    ensure_env()
    DATA.mkdir(exist_ok=True)
    env = read_env()
    app_id = None
    if env.get("DISCORD_TOKEN"):
        try:
            app_id = str(discord_get("/oauth2/applications/@me", env["DISCORD_TOKEN"]).get("id"))
            say("✅ A working bot token is already set.")
        except urllib.error.URLError:
            say("The saved token doesn't work any more.")
    if not app_id:
        say("\nStep 1 · Create your bot (once):")
        say("  1. Open https://discord.com/developers/applications → New Application → give it a name.")
        say("  2. Bot page: turn ON 'Server Members Intent', click Save Changes.")
        say("  3. Bot page: click Reset Token and copy the token.\n")
        if input("Open the Developer Portal in your browser now? [Y/n] ").strip().lower() in ("", "y", "yes"):
            webbrowser.open("https://discord.com/developers/applications")
        app_id = token_flow()
        if not app_id:
            say("No token entered. Run Setup again when you have it.")
            return 1
    stop(quiet=True)
    start()
    url = invite_url(app_id)
    (DATA / "invite-url.txt").write_text(url)
    say("\nStep 2 · Add the bot to your server:")
    say(f"  {url}")
    webbrowser.open(url)
    say("  Pick your server, click Authorize. Then in Server Settings → Roles, drag the bot's role above the roles")
    say("  it should manage.")
    say("\nStep 3 · In your Discord server, type /setup and follow the five short pages.\n")
    if WIN and input("Start the bot automatically when you sign in to Windows? [Y/n] ").strip().lower() in ("", "y", "yes"):
        autostart(True)
    desktop_shortcut()
    say("\nDone! Manage the bot any time with the 'Virtual Room Bot' shortcut on your desktop.")
    say("Remember: the bot is online only while this computer is on.")
    return 0


# ---------------------------------------------------------------- updates (public GitHub releases, verified)
def http_json(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers={**UA, "Accept": "application/vnd.github+json"}),
                                timeout=30) as r:
        return json.loads(r.read())


def download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except ValueError:
        return (0,)


def current_version() -> str:
    return (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "0.0.0"


def latest_release() -> dict:
    repo = read_env().get("UPDATE_REPO") or REPO
    rel = http_json(f"https://api.github.com/repos/{repo}/releases/latest")   # stable releases only, never drafts/pre
    zips = [a for a in rel.get("assets", []) if a["name"].endswith(".zip")]
    sums = [a for a in rel.get("assets", []) if a["name"].endswith(".sha256")]
    if not zips or not sums:
        raise RuntimeError("the latest release has no ZIP + checksum")
    return {"version": rel["tag_name"].lstrip("v"), "zip": zips[0]["browser_download_url"],
            "sha": sums[0]["browser_download_url"], "notes": rel.get("body", "")}


def code_items(folder: Path) -> list[Path]:
    return [p for p in folder.iterdir() if p.name not in KEEP]


def swap_code(new_root: Path, backup_to: Path) -> None:
    backup_to.mkdir(parents=True, exist_ok=True)
    for p in code_items(ROOT):
        shutil.move(str(p), str(backup_to / p.name))
    for p in code_items(new_root):
        shutil.move(str(p), str(ROOT / p.name))


def healthy(seconds: int = 120) -> bool:
    return wait_state(seconds) in ("ready", "no_server")


def update(check_only: bool = False, yes: bool = False) -> int:
    cur = current_version()
    try:
        rel = latest_release()
    except Exception as e:  # noqa: BLE001
        say(f"Could not check for updates: {e}")
        return 1
    if version_tuple(rel["version"]) <= version_tuple(cur):
        say(f"✅ You have the latest version ({cur}).")
        return 0
    say(f"⬆️ Version {rel['version']} is available (you have {cur}).")
    if rel["notes"]:
        say(rel["notes"][:1500])
    if check_only:
        return 0
    if not yes and input("\nInstall it now? A backup is made first and it rolls back if anything fails. [y/N] ").strip().lower() not in ("y", "yes"):
        return 0
    work = DATA / "update"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    say("1/6 downloading…")
    z, s = work / "release.zip", work / "release.sha256"
    download(rel["zip"], z)
    download(rel["sha"], s)
    if sha256(z) != s.read_text().split()[0].strip().lower():
        say("❌ The download didn't match its checksum. Nothing was changed.")
        return 1
    with zipfile.ZipFile(z) as zf:
        zf.extractall(work / "x")
    tops = [p for p in (work / "x").iterdir() if p.is_dir()]
    new = tops[0] if len(tops) == 1 and not (work / "x" / "VERSION").exists() else work / "x"
    if not (new / "VERSION").exists() or not (new / "vrbot").is_dir():
        say("❌ The download doesn't look like a Virtual Room Bot release. Nothing was changed.")
        return 1
    if not (new / "installer" / "vrb.py").exists():
        say("❌ That release can't be installed by this Windows manager (it has no installer folder).")
        say("   Nothing was changed. Download it from the Releases page and follow its README instead.")
        return 1
    say("2/6 backing up the database…")
    db_backup = None
    if (DATA / "vrbot.db").exists():
        if backup() != 0:
            say("❌ Backup failed. Nothing was changed.")
            return 1
        db_backup = sorted((DATA / "backups").glob("manual-*.db"), key=lambda p: p.name)[-1]
    else:
        say("   (no database yet: nothing to back up)")
    say("3/6 stopping the bot…")
    stop(quiet=True)
    prev = ROOT / "previous" / cur
    shutil.rmtree(prev, ignore_errors=True)
    say("4/6 installing the new version…")
    swap_code(new, prev)
    (ROOT / "previous" / "last.json").write_text(json.dumps({"version": cur, "db_backup": str(db_backup) if db_backup else None}))
    r = subprocess.run([python_exe(), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r",
                        str(ROOT / "requirements.txt")], cwd=ROOT)
    if r.returncode == 0:
        say("5/6 starting and checking health…")
        start(quiet=True)
        if healthy():
            say(f"6/6 ✅ Updated to {rel['version']}.")
            shutil.rmtree(work, ignore_errors=True)
            return 0
    say("❌ The new version didn't come up healthy — rolling back.")
    return rollback(auto=True)


def rollback(auto: bool = False) -> int:
    """Back to the previous version's code AND its database backup (the update's migrations may have changed it)."""
    meta_f = ROOT / "previous" / "last.json"
    if not meta_f.exists():
        say("Nothing to roll back to.")
        return 1
    meta = json.loads(meta_f.read_text())
    prev = ROOT / "previous" / meta["version"]
    if not auto and input(f"Go back to version {meta['version']} (code and database)? [y/N] ").strip().lower() not in ("y", "yes"):
        return 0
    stop(quiet=True)
    failed = ROOT / "previous" / f"failed-{current_version()}"
    shutil.rmtree(failed, ignore_errors=True)
    swap_code(prev, failed)
    db = DATA / "vrbot.db"
    if meta.get("db_backup") and Path(meta["db_backup"]).exists():
        for side in ("-wal", "-shm"):
            Path(str(db) + side).unlink(missing_ok=True)
        shutil.copyfile(meta["db_backup"], db)
    subprocess.run([python_exe(), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r",
                    str(ROOT / "requirements.txt")], cwd=ROOT)
    start(quiet=True)
    ok = healthy()
    say(f"{'✅' if ok else '⚠️'} Back on version {current_version()}" + ("" if ok else " — check 'View logs'."))
    return 0 if ok else 1


# ---------------------------------------------------------------- menu
MENU = [("1", "Start bot", lambda: start()), ("2", "Stop bot", lambda: stop()),
        ("3", "Restart bot", lambda: (stop(quiet=True), start())[1]), ("4", "Status", status),
        ("5", "View logs", lambda: logs()), ("6", "Back up now", backup), ("7", "Check for updates", lambda: update()),
        ("8", "Invite link", invite),
        ("9", "Change bot token", lambda: (token_flow(), say("Restart the bot to use it."))),
        ("D", "Diagnostics report (for bug reports)", diagnostics),
        ("A", "Start with Windows: on", lambda: autostart(True)), ("B", "Start with Windows: off", lambda: autostart(False)),
        ("0", "Exit", None)]


def menu() -> int:
    while True:
        say("\n=== Virtual Room Bot ===")
        for k, label, _ in MENU:
            say(f"  {k}  {label}")
        try:
            choice = input("Choose: ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            return 0
        for k, _, fn in MENU:
            if choice == k:
                if fn is None:
                    return 0
                try:
                    fn()
                except KeyboardInterrupt:
                    say("\nCancelled.")
                except Exception as e:  # noqa: BLE001
                    say(f"⚠️ {type(e).__name__}: {e}")
                break


def main(argv: list[str]) -> int:
    if WIN:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    cmd = argv[1] if len(argv) > 1 else "menu"
    quiet = "--quiet" in argv
    table = {"menu": menu, "configure": configure, "start": lambda: start(quiet), "stop": lambda: stop(quiet),
             "restart": lambda: (stop(True), start(quiet))[1], "status": status,
             "logs": lambda: logs(int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 40), "backup": backup,
             "update": lambda: update("--check" in argv, "--yes" in argv), "rollback": lambda: rollback("--yes" in argv),
             "autostart": lambda: autostart(len(argv) > 2 and argv[2] == "on"),
             "token": lambda: 0 if token_flow() else 1, "invite": invite, "supervise": supervise,
             "diagnostics": diagnostics}
    if cmd not in table:
        say(__doc__)
        return 2
    return table[cmd]() or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
