from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
LOGS = ROOT / "logs"
LIVEKIT_DIR = ROOT / "tools" / "livekit"
LIVEKIT_EXE = LIVEKIT_DIR / "livekit-server.exe"
LIVEKIT_CONFIG = LIVEKIT_DIR / "voicemate-livekit.yaml"
PYTHON = BACKEND / ".venv" / "Scripts" / "python.exe"


def load_env() -> dict[str, str]:
    env = os.environ.copy()
    env_file = BACKEND / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    env.setdefault("LIVEKIT_HOST", "192.168.10.233")
    env.setdefault("LIVEKIT_PORT", "7880")
    env.setdefault("LIVEKIT_URL", f"ws://{env['LIVEKIT_HOST']}:{env['LIVEKIT_PORT']}")
    return env


def list_voice_mate_processes() -> list[tuple[int, str, str]]:
    current_pid = os.getpid()
    rows: list[tuple[int, str, str]] = []
    root = str(ROOT).replace("'", "''")
    ps = f"""
$root = '{root}'
$selfPid = {current_pid}
Get-CimInstance Win32_Process |
  Where-Object {{
    $_.ProcessId -ne $selfPid -and
    $_.CommandLine -and
    $_.CommandLine.Contains($root) -and
    ($_.CommandLine -match 'server\\.py' -or $_.CommandLine -match 'livekit_agent\\.py' -or $_.Name -eq 'livekit-server.exe')
  }} |
  ForEach-Object {{ "$($_.ProcessId)`t$($_.Name)`t$($_.CommandLine)" }}
"""
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:
        return rows

    for line in completed.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        pid_text, name, command = parts
        try:
            pid = int(pid_text)
        except Exception:
            continue
        if pid == current_pid:
            continue
        rows.append((pid, name, command))
    return rows


def stop_old_processes() -> None:
    old_processes = list_voice_mate_processes()
    for pid, _, _ in old_processes:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    time.sleep(2)


def start_process(name: str, args: list[str], cwd: Path, env: dict[str, str], stdout_name: str, stderr_name: str) -> None:
    stdout = open(LOGS / stdout_name, "wb")
    stderr = open(LOGS / stderr_name, "wb")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(args, cwd=str(cwd), env=env, stdout=stdout, stderr=stderr, creationflags=flags)
    print(f"started {name}")


def main() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    if not PYTHON.exists():
        print(f"missing venv python: {PYTHON}", file=sys.stderr)
        return 1
    if not LIVEKIT_EXE.exists():
        print(f"missing livekit server: {LIVEKIT_EXE}", file=sys.stderr)
        return 1

    env = load_env()
    stop_old_processes()

    start_process("api", [str(PYTHON), str(BACKEND / "server.py")], BACKEND, env, "api.out.log", "api.err.log")
    time.sleep(2)
    start_process(
        "livekit",
        [str(LIVEKIT_EXE), "--config", str(LIVEKIT_CONFIG), "--node-ip", env["LIVEKIT_HOST"]],
        LIVEKIT_DIR,
        env,
        "livekit.out.log",
        "livekit.err.log",
    )
    time.sleep(2)
    start_process("agent", [str(PYTHON), str(BACKEND / "livekit_agent.py"), "start"], BACKEND, env, "agent.out.log", "agent.err.log")
    time.sleep(5)

    print("running VoiceMate processes:")
    for pid, name, command in list_voice_mate_processes():
        print(f"{pid} {name} {command}")
    print("health: http://127.0.0.1:8000/v1/health")
    print(f"livekit: ws://{env['LIVEKIT_HOST']}:{env['LIVEKIT_PORT']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
