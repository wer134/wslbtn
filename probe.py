#!/usr/bin/env python3
"""wslbtn 검증용 프로브.

Windows Terminal에서 OSC 8 링크를 클릭했을 때 어떤 방식이 WSL까지 신호를
전달하는지, 그리고 브라우저 탭이 얼마나 거슬리는지 확인한다.

사용법 (반드시 Windows Terminal 안의 WSL에서 실행):
    python3 probe.py                    # 서버 시작 + 테스트 링크 출력
    python3 probe.py --register-scheme  # 커스텀 스킴(wslbtn-probe://) 핸들러도 등록해서 테스트
    python3 probe.py --unregister-scheme
"""

import argparse
import http.server
import os
import secrets
import subprocess
import sys
import threading
import time

PORT = 8765
SCHEME = "wslbtn-probe"
TOKEN = secrets.token_urlsafe(8)  # 이번 실행에서 출력한 링크만 인정
START = time.monotonic()

VARIANTS = {
    "http-204": "localhost → 204 No Content (탭이 아예 안 남는지)",
    "http-close": "localhost → HTML + window.close() (탭이 스스로 닫히는지)",
    "http-page": "localhost → 일반 HTML 페이지 (기준값: 탭이 남음)",
    "file-cmd": "file:// → Windows .cmd 실행 (경고창 / 콘솔 깜빡임 여부)",
    "custom-scheme": f"{SCHEME}:// 커스텀 스킴 (WT가 막는지)",
}
hits = {k: 0 for k in VARIANTS}
close_failed = False
lock = threading.Lock()

CLOSE_PAGE = """<!doctype html><meta charset="utf-8"><title>wslbtn</title>
<p>wslbtn: 클릭 수신됨. 이 탭이 보인다면 window.close()가 실패한 것.</p>
<script>
window.close();
setTimeout(() => fetch('/closefail/%s'), 400);
</script>"""

PLAIN_PAGE = """<!doctype html><meta charset="utf-8"><title>wslbtn</title>
<p>wslbtn: 클릭 수신됨 (기준값 페이지).</p>"""


def log(msg):
    print(f"\r\033[32m[+{time.monotonic() - START:6.1f}s]\033[0m {msg}", flush=True)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global close_failed
        parts = self.path.strip("/").split("/")
        if parts == ["ping"]:
            return self._send(200, "text/plain", "pong")
        if len(parts) == 3 and parts[0] == "hit" and parts[1] in VARIANTS and parts[2] == TOKEN:
            v = parts[1]
            with lock:
                hits[v] += 1
            log(f"클릭 수신: \033[1m{v}\033[0m")
            if v == "http-204":
                return self._send(204)
            if v == "http-close":
                return self._send(200, "text/html; charset=utf-8", CLOSE_PAGE % TOKEN)
            return self._send(200, "text/html; charset=utf-8", PLAIN_PAGE)
        if parts == ["closefail", TOKEN]:
            close_failed = True
            log("http-close: window.close() 실패 → 탭이 남아 있음")
            return self._send(204)
        self._send(404)

    def _send(self, code, ctype=None, body=""):
        data = body.encode()
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def win_temp_dir():
    """Windows %TEMP%\\wslbtn-probe 를 (윈도우 경로, WSL 경로)로 돌려준다."""
    out = subprocess.run(["cmd.exe", "/c", "echo %TEMP%"], capture_output=True,
                         text=True, cwd="/mnt/c").stdout.strip()
    win = out + "\\wslbtn-probe"
    wsl = subprocess.run(["wslpath", "-u", win], capture_output=True, text=True).stdout.strip()
    os.makedirs(wsl, exist_ok=True)
    return win, wsl


def write_cmd(wsl_dir, name, variant):
    """클릭되면 curl.exe로 서버에 신호를 보내는 .cmd 파일."""
    url = f"http://localhost:{PORT}/hit/{variant}/{TOKEN}"
    with open(os.path.join(wsl_dir, name), "w", newline="\r\n") as f:
        f.write(f'@echo off\ncurl.exe -s "{url}" >nul\n')


def reg_import(wsl_dir, win_dir, content):
    path = os.path.join(wsl_dir, "scheme.reg")
    with open(path, "w", encoding="utf-16") as f:
        f.write(content)
    # reg.exe는 콘솔 코드페이지(한글 Windows면 CP949)로 출력한다
    r = subprocess.run(["reg.exe", "import", win_dir + "\\scheme.reg"],
                       capture_output=True, encoding="cp949", errors="replace", cwd="/mnt/c")
    if r.returncode != 0:
        print(f"reg.exe 오류: {(r.stderr or r.stdout).strip()}")
    return r.returncode == 0


def register_scheme(win_dir, wsl_dir):
    cmd = (win_dir + "\\scheme.cmd").replace("\\", "\\\\")
    ok = reg_import(wsl_dir, win_dir, f"""Windows Registry Editor Version 5.00

[HKEY_CURRENT_USER\\Software\\Classes\\{SCHEME}]
@="URL:{SCHEME}"
"URL Protocol"=""

[HKEY_CURRENT_USER\\Software\\Classes\\{SCHEME}\\shell\\open\\command]
@="\\"{cmd}\\" \\"%1\\""
""")
    print(f"커스텀 스킴 {SCHEME}:// 등록 {'성공' if ok else '실패'} (HKCU, 관리자 권한 불필요)")
    return ok


def unregister_scheme(win_dir, wsl_dir):
    ok = reg_import(wsl_dir, win_dir, f"""Windows Registry Editor Version 5.00

[-HKEY_CURRENT_USER\\Software\\Classes\\{SCHEME}]
""")
    print(f"커스텀 스킴 {SCHEME}:// 등록 해제 {'성공' if ok else '실패'}")


def osc8(url, text):
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def check_reachable():
    """Windows 쪽에서 localhost로 WSL 서버에 닿는지 확인 (NAT 모드 포워딩 검증)."""
    r = subprocess.run(["curl.exe", "-s", "-m", "3", f"http://localhost:{PORT}/ping"],
                       capture_output=True, text=True, cwd="/mnt/c")
    return r.stdout.strip() == "pong"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--register-scheme", action="store_true")
    ap.add_argument("--unregister-scheme", action="store_true")
    args = ap.parse_args()

    win_dir, wsl_dir = win_temp_dir()
    if args.unregister_scheme:
        return unregister_scheme(win_dir, wsl_dir)

    if not os.environ.get("WT_SESSION"):
        print("\033[33m경고: Windows Terminal 안이 아닌 것 같음 (WT_SESSION 없음). 결과가 다를 수 있음.\033[0m")

    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(f"포트 {PORT}가 이미 사용 중. 이전 probe.py가 떠 있는지 확인하세요.")
    threading.Thread(target=server.serve_forever, daemon=True).start()

    reachable = check_reachable()
    print(f"Windows → WSL localhost:{PORT} 연결: "
          + ("\033[32mOK\033[0m" if reachable else
             "\033[31m실패\033[0m (http 테스트는 신호가 안 올 것. .wslconfig의 localhostForwarding 확인)"))

    write_cmd(wsl_dir, "hit.cmd", "file-cmd")
    write_cmd(wsl_dir, "scheme.cmd", "custom-scheme")
    scheme_registered = args.register_scheme and register_scheme(win_dir, wsl_dir)

    base = f"http://localhost:{PORT}/hit"
    file_url = "file:///" + (win_dir + "\\hit.cmd").replace("\\", "/")
    links = {
        "http-204": f"{base}/http-204/{TOKEN}",
        "http-close": f"{base}/http-close/{TOKEN}",
        "http-page": f"{base}/http-page/{TOKEN}",
        "file-cmd": file_url,
        "custom-scheme": f"{SCHEME}://hit/{TOKEN}",
    }

    print("\n아래 링크를 하나씩 클릭해 보세요 (안 되면 Ctrl+클릭). 각각 탭/창/경고가 어떻게 뜨는지 봐 두세요.\n")
    for i, (v, desc) in enumerate(VARIANTS.items(), 1):
        label = f"[ {v} ]"
        print(f"  {i}. {osc8(links[v], label)}{' ' * (19 - len(label))}  {desc}")
    if not scheme_registered:
        print(f"\n  ※ 5번은 핸들러 미등록 상태. WT가 경고/무반응이면 --register-scheme 으로 다시 시도해서"
              f"\n    'WT가 막는 것'인지 '핸들러가 없는 것'인지 구분하세요.")
    print("\n클릭 신호가 오면 여기에 표시됩니다. 끝나면 Ctrl+C.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    server.shutdown()

    print("\n\n===== 결과 =====")
    for v in VARIANTS:
        mark = "\033[32m수신\033[0m" if hits[v] else "\033[31m없음\033[0m"
        extra = ""
        if v == "http-close" and hits[v]:
            extra = " (탭 자동 닫힘 실패)" if close_failed else " (탭 자동 닫힘 성공으로 추정)"
        print(f"  {v:<14} {mark} x{hits[v]}{extra}")
    if scheme_registered:
        print(f"\n커스텀 스킴 등록이 남아 있음. 정리: python3 {sys.argv[0]} --unregister-scheme")


if __name__ == "__main__":
    main()
