# 배포 점검 (0.5.85, 2026-10-10)

공개 배포 전에 실행파일과 저장소를 점검한 결과다. 법률 자문이 아니라 기술 점검이며, 회사 이름으로 공개하기 전에 법무 확인을 권한다.

## 결론

- **실행파일 배포는 가능하다.** 조건은 `THIRD_PARTY_LICENSES` 폴더를 실행파일과 함께 배포하는 것 하나다(배포 압축 파일에 들어 있다).
- Grainy 자체의 코드는 MIT 라이선스다(`LICENSE`, 2026-10-09 결정). 외부 부품은 아래 표의 각 라이선스를 따른다.
- 저장소는 2026-10-09에 공개했다. 공개본에는 작업 기록과 내부 문서를 넣지 않았다.

## 포함된 외부 부품

| 부품 | 용도 | 라이선스 | 조건과 현재 상태 |
|---|---|---|---|
| Python 3.13 | 실행 환경 | PSF-2.0 | 고지문 포함 |
| Qt 6 / PySide6 6.11 | 화면 | LGPL-3.0 | 고지문·소스 위치 포함. DLL을 따로 두어 사용자가 교체할 수 있다(동적 연결) |
| NumPy 2.5 | 수치 계산 | BSD-3-Clause 외 | 고지문 포함 |
| OpenCV 5.0 (headless) | 이미지 처리 | Apache-2.0 | 고지문 포함. 함께 오는 FFmpeg DLL은 빌드에서 삭제 |
| Pillow 12 | 이미지 입출력, AVIF | MIT-CMU | 고지문 포함 |
| rawpy 0.27 / LibRaw 0.22 | RAW 디코딩 | MIT / LGPL-2.1 또는 CDDL-1.0 | 고지문 포함. GPL 데모자이크 팩이 **빠져 있음**을 확인(`rawpy.flags`) |
| imagecodecs 2026.8 | JPEG XL, PNG, 색 변환 | BSD-3-Clause + 내부 코덱별 라이선스 | 코덱별 고지문 포함. 내부 코덱은 모두 허용형 라이선스이고, HEIF·JetRaw·JPEG XS는 **들어 있지 않음**을 확인 |
| tifffile, piexif, ExifRead, Send2Trash | TIFF, 메타데이터, 휴지통 | BSD / MIT | 고지문 포함 |
| lensfunpy 1.18 / Lensfun | 렌즈 보정 | MIT / LGPL-3.0, 렌즈 DB는 CC BY-SA 3.0 | 고지문과 Lensfun 소스 묶음 포함. DLL 교체 가능 |
| LittleCMS 2.19 | 색 변환 (직접 빌드한 DLL) | MIT | 고지문·소스 위치 포함 |
| setuptools | 실행 환경 부속 | MIT | 고지문 포함 |
| Microsoft Visual C++ 런타임 | 실행 환경 | Microsoft 재배포 허용 | 그대로 포함 |
| Natural Earth 지도 | GPS 지도 | 퍼블릭 도메인 | 출처 표기 |
| CIE 1931 표준 관찰자 표 | 색온도 계산 | CC BY-SA 4.0 | 원본 그대로, 출처·해시 표기 |
| DNG 기술 | 카메라 프로파일 읽기 | Adobe DNG SDK 라이선스 | 고지문 포함. **Adobe 카메라 프로파일은 배포하지 않는다**(테스트용 프로파일은 직접 만든 것) |
| PyInstaller 부트로더 | 실행파일 묶기 | GPL-2.0 + 예외 조항 | 예외 조항에 따라 배포물에는 조건이 붙지 않는다 |

## 0.5.74에서 제거한 것

- **FFmpeg 전부**: imageio-ffmpeg, PyAV, Qt Multimedia, OpenCV에 딸린 FFmpeg DLL. FFmpeg는 빌드 구성에 따라 GPL 조건이 붙을 수 있어 동영상 기능과 함께 뺐다.
- 빌드 스크립트는 FFmpeg 파일이 하나라도 남으면 실패한다.
- 배포본 크기: 507MB → 306MB.

## 개인정보와 네트워크

- 사용 통계 수집은 없다. 인터넷으로 나가는 기능은 아래 피드백과 업데이트 둘뿐이다.
- **업데이트**(`luma/updater.py`): "업데이트 확인"을 누르거나 "새 버전 자동 확인"을 켰을 때만 GitHub 릴리스 정보를 읽는다. 자동 확인은 처음 실행할 때 켤지 묻고(기본은 꺼짐), 설정에서 끌 수 있다. 사용자 정보는 보내지 않는다. 업데이트 파일은 GitHub이 공개한 SHA-256으로 검증하고, 이전 버전을 남긴 채 교체하며 실패하면 되돌린다.
- **피드백 보내기**: 인터넷으로 나가는 유일한 기능이다(`luma/feedback.py`). 사용자가 보내기를 누를 때만 입력한 글, Grainy 버전, PC 사양 한 줄(Windows 버전, 그래픽카드 이름), 보낼 때 새로 뽑는 무작위 접수 번호 5자리를 구글 설문지로 보낸다. 접수 번호는 그 PC의 설정 파일에만 남고, 반영된 번호는 `assets/feedback-done.json`에 적어 배포하면 업데이트 후 그 PC에서 "반영되었습니다"가 뜬다. 사진, 파일 이름·경로, 사용자·컴퓨터 이름은 보내지 않는다(테스트로 확인). 응답은 운영자의 구글 시트에 쌓인다.
- **ChatGPT 연결(Codex)**: 공개 배포본에서는 숨겨져 있다. 사용자 설정 파일에서 직접 켠 경우에만, 따로 설치한 Codex 프로그램을 통해 동작한다. 코드 설명에 따르면 입력한 문장과 현재 보정 숫자값만 전달하고 사진은 보내지 않는다.
- **구글 드라이브 백업(선택 기능)**: PC의 Google Drive 폴더에 카탈로그 파일을 복사할 뿐이고, 구글 계정 정보는 다루지 않는다.
- **AI 연결(MCP)**(`luma/mcp_*.py`, `luma/photo_service.py`, `luma/photo_images.py`, `assets/mcp`): Grainy가 실행 중이면 켜져 있다. 동의 창은 없고, 사용자가 설정 › AI 연결 (MCP)…에서 끄면 그 상태를 기억한다(`mcp_enabled`가 없으면 켜짐, false면 꺼짐). 켜지면 127.0.0.1에만 HTTP 서버를 연다. 모든 요청에 연결 키(64자리 16진수, `hmac.compare_digest`로 비교)가 있어야 하고 Host·Origin을 확인하며, 다른 컴퓨터에서는 접속할 수 없다. 시작하지 못하면(포트 사용 중, 설정 폴더에 쓸 수 없음, 다른 Grainy가 연결을 쓰는 중) 창이나 메시지 없이 설정 창의 상태와 최근 활동에만 남고 Grainy 시작을 막지 않는다. AI 프로그램은 사용자가 명령 한 줄로 설치한 중계 스크립트(Windows `%LOCALAPPDATA%\Grainy\mcp\grainy-mcp-bridge.ps1`, macOS `~/Library/Application Support/Grainy/mcp/grainy-mcp-bridge.sh`)로 연결하고, 설치 명령(`docs/mcp-install.json`)에는 키도 포트도 들어가지 않는다. 사용자가 AI 프로그램에 설치하고 일을 시키지 않으면 아무것도 읽히지 않고 컴퓨터 밖으로 나가지 않는다. Grainy 자체는 이 기능으로 인터넷에 아무것도 보내지 않는다. 연결한 AI 프로그램(사용자가 직접 설치한 Claude Code, Codex, Claude 데스크톱)이 읽을 수 있는 것: 사진 목록, 파일 이름과 전체 파일·폴더 경로(사용자 이름 포함), 키워드, 별점·플래그·라벨, 카메라·렌즈·노출 정보·촬영 날짜(GPS·일련번호·소유자 이름·제조사 정보는 보내지 않는다), 보정값, 최대 1568픽셀 미리보기와 최대 36장짜리 밀착 이미지. 그 내용은 그 AI 서비스로 가고 그 프로그램에도 남을 수 있다. 바꿀 수 있는 것: 보정값(작업마다 되돌릴 수 있는 한 묶음, 최근 5개, AI가 처음 손댄 사진에는 'AI 작업 전' 스냅샷), 열린 사진. 내보내기는 AI 내보내기 폴더(기본 사진/Grainy) 또는 그 아래 한 단계 폴더, 또는 사용자가 마지막으로 내보낸 폴더에만 하고, 있는 파일은 덮어쓰지 않는다. 원본 파일은 쓰지 않는다. 키는 사용자 설정(`settings.json`)에 그대로 있고, 중계 스크립트가 읽는 사본(`mcp/endpoint`, `mcp/auth-header`)은 연결을 켠 Grainy가 실행되는 동안만 있다. 중계 스크립트와 Grainy가 꺼져 있을 때의 답(`mcp/offline-*.json`, 영어·한국어로 Grainy를 실행하거나 연결을 켜라는 안내)은 연결이 켜진 채 시작할 때마다 다시 쓴다. 연결을 끌 때는 포트를 놓기 전에 지우고, 비정상 종료로 남은 사본은 다음 실행 때 지운다. 같은 설정을 쓰는 다른 Grainy(다른 라이브러리로 실행한 것)는 남의 사본을 지우지 않고, 그쪽에서 연결이 켜져 있으면 자기 연결을 켜지 않는다(`mcp/owner.lock`).
- 사진, 보관함, 시험용 자료는 저장소와 배포본에 들어가지 않는다(`.gitignore`).

## 저장소(소스) 점검

- 비밀 값(API 키, 토큰, 비밀번호)은 없다(패턴 검색).
- 공개본은 작업 기록 없이 현재 상태만 담았고, 작성자 표시는 깃헙의 비공개용 주소다.
- 개인 이메일, PC 사용자 이름, PC 경로가 소스와 실행파일에 없음을 검색으로 확인했다.
- 앱 자체의 라이선스는 MIT다. 저작권자 표기는 깃헙 계정 이름(pastorius9)으로 넣었다.

## 알려진 한계

- **코드 서명이 없다.** 받는 사람 PC에서 "알 수 없는 게시자" 경고(SmartScreen)가 뜬다.
- Windows 10/11 64비트 전용이다.
- 설치 프로그램이 따로 없다. 압축을 풀고 `Grainy.exe`를 실행한다.
- **AI 연결(MCP)의 한계**
  - 중계 스크립트는 Grainy가 AI 연결을 켠 채 한 번 실행되어야 생긴다. 그 전에 설치한 AI 프로그램은 Grainy를 실행한 뒤 다시 시작해야 연결된다. Windows 설치 명령은 PowerShell용이다(cmd에서는 `$env:LOCALAPPDATA`가 풀리지 않는다). `'--'`의 따옴표는 npm으로 설치한 claude·codex(PowerShell 스크립트) 때문에 필요하다. 따옴표 없는 `--`는 PowerShell이 빼 버린다(시험으로 확인).
  - Windows 중계 스크립트는 `-ExecutionPolicy Bypass`로 시작한다. 그룹 정책이 이를 막는 PC에서는 스크립트 파일 대신 그 내용을 실행하게 바꾼다. Claude 데스크톱 설정의 `args`는 `["-NoProfile", "-Command", "& ([scriptblock]::Create((Get-Content -Raw -LiteralPath (Join-Path $env:LOCALAPPDATA 'Grainy\\mcp\\grainy-mcp-bridge.ps1'))))"]`로 하고, Claude Code·Codex는 PowerShell에서 `claude mcp add --scope user grainy '--' powershell -NoProfile -Command '& ([scriptblock]::Create((Get-Content -Raw -LiteralPath (Join-Path $env:LOCALAPPDATA ''Grainy\mcp\grainy-mcp-bridge.ps1''))))'`처럼 설치한다(Codex는 앞부분만 `codex mcp add grainy`). 이때 중계 스크립트는 `%LOCALAPPDATA%\Grainy\mcp`에서 파일을 찾는다(둘 다 시험으로 확인). PowerShell의 제한 언어 모드(Constrained Language Mode)에서는 `Add-Type`과 `HttpClient`가 막혀 중계 스크립트가 동작하지 않는다.
  - 운영체제 기본 파일 대화상자(폴더 선택 등)가 열려 있는 동안에는 Grainy가 그 대화상자를 알아채지 못해 AI의 보정이 그사이에 들어갈 수 있다. 대화상자를 연 코드는 닫힌 뒤 카탈로그를 다시 읽으므로 이것으로 값이 섞이지는 않는다.
  - 중계 스크립트는 요청을 한 번에 하나씩 처리한다(Claude 데스크톱은 도구를 차례로 부른다).
  - 다른 계정과 함께 쓰는 컴퓨터에서는, Grainy가 비정상 종료된 뒤 다시 실행하기 전까지 중계 스크립트가 남은 사본(`mcp/endpoint`, `mcp/auth-header`)으로, 그사이 같은 포트(127.0.0.1)를 먼저 연 다른 계정의 프로그램에 연결 키를 보낼 수 있다. 고급에서 HTTP로 직접 연결하도록 설정한 프로그램은 Grainy가 꺼져 있는 동안에도 자기 설정의 키를 그렇게 보낼 수 있다. 그런 일이 있었을 수 있으면 AI 연결 창의 고급에서 새 키 만들기를 누르고, HTTP로 설정한 프로그램만 다시 설정한다.
  - 설치 명령이 PowerShell·sh에서 넘기는 인자와, Codex·Claude 데스크톱이 넘기는 환경 변수만으로 중계 스크립트가 동작하는지는 시험으로 확인했다(`tests/test_mcp_bridge.py`). 실제 Claude Code·Codex·Claude 데스크톱에 설치해 끝까지 써 보는 것, MSIX 설치판 Claude 데스크톱이 띄운 PowerShell이 `%LOCALAPPDATA%\Grainy\mcp`를 읽는지, Claude Code의 이미지 결과 크기 한도(`MAX_MCP_OUTPUT_TOKENS`), 새 MCP 방식(2026-07-28, 코드에 들어 있지만 꺼져 있음)은 확인하지 않았다. 확인 순서는 DEVELOPMENT.md에 있다.
- 다른 사람의 PC에 설치해 본 적은 없다. 다만 깃헙의 빌드 서버(아무것도 설치되지 않은 Windows Server 2022, 그래픽카드 없음)에서 소스로 빌드한 실행파일이 자체 검사를 통과한다(`.github/workflows/build.yml`). 그래픽카드 종류가 다른 PC에서의 속도와 화면 표시는 확인하지 못했다.

## 남은 결정

1. **이름과 로고**: "Grainy" 이름의 상표 조사는 하지 않았다. 로고 글자는 Century Gothic의 윤곽선이다(글꼴 파일은 배포하지 않는다). 공개 전에 글꼴 사용 조건을 확인하거나 공개 라이선스 글꼴로 바꾼다.
2. **코드 서명**: 경고 없이 배포하려면 코드 서명 인증서가 필요하다(유료).
