#!/usr/bin/env python3
import argparse
import atexit
import json
import os
import random
import shutil
import signal
import socket
import string
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4

CLEANUP_FILES = []
CLEANUP_PIDS = []


def cleanup():
    for pid in CLEANUP_PIDS:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.1)
                except ProcessLookupError:
                    break
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    for path in CLEANUP_FILES:
        try:
            os.unlink(path)
        except OSError:
            pass


atexit.register(cleanup)


def find_discord_ipc():
    candidates = []

    if env := os.environ.get("DISCORD_IPC_PATH"):
        candidates.append(env)

    if xdg := os.environ.get("XDG_RUNTIME_DIR"):
        candidates.extend([
            os.path.join(xdg, f"discord-ipc-{i}")
            for i in range(10)
        ])
        candidates.extend([
            os.path.join(xdg, "app", "com.discordapp.Discord", f"discord-ipc-{i}")
            for i in range(10)
        ])
        candidates.extend([
            os.path.join(xdg, "snap.discord", f"discord-ipc-{i}")
            for i in range(10)
        ])

    for var in ("TMPDIR", "TMP", "TEMP"):
        if d := os.environ.get(var):
            dirname = d.rstrip("/")
            candidates.extend([
                os.path.join(dirname, f"discord-ipc-{i}")
                for i in range(10)
            ])

    candidates.extend([
        os.path.join("/tmp", f"discord-ipc-{i}")
        for i in range(10)
    ])

    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def make_nonce():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=16))


def write_frame(sock, opcode, data):
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    header = struct.pack("<II", opcode, len(payload))
    sock.sendall(header + payload)


def read_frame(sock):
    header = b""
    while len(header) < 8:
        chunk = sock.recv(8 - len(header))
        if not chunk:
            raise ConnectionError("Connection closed")
        header += chunk
    opcode, length = struct.unpack("<II", header)
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            raise ConnectionError("Connection closed")
        payload += chunk
    return opcode, payload.decode("utf-8")


def can_execute_in(dirpath):
    try:
        probe = os.path.join(dirpath, f".poof_probe_{os.getpid()}")
        with open(probe, "w") as f:
            f.write("#!/bin/sh\nexit 0")
        os.chmod(probe, 0o755)
        ret = os.system(probe + " >/dev/null 2>&1") == 0
        os.unlink(probe)
        return ret
    except OSError:
        return False


def spawn_fake_process_bin(game_name):
    tmpdir = tempfile.gettempdir()
    if not can_execute_in(tmpdir):
        return None

    fake_path = os.path.join(tmpdir, game_name)
    SLEEP_BIN = shutil.which("sleep") or "/bin/sleep"
    shutil.copy2(SLEEP_BIN, fake_path)
    os.chmod(fake_path, 0o755)
    CLEANUP_FILES.append(fake_path)

    proc = subprocess.Popen(
        [fake_path, "86400"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    CLEANUP_PIDS.append(proc.pid)
    return proc.pid


def spawn_fake_process_prctl(game_name):
    pid = os.fork()
    if pid == 0:
        os.setsid()
        libc = __import__("ctypes").CDLL("libc.so.6")
        libc.prctl(15, game_name.encode(), 0, 0, 0)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        while True:
            time.sleep(3600)
    time.sleep(0.1)
    CLEANUP_PIDS.append(pid)
    return pid


def spawn_fake_process(game_name, method="auto"):
    if method == "bin":
        pid = spawn_fake_process_bin(game_name)
        if pid:
            return pid, "bin (copied executable)"
        print("[!] Binary copy method failed, falling back to prctl")
        pid = spawn_fake_process_prctl(game_name)
        return pid, "prctl (fallback)"

    if method == "prctl":
        pid = spawn_fake_process_prctl(game_name)
        return pid, "prctl"

    pid = spawn_fake_process_bin(game_name)
    if pid:
        return pid, "bin (copied executable)"

    print("[!] /tmp appears noexec, falling back to prctl method")
    pid = spawn_fake_process_prctl(game_name)
    return pid, "prctl (fallback)"


# ── Quest API(Method) ──

QUEST_UA = (
    "Discord/1.0.9180 Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 Electron/28.2.10"
)


class QuestAPI:
    BASE = "https://discord.com/api/v9"

    def __init__(self, token):
        self.headers = {
            "Authorization": token,
            "Content-Type": "application/json",
            "User-Agent": QUEST_UA,
            "Origin": "https://discord.com",
        }

    def _request(self, method, path, body=None):
        url = self.BASE + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = ": " + e.read().decode()[:200]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code} {method} {path}{detail}") from e

    def get_quests(self):
        return self._request("GET", "/quests/@me")

    def send_heartbeat(self, quest_id, stream_key, terminal=False):
        return self._request("POST", f"/quests/{quest_id}/heartbeat", {
            "stream_key": stream_key,
            "terminal": terminal,
        })

    def send_video_progress(self, quest_id, timestamp):
        return self._request("POST", f"/quests/{quest_id}/video-progress", {
            "timestamp": timestamp,
        })


# ── caching token ───

CONFIG_DIR = os.path.expanduser("~/.config/poof")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token")


def load_cached_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return None


def cache_token(token):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        f.write(token.strip())
    os.chmod(TOKEN_FILE, 0o600)
    print(f"[+] Token cached to {TOKEN_FILE}")


def resolve_token(args_token):
    if args_token:
        return args_token
    cached = load_cached_token()
    if cached:
        return cached
    return None


def print_token_instructions():
    print("""[!] Discord auth token required.

To get your token:
  1. Open Discord and press Ctrl+Shift+I (DevTools) -- To open developer tools you need to have a client mod like vencord or equicord.
  2. Go to the Application tab in the top menu.
  3. On the left sidebar, expand Local Storage and click on https://discord.com.
  4. Refresh the page (Ctrl + R or Cmd + R).
  5. In the search/filter box, type token.
  6. Your token will appear as an alphanumeric string under the "Value" column.
  7. Run: poof token "PASTE_HERE\"""")


# ── handling quest completion ──

SUPPORTED_TASKS = {"WATCH_VIDEO", "PLAY_ON_DESKTOP"}


def fmt_dur(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s" if m else f"{s}s"


def get_progress(quest):
    config = quest.get("config", {})
    status = quest.get("user_status") or {}
    if status.get("completed_at"):
        return None  # already complete
    if not status.get("enrolled_at"):
        return -1  # not enrolled

    task_config = config.get("task_config", config.get("task_config_v2", {}))
    tasks = task_config.get("tasks", {})
    task_name = next((t for t in tasks if t in SUPPORTED_TASKS), None)
    if not task_name:
        return -2  # unsupported

    target = tasks[task_name].get("target", 0)
    if config.get("config_version") == 1:
        current = status.get("stream_progress_seconds", 0)
    else:
        current = status.get("progress", {}).get(task_name, {}).get("value", 0)
    return current, target, task_name


def handle_quests(api):
    try:
        data = api.get_quests()
    except RuntimeError as e:
        print(f"[-] {e}")
        sys.exit(1)

    quests = data.get("quests", [])
    if not quests:
        print("[*] No quests found.")
        return

    print()
    print(f"{'Quest Name':<36} {'Game':<18} {'Progress':<16} {'Quest ID':<22} Status")
    print("-" * 108)
    for q in quests:
        cfg = q.get("config", {})
        qid = q.get("id", "?")
        name = cfg.get("messages", {}).get("quest_name", qid)[:34]
        game = cfg.get("messages", {}).get("game_title", "?")[:16]
        tasks = cfg.get("task_config", cfg.get("task_config_v2", {})).get("tasks", {})
        prog = get_progress(q)
        if prog is None:
            pstr = "COMPLETED"
            icon = "✅"
        elif prog == -1:
            pstr = "Not enrolled"
            icon = "⏸"
        elif prog == -2:
            pstr = "Unsupported"
            icon = "⏭"
        else:
            cur, tgt, _ = prog
            pstr = f"{fmt_dur(cur)} / {fmt_dur(tgt)}"
            icon = "⏳" if cur < tgt else "✅"
        print(f"{name:<36} {game:<18} {pstr:<16} {qid:<22} {icon}")


def handle_complete(api, quest_id=None):
    try:
        data = api.get_quests()
    except RuntimeError as e:
        print(f"[-] {e}")
        sys.exit(1)

    quests = data.get("quests", [])

    if quest_id:
        candidates = [q for q in quests if q.get("id") == quest_id]
        if not candidates:
            print(f"[-] Quest '{quest_id}' not found.")
            sys.exit(1)
        _complete_quests(api, candidates)
        return

    active = []
    for q in quests:
        prog = get_progress(q)
        if prog is None or prog == -1 or prog == -2:
            continue
        cur, tgt, _ = prog
        if cur < tgt:
            active.append(q)

    if not active:
        print("[*] No active quests to complete.")
        return

    print(f"[*] Found {len(active)} active quest(s)")
    _complete_quests(api, active)


def _complete_quests(api, quests):
    for q in quests:
        cfg = q.get("config", {})
        messages = cfg.get("messages", {})
        name = messages.get("quest_name", q.get("id", "?"))
        tasks = cfg.get("task_config", cfg.get("task_config_v2", {})).get("tasks", {})
        task_type = next(iter(tasks), None)

        print(f"\n{'='*60}")
        print(f"  {name}  [{task_type}]")

        if task_type == "WATCH_VIDEO":
            _complete_video(api, q)
        elif task_type == "PLAY_ON_DESKTOP":
            _complete_play(api, q)
        else:
            print(f"  ⏭ Skipped (unsupported type \"{task_type}\")")


def _complete_video(api, quest):
    qid = quest["id"]
    cfg = quest["config"]
    tasks = cfg.get("task_config", cfg.get("task_config_v2", {})).get("tasks", {})
    target = tasks["WATCH_VIDEO"]["target"]
    prog = get_progress(quest)
    cur = prog[0] if prog and isinstance(prog, tuple) else 0

    print(f"  Target: {fmt_dur(target)}, Current: {fmt_dur(cur)}")
    if cur >= target:
        print("  ✅ Already completed!")
        return True

    enrolled_at = quest.get("user_status", {}).get("enrolled_at")
    enrolled_ts = (
        _parse_iso(enrolled_at) if enrolled_at else time.time()
    )

    cancelled = False
    def on_sigint(*_):
        nonlocal cancelled
        cancelled = True
    orig = signal.signal(signal.SIGINT, on_sigint)

    try:
        while cur < target and not cancelled:
            max_allowed = int(time.time() - enrolled_ts) + 10
            if max_allowed - cur >= 7:
                ts = min(target, cur + 7 + random.random())
                try:
                    api.send_video_progress(qid, ts)
                    cur = ts
                    print(f"  \rProgress: {fmt_dur(cur)} / {fmt_dur(target)}", end="")
                except RuntimeError as e:
                    print(f"\n  ⚠️  {e}")
                    time.sleep(5)
            if not cancelled:
                time.sleep(1)
    finally:
        signal.signal(signal.SIGINT, orig)

    if cancelled:
        print("\n  ⏹  Cancelled.")
        return False
    print()
    print("  ✅ Completed!")
    return True


def _complete_play(api, quest):
    qid = quest["id"]
    cfg = quest["config"]
    tasks = cfg.get("task_config", cfg.get("task_config_v2", {})).get("tasks", {})
    target = tasks["PLAY_ON_DESKTOP"]["target"]

    prog = get_progress(quest)
    cur = prog[0] if prog and isinstance(prog, tuple) else 0
    ver_1 = cfg.get("config_version") == 1

    print(f"  Target: {fmt_dur(target)}, Current: {fmt_dur(cur)}")
    if cur >= target:
        print("  ✅ Already completed!")
        return True

    stream_key = f"call:{qid}:1"
    print(f"  Stream key: {stream_key}")
    print(f"  Heartbeating every 2s (press Ctrl+C to stop)")

    cancelled = False
    def on_sigint(*_):
        nonlocal cancelled
        cancelled = True
    orig = signal.signal(signal.SIGINT, on_sigint)

    try:
        while cur < target and not cancelled:
            try:
                resp = api.send_heartbeat(qid, stream_key)
                if ver_1:
                    cur = resp.get("stream_progress_seconds", cur)
                else:
                    cur = resp.get("progress", {}).get("PLAY_ON_DESKTOP", {}).get("value", cur)
                if resp.get("completed_at"):
                    cur = target
            except RuntimeError as e:
                print(f"\n  ⚠️  Heartbeat: {e}")

            print(f"  \rProgress: {fmt_dur(cur)} / {fmt_dur(target)}", end="")
            sys.stdout.flush()

            if cur >= target:
                try:
                    api.send_heartbeat(qid, stream_key, terminal=True)
                except RuntimeError:
                    pass
                print()
                print("  ✅ Completed!")
                return True

            if not cancelled:
                time.sleep(2)
    finally:
        signal.signal(signal.SIGINT, orig)

    if cancelled:
        try:
            api.send_heartbeat(qid, stream_key, terminal=True)
            print("\n  ⏹  Cancelled — sent terminal heartbeat.")
        except RuntimeError:
            print("\n  ⏹  Cancelled.")
    return False

    if not app_id:
        print("  ⚠️  No application id in quest config")
        return False

    stream_key = f"game:{app_id}"
    print(f"  Stream key: {stream_key}")
    print(f"  Heartbeating every 20s (press Ctrl+C to stop)")

    while cur < target:
        try:
            api.send_heartbeat(qid, stream_key)
        except RuntimeError as e:
            print(f"\n  ⚠️  Heartbeat: {e}")

        for _ in range(20):
            time.sleep(1)
            try:
                data = api.get_quests()
                for q in data.get("quests", []):
                    if q.get("id") != qid:
                        continue
                    s = q.get("user_status") or {}
                    if s.get("completed_at"):
                        cur = target
                        break
                    if ver_1:
                        cur = s.get("stream_progress_seconds", cur)
                    else:
                        cur = s.get("progress", {}).get("PLAY_ON_DESKTOP", {}).get("value", cur)
                    break
            except RuntimeError:
                pass

        print(f"  \rProgress: {fmt_dur(cur)} / {fmt_dur(target)}", end="")
        sys.stdout.flush()

        if cur >= target:
            try:
                api.send_heartbeat(qid, stream_key, terminal=True)
            except RuntimeError:
                pass
            print()
            print("  ✅ Completed!")
            return True

    print()
    return True


def _parse_iso(s):
    s = s.replace("Z", "+00:00")
    try:
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return time.time()


# ── spoof + RPC ──

def do_spoof(game_name, app_id, method, no_process):
    fake_pid = None
    method_str = "none"

    if not no_process:
        fake_pid, method_str = spawn_fake_process(game_name, method)
        print(f"[+] Spawned fake process [{method_str}]")
        print(f"    Name: {game_name}")
        print(f"    PID:  {fake_pid}")
        time.sleep(0.3)
    else:
        print("[*] Skipping fake process (--no-process)")

    sock = None

    if app_id:
        ipc_path = find_discord_ipc()
        if not ipc_path:
            print("[!] Discord IPC socket not found; RPC activity won't be set.")
            print("    Process detection alone should still work for Discord quests.")
        else:
            print(f"[+] Found Discord IPC: {ipc_path}")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(ipc_path)
                print("[+] Connected to Discord IPC")
                write_frame(sock, OP_HANDSHAKE, {"v": 1, "client_id": app_id})
                opcode, raw = read_frame(sock)
                if opcode != OP_FRAME:
                    print(f"[-] Handshake failed (opcode {opcode})")
                    sock.close()
                    sock = None
                else:
                    ready = json.loads(raw)
                    user = ready.get("data", {}).get("user", {})
                    username = user.get("username", "?")
                    discriminator = user.get("discriminator", "")
                    tag = f"{username}#{discriminator}" if discriminator else username
                    print(f"[+] Authenticated as {tag}")

                    pid_for_rpc = fake_pid if fake_pid else os.getpid()
                    activity = {
                        "cmd": "SET_ACTIVITY",
                        "args": {
                            "pid": pid_for_rpc,
                            "activity": {
                                "state": "In-Game",
                                "details": "Playing",
                                "timestamps": {"start": int(time.time() * 1000)},
                                "instance": True,
                            },
                        },
                        "nonce": make_nonce(),
                    }
                    write_frame(sock, OP_FRAME, activity)
                    opcode, raw = read_frame(sock)
                    if opcode == OP_FRAME:
                        resp = json.loads(raw)
                        if resp.get("cmd") == "SET_ACTIVITY" and resp.get("evt") is None:
                            print("[+] Activity set via RPC!")
                        elif resp.get("evt") == "ERROR":
                            print(f"[-] SET_ACTIVITY rejected: {resp.get('data', {}).get('message', raw)}")
                        else:
                            print("[+] SET_ACTIVITY acknowledged")
            except Exception as e:
                print(f"[!] IPC connection failed: {e}")
                if sock:
                    sock.close()
                    sock = None
    else:
        print("[*] No --app-id provided; skipping RPC. Process detection only.")

    return fake_pid, sock


def run_spoof_loop(sock):
    print("[*] Poof is running. Press Ctrl+C to stop.")
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if sock:
        sock.settimeout(5.0)

    while running:
        if sock:
            try:
                opcode, raw = read_frame(sock)
                if opcode == OP_PING:
                    data = json.loads(raw) if raw.strip() else {}
                    write_frame(sock, OP_PONG, data)
                elif opcode == OP_CLOSE:
                    print("[*] Discord closed the connection")
                    break
            except socket.timeout:
                continue
            except (ConnectionError, EOFError, OSError) as e:
                print(f"[*] IPC connection lost: {e}")
                break
        else:
            time.sleep(0.5)

    if sock:
        sock.close()
    print("[*] Poof stopped.")



def main():
    prog = sys.argv[0]
    known_commands = {"spoof", "quests", "complete", "token"}

    # Determine command: first non-flag argument decides
    cmd = "spoof"
    cmd_args = sys.argv[1:]
    if cmd_args and cmd_args[0] in known_commands:
        cmd = cmd_args[0]
        cmd_args = cmd_args[1:]

    if cmd == "spoof":
        p = argparse.ArgumentParser(
            prog=prog,
            description="Poof — Complete Discord quests by creating a lightweight process or using the Quest API",
            epilog=(
                "Examples:\n"
                "  <--Fake process method-->\n"
                "  poof 'genshinimpact.exe' #To spawn a fake process\n"
                "  <--Quest API method-->\n"
                "  poof token 'PASTE HERE' #cache token\n"
                "  poof quests #To list quests (requires Discord auth token)\n"
                "  poof complete #To complete the accepted quests\n"
                "  poof complete --quest-id 150299293020393 #To complete a specific quest using the quest-id\n"
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p.add_argument("game_name", help="Game executable name (e.g. genshinimpact.exe)")
        p.add_argument("--app-id", help="Discord Application ID for RPC")
        p.add_argument("--method", choices=["auto", "bin", "prctl"], default="auto")
        p.add_argument("--no-process", action="store_true", help="Skip fake process")
        p.add_argument("--no-spoof", action="store_true", help="Skip process spoofing, API only")
        p.add_argument("--token", help="Discord auth token (also completes matching quests)")
        p.add_argument("--cache-token", action="store_true",
                        help="Save --token to ~/.config/poof/token")
        a = p.parse_args(cmd_args)

        if a.cache_token and a.token:
            cache_token(a.token)

        if not a.no_spoof:
            do_spoof(a.game_name, a.app_id, a.method, a.no_process)

        if a.token:
            api = QuestAPI(a.token)
            print()
            handle_complete(api)
        elif not a.no_spoof:
            run_spoof_loop(None)
        else:
            print("[*] Use --token to also complete quests, or run: poof complete")

    elif cmd == "quests":
        p = argparse.ArgumentParser(prog=prog + " quests", description="List enrolled quests")
        p.add_argument("--token", help="Discord auth token")
        a = p.parse_args(cmd_args)
        token = resolve_token(a.token)
        if not token:
            print_token_instructions()
            sys.exit(1)
        api = QuestAPI(token)
        handle_quests(api)
        if not a.token:
            print(f"\n[*] Token loaded from {TOKEN_FILE}")

    elif cmd == "complete":
        p = argparse.ArgumentParser(prog=prog + " complete", description="Complete active quests")
        p.add_argument("--token", help="Discord auth token")
        p.add_argument("--quest-id", help="Complete a specific quest by ID")
        a = p.parse_args(cmd_args)
        token = resolve_token(a.token)
        if not token:
            print_token_instructions()
            sys.exit(1)
        api = QuestAPI(token)
        handle_complete(api, a.quest_id)
        if not a.token:
            print(f"\n[*] Token loaded from {TOKEN_FILE}")

    elif cmd == "token":
        p = argparse.ArgumentParser(prog=prog + " token", description="Manage cached auth token")
        p.add_argument("value", nargs="?", help="Token value to cache")
        a = p.parse_args(cmd_args)
        if a.value:
            cache_token(a.value)
            print("[+] Token cached. You can now run poof quests/complete without --token.")
        else:
            print_token_instructions()

    else:
        print(f"Unknown command: {cmd}")
        print(f"Usage: {prog} [spoof|quests|complete|token] [...]")
        sys.exit(1)


if __name__ == "__main__":
    main()
