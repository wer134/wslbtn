#!/usr/bin/env python3
"""wslbtn — WSL 터미널 출력에 클릭 가능한 버튼을 찍는다.

    wslbtn install                          # Windows에 wslbtn:// 핸들러 등록 (최초 1회)
    wslbtn btn "재시작" -- systemctl restart app
    wslbtn btn "로그" -s "tail -n 50 app.log | less"
    wslbtn uninstall

버튼(OSC 8 링크)에는 랜덤 토큰만 들어가고, 실제 명령은 WSL 쪽 상태 디렉토리에
저장된다. 클릭하면 Windows가 창 없는 런처(wslbtn-launch.exe)를 실행하고, 런처가 URL을
검사한 뒤 `wsl.exe --exec wslbtn.py fire <url>`을 콘솔 창 없이 실행한다.
fire가 토큰을 확인한 뒤 명령을 실행해 출력을 버튼이 찍혔던 터미널에 쓴다.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCHEME = "wslbtn"
TOKEN_RE = re.compile(rf"^{SCHEME}://fire/([A-Za-z0-9_-]{{22}})/?$")
DEFAULT_TTL = 24 * 3600

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "wslbtn"
BUTTONS_DIR = STATE_DIR / "buttons"
LOG_FILE = STATE_DIR / "fire.log"


# ---------------------------------------------------------------- 저장소

def _ensure_dirs():
    BUTTONS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    os.chmod(BUTTONS_DIR, 0o700)


def save_button(button):
    """버튼을 저장하고 토큰을 돌려준다. env가 들어가므로 0600으로 쓴다."""
    _ensure_dirs()
    token = secrets.token_urlsafe(16)  # 22자, 128비트
    path = BUTTONS_DIR / f"{token}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(button, f)
    return token


def take_button(token):
    """토큰에 해당하는 버튼을 읽는다. 만료됐거나 없으면 None.

    --once 버튼은 rename으로 선점해서 두 번 클릭해도 한 번만 실행되게 한다.
    """
    path = BUTTONS_DIR / f"{token}.json"
    try:
        button = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if time.time() > button["expires"]:
        path.unlink(missing_ok=True)
        return None
    if button.get("once"):
        claimed = path.with_suffix(".used")
        try:
            os.rename(path, claimed)
        except FileNotFoundError:
            return None  # 다른 클릭이 먼저 가져감
        claimed.unlink(missing_ok=True)
    return button


def prune_expired():
    if not BUTTONS_DIR.exists():
        return
    now = time.time()
    for p in BUTTONS_DIR.glob("*.json"):
        try:
            if json.loads(p.read_text())["expires"] < now:
                p.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError):
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------- btn

def current_tty():
    for fd in (1, 2, 0):
        try:
            return os.ttyname(fd)
        except OSError:
            continue
    return None


def osc8(url, text):
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def cmd_btn(args):
    if bool(args.shell) == bool(args.argv):
        sys.exit("wslbtn btn: `-- 명령 ...` 또는 `-s \"셸 문자열\"` 중 하나를 지정하세요.")
    argv = ["bash", "-c", args.shell] if args.shell else args.argv
    prune_expired()
    token = save_button({
        "label": args.label,
        "argv": argv,
        "cwd": os.getcwd(),
        "env": dict(os.environ),
        "tty": current_tty(),
        "once": args.once,
        "expires": time.time() + args.ttl,
    })
    style = "\033[1;97;44m" if not args.once else "\033[1;97;41m"
    text = f"{style} {args.label} \033[0m"
    end = "" if args.no_newline else "\n"
    sys.stdout.write(osc8(f"{SCHEME}://fire/{token}", text) + end)


# ---------------------------------------------------------------- fire

def log(msg):
    _ensure_dirs()
    with open(LOG_FILE, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def cmd_fire(rest):
    # Windows가 넘겨준 URL. 인자가 정확히 하나가 아니면 조작된 URL로 보고 거부한다.
    if len(rest) != 1:
        return log(f"거부: 인자 {len(rest)}개 {rest!r}")
    m = TOKEN_RE.match(rest[0])
    if not m:
        return log(f"거부: 형식 불일치 {rest[0]!r}")
    button = take_button(m.group(1))
    if button is None:
        return log("거부: 없는 토큰이거나 만료됨")

    label, tty = button["label"], button["tty"]
    try:
        out = open(tty, "a") if tty else None
    except OSError:
        out = None  # 터미널이 닫혔음
    if out is None:
        out = open(LOG_FILE, "a")
        log(f"터미널 {tty} 없음, 출력은 로그로: [{label}]")

    with out:
        out.write(f"\r\n\033[36m── [{label}] $ {_display(button['argv'])}\033[0m\r\n")
        out.flush()
        start = time.monotonic()
        try:
            code = subprocess.run(button["argv"], cwd=button["cwd"], env=button["env"],
                                  stdin=subprocess.DEVNULL, stdout=out, stderr=out).returncode
        except OSError as e:
            out.write(f"\033[31m실행 실패: {e}\033[0m\r\n")
            code = None
        color = "32" if code == 0 else "31"
        out.write(f"\033[{color}m── [{label}] 종료 코드 {code} ({time.monotonic() - start:.1f}s)\033[0m\r\n")
    log(f"실행: [{label}] exit={code}")


def _display(argv):
    if argv[:2] == ["bash", "-c"] and len(argv) == 3:
        return argv[2]
    return " ".join(argv)


# ---------------------------------------------------------------- install

def _win(cmd):
    """Windows 명령 실행. 출력은 콘솔 코드페이지(한글 Windows면 CP949)."""
    return subprocess.run(cmd, capture_output=True, encoding="cp949", errors="replace", cwd="/mnt/c")


def _win_env(name):
    return _win(["cmd.exe", "/c", f"echo %{name}%"]).stdout.strip()


def _to_wsl(win_path):
    return subprocess.run(["wslpath", "-u", win_path], capture_output=True, text=True).stdout.strip()


def _reg_import(content):
    win_path = _win_env("TEMP") + "\\wslbtn-install.reg"
    wsl_path = _to_wsl(win_path)
    with open(wsl_path, "w", encoding="utf-16") as f:
        f.write(content)
    try:
        r = _win(["reg.exe", "import", win_path])
    finally:
        os.unlink(wsl_path)
    if r.returncode != 0:
        sys.exit(f"reg.exe 오류: {(r.stderr or r.stdout).strip()}")


def _reg_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# 창 없이(GUI 서브시스템) 떠서 URL을 검사한 뒤 wsl.exe를 콘솔 창 없이 실행하는 런처.
# wsl.exe를 레지스트리에 직접 걸면 클릭할 때마다 콘솔 창이 깜빡이고,
# wslg.exe의 `--`는 인자를 셸로 해석해서 URL로 명령을 끼워 넣을 수 있어 쓰지 않는다.
LAUNCHER_CS = r"""
using System.Diagnostics;
using System.Text.RegularExpressions;

static class WslbtnLaunch {
    const string Args = @"__ARGS__";
    static int Main(string[] a) {
        if (a.Length != 1 || !Regex.IsMatch(a[0], @"^__SCHEME__://fire/[A-Za-z0-9_-]{22}/?$")) return 2;
        var psi = new ProcessStartInfo(@"C:\Windows\System32\wsl.exe", Args + " " + a[0]);
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        // 콘솔이 없는 상태에서 표준 입출력이 없으면 wsl.exe가 실패하므로 파이프로 연결하고 버린다.
        // (fire의 출력은 버튼이 찍힌 터미널로 직접 간다)
        psi.RedirectStandardInput = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        // 런처가 먼저 끝나면 wsl.exe도 같이 종료되므로 끝날 때까지 기다린다 (창이 없어 보이지 않음)
        var p = Process.Start(psi);
        p.StandardInput.Close();
        p.BeginOutputReadLine();
        p.BeginErrorReadLine();
        p.WaitForExit();
        return p.ExitCode;
    }
}
"""
CSC = "C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319\\csc.exe"


def launcher_dir():
    return _win_env("LOCALAPPDATA") + "\\wslbtn"


def wsl_args():
    """런처가 wsl.exe에 넘길 인자 (URL 앞부분). URL은 런처가 검사한 뒤 뒤에 붙인다."""
    distro = os.environ.get("WSL_DISTRO_NAME")
    if not distro:
        sys.exit("WSL_DISTRO_NAME이 없음. WSL 안에서 실행하세요.")
    user = os.environ.get("USER") or os.getlogin()
    parts = [distro, user, sys.executable, str(Path(__file__).resolve())]
    # wsl.exe는 따옴표를 이름의 일부로 읽기도 해서(-d "Ubuntu" → 배포판 없음) 따옴표 없이 넘긴다.
    # 그래서 공백이나 특수문자가 있는 값은 거부한다.
    bad = [x for x in parts if not re.fullmatch(r"[A-Za-z0-9._/+-]+", x)]
    if bad:
        sys.exit(f"공백이나 특수문자가 있는 경로는 아직 지원하지 않음: {bad}")
    return f"-d {distro} -u {user} --exec {parts[2]} {parts[3]} fire"


def build_launcher():
    win_dir = launcher_dir()
    wsl_dir = _to_wsl(win_dir)
    os.makedirs(wsl_dir, exist_ok=True)
    src = os.path.join(wsl_dir, "wslbtn-launch.cs")
    with open(src, "w", encoding="utf-8") as f:
        f.write(LAUNCHER_CS.replace("__ARGS__", wsl_args()).replace("__SCHEME__", SCHEME))
    exe = win_dir + "\\wslbtn-launch.exe"
    r = _win([_to_wsl(CSC), "/nologo", "/target:winexe", f"/out:{exe}", win_dir + "\\wslbtn-launch.cs"])
    if r.returncode != 0:
        sys.exit(f"런처 컴파일 실패:\n{r.stdout}{r.stderr}")
    return exe


def cmd_install(_args):
    key = f"HKEY_CURRENT_USER\\Software\\Classes\\{SCHEME}"
    cmd = f'"{build_launcher()}" "%1"'
    _reg_import(f"""Windows Registry Editor Version 5.00

[{key}]
@={_reg_str(f"URL:{SCHEME}")}
"URL Protocol"=""

[{key}\\shell\\open\\command]
@={_reg_str(cmd)}
""")
    print(f"{SCHEME}:// 핸들러 등록 완료 (HKCU)\n  {cmd}")


def cmd_uninstall(_args):
    _reg_import(f"""Windows Registry Editor Version 5.00

[-HKEY_CURRENT_USER\\Software\\Classes\\{SCHEME}]
""")
    shutil.rmtree(_to_wsl(launcher_dir()), ignore_errors=True)
    shutil.rmtree(BUTTONS_DIR, ignore_errors=True)
    print(f"{SCHEME}:// 핸들러, 런처, 저장된 버튼 삭제 완료")


# ---------------------------------------------------------------- main

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # fire는 Windows가 부르는 경로라 argparse를 거치지 않는다 (옵션처럼 생긴 URL 방지)
    if argv[:1] == ["fire"]:
        return cmd_fire(argv[1:])

    ap = argparse.ArgumentParser(prog="wslbtn", description="WSL 터미널에 클릭 가능한 버튼을 찍는다.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("btn", help="버튼 출력")
    b.add_argument("label")
    b.add_argument("-s", "--shell", help="bash -c 로 실행할 문자열")
    b.add_argument("--once", action="store_true", help="한 번만 실행 가능한 버튼")
    b.add_argument("--ttl", type=int, default=DEFAULT_TTL, help="유효 시간(초), 기본 24시간")
    b.add_argument("-n", "--no-newline", action="store_true", help="줄바꿈 없이 출력")
    b.set_defaults(func=cmd_btn)

    sub.add_parser("install", help="Windows에 wslbtn:// 핸들러 등록").set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="핸들러 제거").set_defaults(func=cmd_uninstall)

    # `--` 뒤는 실행할 명령이라 argparse에 넘기지 않는다
    command = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    args = ap.parse_args(argv)
    args.argv = command
    args.func(args)


if __name__ == "__main__":
    main()
