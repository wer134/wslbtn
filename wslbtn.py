#!/usr/bin/env python3
"""wslbtn — WSL 터미널 출력에 클릭 가능한 버튼을 찍는다.

    wslbtn install                          # Windows에 wslbtn:// 핸들러 등록 (최초 1회)
    wslbtn btn "재시작" -- systemctl restart app
    wslbtn btn "로그" -s "tail -n 50 app.log | less"
    wslbtn menu                             # 이 터미널의 버튼을 그냥 클릭/숫자 키로 실행
    wslbtn uninstall

버튼(OSC 8 링크)에는 랜덤 토큰만 들어가고, 실제 명령은 WSL 쪽 상태 디렉토리에
저장된다. 클릭하면 Windows가 창 없는 런처(wslbtn-launch.exe)를 실행하고, 런처가 URL을
검사한 뒤 `wsl.exe --exec wslbtn.py fire <url>`을 콘솔 창 없이 실행한다.
fire가 토큰을 확인한 뒤 명령을 실행해 출력을 버튼이 찍혔던 터미널에 쓴다.
"""

import argparse
import fcntl
import json
import os
import re
import secrets
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
import unicodedata
from pathlib import Path

SCHEME = "wslbtn"
# '#' 뒤는 툴팁에 실행할 명령을 보여 주기 위한 부분이라 무시한다 (실행은 토큰으로만 결정)
TOKEN_RE = re.compile(rf"^{SCHEME}://fire/([A-Za-z0-9_-]{{22}})/?(?:#.*)?$", re.S)
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
        "sid": os.getsid(0),  # 터미널의 셸 (출력 후 프롬프트를 다시 그리게 할 대상)
        "once": args.once,
        "created": time.time(),
        "expires": time.time() + args.ttl,
    })
    style = "\033[1;97;44m" if not args.once else "\033[1;97;41m"
    text = f"{style} {args.label} \033[0m"
    end = "" if args.no_newline else "\n"
    url = f"{SCHEME}://fire/{token}#{tooltip_text(argv)}"
    sys.stdout.write(osc8(url, text) + end)


def tooltip_text(argv, limit=100):
    """마우스를 올렸을 때 Windows Terminal 툴팁에 보일 명령 (URL의 '#' 뒤).

    클릭하면 이 URL이 Windows 명령줄 인자로 넘어가므로, 인자를 쪼갤 수 있는 따옴표와
    역슬래시, 그리고 %, 제어 문자, 비ASCII 문자는 퍼센트 인코딩한다.
    """
    text = _display(argv)
    if len(text) > limit:
        text = text[:limit - 3] + "..."
    out = []
    for ch in text:
        if 0x20 <= ord(ch) < 0x7F and ch not in '"\\%':
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode())
    return "".join(out)


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
    to_tty = out is not None
    if out is None:
        out = open(LOG_FILE, "a")
        log(f"터미널 {tty} 없음, 출력은 로그로: [{label}]")

    with out:
        out.write("\r\n")
        code = run_button(button, out, stdin=subprocess.DEVNULL)
    if to_tty:
        redraw_prompt(tty, button.get("sid"))
    log(f"실행: [{label}] exit={code}")


SLOW = 1.0  # 이보다 오래 걸리면 성공해도 걸린 시간을 보여 준다


def run_button(button, out, stdin=None):
    """머리줄 → 명령 실행 → (실패했거나 오래 걸렸을 때만) 꼬리줄을 out에 쓴다.

    stdin=None이면 터미널 입력을 물려받는다.
    """
    label = button["label"]
    out.write(f"\033[1;36m▶ {label}\033[0m \033[2m{_display(button['argv'])}\033[0m\r\n")
    out.flush()
    start = time.monotonic()
    try:
        code = subprocess.run(button["argv"], cwd=button["cwd"], env=button["env"],
                              stdin=stdin, stdout=out, stderr=out).returncode
    except OSError as e:
        out.write(f"\033[31m실행 실패: {e}\033[0m\r\n")
        code = None
    elapsed = time.monotonic() - start
    if code != 0:
        out.write(f"\033[31m✗ {label} · 종료 코드 {code} ({elapsed:.1f}s)\033[0m\r\n")
    elif elapsed >= SLOW:
        out.write(f"\033[2;32m✓ {label} ({elapsed:.1f}s)\033[0m\r\n")
    out.flush()
    return code


def redraw_prompt(tty, sid):
    """셸이 프롬프트에서 대기 중이면 프롬프트(와 입력 중인 내용)를 다시 그리게 한다.

    bash(readline)는 SIGWINCH만으로는 다시 그리지 않고 화면 크기가 실제로 바뀌어야
    다시 그린다. 그래서 폭을 1칸 줄였다가 되돌린다 (크기가 바뀌면 커널이 SIGWINCH를
    보낸다). vim 같은 프로그램이 앞에서 돌고 있으면 건드리지 않는다.
    """
    if not sid:
        return
    try:
        # /proc/<pid>/stat: comm 뒤로 state ppid pgrp session tty_nr tpgid ...
        fields = Path(f"/proc/{sid}/stat").read_text().rsplit(")", 1)[1].split()
        tty_nr, tpgid = int(fields[4]), int(fields[5])
        if tty_nr != os.stat(tty).st_rdev or tpgid != sid:  # PID 재사용 / 다른 프로그램 실행 중
            return
        fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY)
    except (OSError, ValueError, IndexError):
        return
    try:
        size = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, xpix, ypix = struct.unpack("HHHH", size)
        if cols < 2:
            return
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols - 1, xpix, ypix))
        time.sleep(0.1)  # 셸이 첫 번째 크기 변경을 처리할 시간 (너무 빠르면 하나로 합쳐짐)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass
    finally:
        os.close(fd)


def _display(argv):
    if argv[:2] == ["bash", "-c"] and len(argv) == 3:
        return argv[2]
    return " ".join(argv)


# ---------------------------------------------------------------- menu
#
# Windows Terminal은 OSC 8 링크를 Ctrl+클릭으로만 연다 (설정으로 못 바꿈).
# 그래서 Neovim처럼 터미널 마우스 모드를 켜고 클릭 좌표를 직접 받아, 메뉴가 떠 있는
# 동안에는 그냥 클릭으로 버튼을 누를 수 있게 한다.

MOUSE_ON = "\033[?1000h\033[?1006h"   # 클릭 보고 + SGR 좌표 형식
MOUSE_OFF = "\033[?1000l\033[?1006l"
MOUSE_RE = re.compile(rb"\033\[<(\d+);(\d+);(\d+)([Mm])")
KEYS = "123456789"


def live_buttons(tty):
    """이 터미널에서 만든, 아직 유효한 버튼 (최근 것부터). 같은 라벨+명령은 최신 하나만."""
    if not BUTTONS_DIR.exists():
        return []
    now, seen, result = time.time(), set(), []
    found = []
    for p in BUTTONS_DIR.glob("*.json"):
        try:
            b = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if b.get("tty") == tty and b["expires"] > now:
            b["token"] = p.stem
            found.append(b)
    for b in sorted(found, key=lambda b: b.get("created", 0), reverse=True):
        key = (b["label"], tuple(b["argv"]))
        if key not in seen:
            seen.add(key)
            result.append(b)
    return result


def cell_width(text):
    """터미널에서 차지하는 칸 수 (한글 등 전각 문자는 2칸)."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def layout(buttons, cols):
    """버튼을 터미널 폭에 맞춰 줄 단위로 배치한다.

    반환: [[(시작 열, 끝 열, 버튼 인덱스, 표시 문자열), ...], ...]  (열은 1부터, 끝 포함)
    """
    lines, line, col = [], [], 1
    for i, b in enumerate(buttons):
        key = KEYS[i] if i < len(KEYS) else " "
        text = f" {key} {b['label']} "
        w = cell_width(text)
        if line and col + w - 1 > cols:
            lines.append(line)
            line, col = [], 1
        line.append((col, col + w - 1, i, text))
        col += w + 1  # 버튼 사이 한 칸
    if line:
        lines.append(line)
    return lines


def hit_test(lines, first_row, x, y):
    """클릭 좌표(1부터)에 있는 버튼 인덱스. 없으면 None."""
    i = y - first_row
    if 0 <= i < len(lines):
        for start, end, idx, _ in lines[i]:
            if start <= x <= end:
                return idx
    return None


class Menu:
    HINT = "\033[2m클릭 또는 숫자 키로 실행 · q/Esc 종료\033[0m"

    def __init__(self, tty):
        self.tty = tty
        self.fd = sys.stdin.fileno()
        self.out = sys.stdout
        self.lines, self.first_row, self.buttons = [], 1, []
        self.height = 1

    # -- 터미널 입출력

    def read_bytes(self, timeout):
        if select.select([self.fd], [], [], timeout)[0]:
            return os.read(self.fd, 1024)
        return b""

    def cursor_row(self):
        """DSR(ESC[6n)로 커서 행을 물어본다. 그 사이 들어온 다른 입력은 버퍼에 남긴다."""
        self.out.write("\033[6n")
        self.out.flush()
        buf, end = b"", time.monotonic() + 1.0
        while time.monotonic() < end:
            buf += self.read_bytes(0.05)
            m = re.search(rb"\033\[(\d+);(\d+)R", buf)
            if m:
                self.pending += buf[:m.start()] + buf[m.end():]
                return int(m.group(1))
        self.pending += buf
        return None

    def render(self):
        self.buttons = live_buttons(self.tty)
        cols = shutil.get_terminal_size().columns
        self.lines = layout(self.buttons, cols)
        out = []
        for line in self.lines:
            row = ""
            for _, _, idx, text in line:
                style = "\033[1;97;41m" if self.buttons[idx].get("once") else "\033[1;97;44m"
                row += f"{style}{text}\033[0m "
            out.append(row.rstrip())
        if not self.lines:
            out.append("\033[2m(이 터미널에서 만든 버튼이 없음)\033[0m")
        out.append(self.HINT)
        self.out.write("\r\n".join(out))
        self.out.flush()
        row = self.cursor_row()
        # 커서는 안내 줄(마지막 줄)에 있다
        self.first_row = (row - len(out) + 1) if row else -1000
        self.height = len(out)

    def clear(self):
        """메뉴가 차지한 줄을 지우고 커서를 메뉴 첫 줄 맨 앞에 둔다."""
        self.out.write(f"\r\033[{self.height - 1}A" if self.height > 1 else "\r")
        self.out.write("\033[J")
        self.out.flush()

    # -- 실행

    def fire(self, idx):
        button = self.buttons[idx]
        self.out.write(MOUSE_OFF)
        self.clear()
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        if not button.get("once") or take_button(button["token"]) is not None:
            run_button(button, self.out)  # 터미널 입력을 물려받아 대화형 명령도 동작
        self.set_mode()
        self.render()

    def set_mode(self):
        mode = termios.tcgetattr(self.fd)
        mode[3] &= ~(termios.ICANON | termios.ECHO)  # ISIG는 남겨 Ctrl+C 동작
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
        termios.tcsetattr(self.fd, termios.TCSADRAIN, mode)
        self.out.write(MOUSE_ON)
        self.out.flush()

    def handle(self, data):
        """입력을 처리한다. 종료해야 하면 False."""
        if os.environ.get("WSLBTN_DEBUG"):
            log(f"menu 입력 {data!r} / 버튼 첫 행 {self.first_row} / 배치 "
                f"{[[(a, b, i) for a, b, i, _ in line] for line in self.lines]}")
        while data:
            m = MOUSE_RE.match(data)
            if m:
                data = data[m.end():]
                b, x, y, kind = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
                # 왼쪽 버튼 누름. Shift(4)/Alt(8)/Ctrl(16) 비트는 무시해서 Ctrl+클릭도 받는다
                if kind == b"M" and b & ~(4 | 8 | 16) == 0:
                    idx = hit_test(self.lines, self.first_row, x, y)
                    if idx is not None:
                        self.fire(idx)
                continue
            if data[:1] == b"\033":
                if len(data) == 1:
                    return False  # Esc 단독
                data = data[1:]  # 모르는 이스케이프 시퀀스 앞부분은 버린다
                continue
            ch, data = data[:1].decode(errors="ignore"), data[1:]
            if ch in ("q", "Q"):
                return False
            if ch in KEYS and KEYS.index(ch) < len(self.buttons):
                self.fire(KEYS.index(ch))
        return True

    def run(self):
        self.saved = termios.tcgetattr(self.fd)
        self.pending = b""
        try:
            self.set_mode()
            self.render()
            while True:
                data, self.pending = self.pending, b""
                data += self.read_bytes(None)
                # Esc 단독인지 시퀀스 시작인지 구분하려고 잠깐 더 기다린다
                if data.endswith(b"\033"):
                    data += self.read_bytes(0.05)
                if not self.handle(data):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.out.write(MOUSE_OFF)
            self.clear()
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)


def cmd_menu(_args):
    tty = current_tty()
    if not tty or not sys.stdin.isatty():
        sys.exit("wslbtn menu: 터미널에서 실행하세요.")
    Menu(tty).run()


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
        if (a.Length != 1) return 2;
        // '#' 뒤(툴팁용 명령 표시)는 버리고 토큰만 넘긴다. 그래서 그 부분에 무엇이 있든 wsl.exe에 닿지 않는다.
        var m = Regex.Match(a[0], @"^__SCHEME__://fire/([A-Za-z0-9_-]{22})/?(#.*)?$", RegexOptions.Singleline);
        if (!m.Success) return 2;
        var psi = new ProcessStartInfo(@"C:\Windows\System32\wsl.exe", Args + " __SCHEME__://fire/" + m.Groups[1].Value);
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

    sub.add_parser("menu", help="이 터미널의 버튼을 그냥 클릭(또는 숫자 키)으로 누르는 메뉴").set_defaults(func=cmd_menu)
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
