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


def main():
    parser = argparse.ArgumentParser(
        description="Poof — Spoof game processes for Discord quests by creating a lightweight process that mimics a target game's executable.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  poof.py 'genshinimpact.exe'\n"
            "\n"
            "Game process names are matched by Discord's detectable-games database.\n"
            "See https://github.com/LoneDestroyer/Discord-Detectable-Apps/blob/main/detectable_apps.txt for a list.\n"
        ),
    )
    parser.add_argument("game_name", help="Game executable name (e.g. genshinimpact.exe)")
    parser.add_argument("--app-id", help="Discord Application ID (required for RPC activity setting; process detection works without it)")
    parser.add_argument("--method", choices=["auto", "bin", "prctl"], default="auto",
                        help="Fake process method: 'bin' copies a sleep binary; 'prctl' renames via syscall")
    parser.add_argument("--no-process", action="store_true", help="Skip fake process (IPC only)")
    args = parser.parse_args()

    game_name = args.game_name
    app_id = args.app_id

    fake_pid = None
    method_str = "none"
    if not args.no_process:
        fake_pid, method_str = spawn_fake_process(game_name, args.method)
        print(f"[+] Spawned fake process [{method_str}]")
        print(f"    Name: {game_name}")
        print(f"    PID:  {fake_pid}")
        time.sleep(0.3)
    else:
        print("[*] Skipping fake process (--no-process)")

    have_rpc = False
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
                    have_rpc = True
            except Exception as e:
                print(f"[!] IPC connection failed: {e}")
                if sock:
                    sock.close()
                    sock = None
    else:
        print("[*] No --app-id provided; skipping RPC. Process detection only.")

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


if __name__ == "__main__":
    main()
