import fcntl
import io
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import wslbtn


class WslbtnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        state = Path(self.tmp.name)
        wslbtn.STATE_DIR = state
        wslbtn.BUTTONS_DIR = state / "buttons"
        wslbtn.LOG_FILE = state / "fire.log"
        self.out = state / "tty"
        self.out.touch()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            wslbtn.main(["btn", *args])
        url = buf.getvalue().split("\033]8;;")[1].split("\033\\")[0]
        # 테스트에서는 tty 대신 파일에 출력하게 바꿔 둔다
        token = wslbtn.TOKEN_RE.match(url).group(1)
        p = wslbtn.BUTTONS_DIR / f"{token}.json"
        data = json.loads(p.read_text())
        data["tty"] = str(self.out)
        p.write_text(json.dumps(data))
        return url

    def log(self):
        return wslbtn.LOG_FILE.read_text() if wslbtn.LOG_FILE.exists() else ""

    def test_click_runs_command_and_writes_to_tty(self):
        url = self.make("hi", "--", "echo", "hello")
        wslbtn.main(["fire", url])
        out = self.out.read_text()
        self.assertIn("hello", out)
        self.assertIn("▶ hi", out)
        self.assertNotIn("✗", out)  # 빨리 성공하면 꼬리줄 없음

    def test_shell_string(self):
        url = self.make("sh", "-s", "echo a | tr a b")
        wslbtn.main(["fire", url])
        self.assertIn("b\n", self.out.read_text())

    def test_reusable_by_default(self):
        url = self.make("x", "--", "echo", "run")
        wslbtn.main(["fire", url])
        wslbtn.main(["fire", url])
        self.assertEqual(self.out.read_text().count("run\n"), 2)

    def test_once(self):
        url = self.make("x", "--once", "--", "echo", "run")
        wslbtn.main(["fire", url])
        wslbtn.main(["fire", url])
        self.assertEqual(self.out.read_text().count("run\n"), 1)
        self.assertIn("없는 토큰", self.log())

    def test_expired(self):
        url = self.make("x", "--ttl", "0", "--", "echo", "run")
        time.sleep(0.01)
        wslbtn.main(["fire", url])
        self.assertEqual(self.out.read_text(), "")

    def test_failure_shows_exit_code(self):
        url = self.make("bad", "--", "false")
        wslbtn.main(["fire", url])
        self.assertIn("✗ bad · 종료 코드 1", self.out.read_text())

    def test_tooltip_shows_command_safely(self):
        url = self.make("t", "-s", 'echo "hi" 100% C:\\x 안')
        frag = url.split("#", 1)[1]
        self.assertEqual(frag, "echo %22hi%22 100%25 C:%5Cx %EC%95%88")
        wslbtn.main(["fire", url])  # '#' 뒤가 있어도 실행된다
        self.assertIn("hi", self.out.read_text())

    def test_rejects_forged_and_injected(self):
        url = self.make("x", "--", "touch", "pwned").split("#")[0]
        for bad in (["wslbtn://fire/" + "A" * 22],             # 모르는 토큰
                    [url, "--evil"],                           # 인자 끼워넣기
                    [url + '" -o x'],                          # 형식 불일치
                    ["wslbtn://fire/../../etc/passwd"]):
            wslbtn.main(["fire", *bad])
        self.assertEqual(self.out.read_text(), "")
        self.assertEqual(self.log().count("거부"), 4)

    def test_cwd_preserved(self):
        d = tempfile.mkdtemp()
        old = os.getcwd()
        os.chdir(d)
        try:
            url = self.make("pwd", "--", "pwd")
        finally:
            os.chdir(old)
        wslbtn.main(["fire", url])
        self.assertIn(os.path.realpath(d), self.out.read_text())

    def test_button_file_is_private(self):
        self.make("x", "--", "true")
        f = next(wslbtn.BUTTONS_DIR.glob("*.json"))
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)


class LayoutTest(unittest.TestCase):
    def test_korean_is_two_cells(self):
        self.assertEqual(wslbtn.cell_width("안녕 ab"), 7)

    def test_layout_and_hit_test(self):
        buttons = [{"label": "둘"}, {"label": "안녕"}]
        lines = wslbtn.layout(buttons, cols=120)
        # " 1 둘 " = 6칸(1~6열), 한 칸 띄고 " 2 안녕 " = 8칸(8~15열)
        self.assertEqual([(a, b) for a, b, _, _ in lines[0]], [(1, 6), (8, 15)])
        self.assertEqual(wslbtn.hit_test(lines, 10, 3, 10), 0)
        self.assertEqual(wslbtn.hit_test(lines, 10, 15, 10), 1)
        self.assertIsNone(wslbtn.hit_test(lines, 10, 7, 10))   # 버튼 사이 빈칸
        self.assertIsNone(wslbtn.hit_test(lines, 10, 3, 11))   # 다른 줄

    def test_layout_wraps(self):
        # " 1 abcdef " = 10칸 → 1~10열, 12~21열이 한 줄에 들어가고 세 번째는 다음 줄
        lines = wslbtn.layout([{"label": "abcdef"}] * 3, cols=21)
        self.assertEqual([len(l) for l in lines], [2, 1])
        self.assertEqual([len(l) for l in wslbtn.layout([{"label": "abcdef"}] * 3, cols=20)], [1, 1, 1])


@unittest.skipUnless(shutil.which("bash"), "bash 필요")
class PtyBashTest(unittest.TestCase):
    """실제 대화형 bash를 pty에 띄운다. 테스트가 터미널 역할을 한다."""

    HERE = os.path.dirname(os.path.abspath(__file__))

    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        env = dict(os.environ, PS1="PROMPT> ", XDG_STATE_HOME=self.state.name)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvpe("bash", ["bash", "--norc", "--noprofile", "-i"], env)
        self.env = env
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        self.read(1.0)

    def tearDown(self):
        os.kill(self.pid, 9)
        os.waitpid(self.pid, 0)
        self.state.cleanup()

    def read(self, secs, cursor_row=None):
        """출력을 읽는다. cursor_row를 주면 커서 위치 질문(DSR)에 그 행이라고 답한다."""
        buf, end = b"", time.time() + secs
        while time.time() < end:
            if select.select([self.fd], [], [], 0.05)[0]:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    break
                buf += chunk
                if cursor_row and b"\033[6n" in chunk:
                    os.write(self.fd, f"\033[{cursor_row};1R".encode())
        return buf.decode(errors="replace")

    def wslbtn(self, args):
        os.write(self.fd, f"python3 {self.HERE}/wslbtn.py {args}\n".encode())


class PromptRedrawTest(PtyBashTest):
    """클릭(fire) 후 프롬프트가 다시 그려지는지 본다."""

    def make_button(self, suffix=""):
        os.write(self.fd, f"python3 {self.HERE}/wslbtn.py btn t -- echo FIRED{suffix}\n".encode())
        return re.search(r"wslbtn://fire/[A-Za-z0-9_-]{22}", self.read(1.0)).group(0)

    def fire(self, url):
        # Windows에서 오는 것처럼 터미널과 무관한 새 세션에서 실행
        subprocess.run([sys.executable, f"{self.HERE}/wslbtn.py", "fire", url],
                       env=self.env, start_new_session=True)
        return self.read(1.0)

    def size(self):
        return struct.unpack("HHHH", fcntl.ioctl(self.fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]

    def test_redraws_prompt_and_pending_input(self):
        url = self.make_button()
        os.write(self.fd, b"abc")  # 입력 중인 내용
        self.read(0.3)
        after = self.fire(url)
        self.assertIn("FIRED", after)
        self.assertIn("PROMPT> abc", after.split("FIRED")[-1])
        self.assertEqual(self.size(), (40, 120))

    def test_leaves_running_program_alone(self):
        url = self.make_button("; sleep 3")
        after = self.fire(url)
        self.assertIn("FIRED", after)
        self.assertNotIn("PROMPT>", after.split("FIRED")[-1])
        self.assertEqual(self.size(), (40, 120))


class MenuTest(PtyBashTest):
    """wslbtn menu: 마우스 모드에서 그냥 클릭/숫자 키로 실행되는지 본다."""

    def setUp(self):
        super().setUp()
        self.wslbtn("btn 안녕 -- echo HELLO; python3 " + self.HERE + "/wslbtn.py btn 둘 -- echo TWO")
        self.read(1.0)
        self.wslbtn("menu")
        # 안내 줄이 20행이라고 답한다 → 버튼 줄은 19행. 최근 버튼이 먼저: [1 둘](1~6열) [2 안녕](8~15열)
        self.assertIn("\033[?1000h", self.read(1.0, cursor_row=20))

    def test_click_runs_button(self):
        os.write(self.fd, b"\033[<0;10;19M\033[<0;10;19m")
        out = self.read(1.0, cursor_row=24)
        self.assertIn("HELLO", out)
        self.assertNotIn("TWO", out)

    def test_ctrl_click_also_works(self):
        os.write(self.fd, b"\033[<16;10;19M\033[<16;10;19m")
        self.assertIn("HELLO", self.read(1.0, cursor_row=24))

    def test_number_key(self):
        os.write(self.fd, b"1")
        self.assertIn("TWO", self.read(1.0, cursor_row=24))

    def test_click_outside_does_nothing(self):
        os.write(self.fd, b"\033[<0;7;19M\033[<0;50;10M")
        self.assertNotIn("▶", self.read(0.5))

    def test_quit_restores_terminal(self):
        os.write(self.fd, b"q")
        out = self.read(1.0)
        self.assertIn("\033[?1000l", out)
        self.assertIn("PROMPT>", out)


if __name__ == "__main__":
    unittest.main()
