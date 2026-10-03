# wslbtn

WSL 터미널 출력에 클릭 가능한 버튼을 찍습니다. Windows Terminal에서 Ctrl+클릭하면 WSL에서 명령이 실행되고, 출력은 버튼이 있던 터미널에 표시됩니다.

## 설치

```bash
alias wslbtn='python3 ~/wslbtn/wslbtn.py'   # ~/.bashrc 에 추가
wslbtn install                              # Windows에 wslbtn:// 핸들러 등록 (HKCU, 관리자 권한 불필요)
```

> `install`은 Windows Terminal 같은 일반 터미널에서 실행하세요. 패키지 앱(Claude 데스크톱 등) 안에서 실행하면 레지스트리 쓰기가 그 앱 전용 공간으로 격리되어서, Windows Terminal에서 클릭해도 "연결할 수 있는 앱이 없음"이 뜹니다.

## 사용

```bash
wslbtn btn "테스트 재실행" -- pytest -x
wslbtn btn "로그 보기" -s "tail -n 30 app.log"
wslbtn btn "배포" --once -- ./deploy.sh      # 한 번만 실행되는 버튼 (빨간색)
wslbtn btn "상태" --ttl 600 -- git status    # 10분 뒤 만료
```

- 기본적으로 버튼은 24시간 동안 여러 번 누를 수 있습니다.
- 명령은 버튼을 만든 시점의 작업 폴더와 환경 변수로 실행됩니다.
- 버튼을 만든 터미널이 닫혔으면 출력은 `~/.local/state/wslbtn/fire.log`에 남습니다.

## 보안

- 링크에는 명령 대신 추측할 수 없는 128비트 토큰만 들어갑니다. 그래서 누가 출력에 가짜 링크를 끼워 넣어도 아무것도 실행되지 않습니다.
- 명령과 환경 변수는 `~/.local/state/wslbtn/buttons/`에 권한 0600으로 저장됩니다.
- Windows 핸들러는 `install` 때 컴파일되는 창 없는 런처(`%LOCALAPPDATA%\wslbtn\wslbtn-launch.exe`)입니다. 런처가 URL 형식을 먼저 검사하고, 통과하면 `wsl.exe --exec`로 `fire`를 콘솔 창 없이 부릅니다. `fire`도 형식이 맞는 인자 하나만 받아서, 검사를 두 번 거칩니다.
- `wslg.exe`는 창이 뜨지 않지만 `--` 뒤의 인자를 셸로 해석해서 URL로 명령을 끼워 넣을 수 있기 때문에 쓰지 않습니다.

## 제거

```bash
wslbtn uninstall
```
