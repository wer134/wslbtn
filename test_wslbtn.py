import io
import json
import os
import tempfile
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
        token = url.rsplit("/", 1)[1]
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
        self.assertIn("종료 코드 0", out)

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

    def test_rejects_forged_and_injected(self):
        url = self.make("x", "--", "touch", "pwned")
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


if __name__ == "__main__":
    unittest.main()
