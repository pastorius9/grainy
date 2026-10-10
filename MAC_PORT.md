# macOS 포팅 인계 문서

Grainy를 macOS에서 실행·배포할 수 있게 만드는 작업을 새 세션에서 시작할 때 읽는 문서다. 0.5.82(2026-10-10) 기준이며, 이 시점까지 macOS에서 실행해 본 적은 없다. 아래의 "될 것이다"는 코드를 읽고 내린 추정이고 확인된 사실이 아니다.

## 먼저 지킬 것

1. **`mac` 브랜치에서만 작업한다.** `main`에는 올리지 않는다. `main`은 Windows 작업 폴더에서 정리해 내보내는 사본이라, 여기에 직접 올리면 다음 Windows 릴리스와 어긋난다. 합치는 일은 Windows 쪽에서 한다.
2. **커밋 전에 git 이름과 이메일을 저장소 소유자의 익명 주소로 맞춘다.** 이 저장소의 기존 커밋과 같은 값이다(`git log -1 --format='%an <%ae>'`로 확인). 이 폴더에만 적용한다.
   ```bash
   git config user.name "<기존 커밋의 이름>"
   git config user.email "<기존 커밋의 noreply 주소>"
   ```
3. **개인 정보를 저장소에 넣지 않는다.** 실명, 회사 이름, 이메일 주소, 사용자 홈 경로(`/Users/<이름>/…`), 사진 폴더 이름, 키·토큰. 올리기 전에 바뀐 파일 전체를 검색해 확인한다. 로그와 검사 산출물(`validation/`)에 홈 경로가 들어가기 쉽다.
4. **릴리스를 만들지 않는다.** GitHub 릴리스 생성과 공개는 소유자가 직접 말했을 때만 한다. macOS 배포 파일의 이름은 `Grainy-<버전>-windows-x64.zip` 형식을 쓰지 않는다. Windows 설치본의 자동 업데이트가 그 이름을 찾는다.
5. **원본 사진을 덮어쓰거나 지우지 않는다.** 검사는 임시 보관함과 만든 사진만 쓴다. 영구 삭제 대신 휴지통(`send2trash`)을 쓴다.
6. 화면에 나오는 문구는 `tr()`을 거치고 `assets/i18n/en*.txt`에 영어를 추가한 뒤 `tools/compile_translations.py`를 실행한다.
7. 다른 프로그램보다 낫다는 식의 주장은 쓰지 않는다.

## 구성

- Python 3.13, PySide6(Qt), NumPy, OpenCV, rawpy(LibRaw), imagecodecs, lensfunpy. `requirements.txt`가 실행에 필요한 것이고 `requirements-test.txt`가 검사용이다. `requirements-lock.txt`는 Windows 빌드에 고정한 목록이라 macOS에서는 그대로 설치되지 않는다(`pefile`, `pywin32-ctypes`).
- 진입점은 `main.py`, 프로그램은 `luma/`, 검사는 `tests/`, 도구는 `tools/`, 네이티브 소스는 `native/`.
- 소스에서 실행: `python main.py`. 소스 실행 시 보관함은 저장소의 `data/` 폴더다(`.gitignore`에 있음).
- 자가 점검: `python main.py --self-test` (`LUMA_SELF_TEST_REPORT=<파일>`에 결과 JSON).
- 전체 검사: `python tools/check.py`. 결과는 `validation/check-…/`에 쌓인다.

## Windows 전용인 부분

대부분 `os.name!='nt'`이면 `None`을 돌려주고 대체 경로로 넘어가게 되어 있다. 그래서 첫 실행까지 고칠 곳은 많지 않을 것으로 본다.

| 파일 | 하는 일 | macOS에서 지금 | 할 일 |
|---|---|---|---|
| `luma/native_gpu.py`, `native/gpu_compute.cpp`, `assets/native/grainy_gpu.dll` | D3D11 컴퓨트 셰이더로 톤·색·디테일·회전 처리 | 꺼짐, CPU 경로 사용(결과는 같고 느림) | 3단계에서 Metal로 다시 작성 |
| `luma/native_color.py`, `assets/native/luma_lcms2.dll` | LittleCMS로 색 프로필 변환 | 꺼짐, `colorio.convert`의 대체 경로 | lcms2를 dylib로 빌드하거나 대체 경로의 정확도·속도 확인 |
| `luma/native_dcp.py`, `native/dcp_tables.cpp`, `assets/native/luma_dcp.dll` | DCP 카메라 프로필 표 적용 | 꺼짐 | 대체 경로 유무 확인, 없으면 dylib 빌드 |
| `luma/native_hdr.py`, `native/hdr_display.cpp` | HDR 화면 출력 | 꺼짐. HDR 기능 자체가 꺼져 있음(`engine.HDR_FEATURE=False`) | 손대지 않음 |
| `luma/display_profiles.py` | 모니터 색 프로필 읽기 | `None` → 모니터 색 관리 없음 | ColorSync로 화면별 프로필 읽기 |
| `luma/updater.py` | 실행파일 폴더 교체 방식의 업데이트 | 종료 대기 등이 Windows 전제 | `.app` 묶음 구조에 맞게 별도 구현. 그 전까지 업데이트 UI를 숨김 |
| `luma/cloud_backup.py` | 볼륨 이름으로 Google Drive 폴더 찾기 | 다른 분기 확인 필요 | `~/Library/CloudStorage/GoogleDrive-…/` 아래를 찾도록 |
| `luma/power.py`, `luma/render_cache.py` | 백그라운드 스로틀 해제, 메모리 크기 조회 | 건너뜀 | 필요하면 대체 |
| `luma/preferences.py`, `main.py`, `luma/profile_library.py`, `luma/auth.py` | `%LOCALAPPDATA%` 아래에 설정·보관함·프로필 | 환경 변수가 없어 홈 폴더 바로 아래에 만듦 | `~/Library/Application Support/Grainy/`로 |
| `main.py` | 작업 표시줄 아이콘용 AppUserModelID | `win32`에서만 실행 | 없음 |
| `Build Grainy.ps1`, `Install Grainy.ps1`, `.github/workflows/build.yml` | PyInstaller 빌드, 설치, CI | 쓸 수 없음 | `.app` 빌드 스크립트와 macOS CI 작업 추가 |
| 단축키·메뉴 | `Ctrl+…`를 문자열로 표시한 곳이 있음(`app.py`의 편집 메뉴 등) | Qt가 일부는 Cmd로 바꾸지만 직접 쓴 글자는 그대로 | `QKeySequence`로 표시하도록 정리, 메뉴 막대 위치 확인 |

검색어: `os.name`, `sys.platform`, `ctypes.windll`, `LOCALAPPDATA`, `.dll`.

## 권장 순서

1. **실행까지.** 가상 환경, `requirements.txt` 설치, `python main.py`. 뜨지 않으면 그 원인만 고친다. 설정·보관함 위치를 `Application Support`로 옮긴다. 자가 점검을 통과시킨다.
2. **검사 정리.** `tools/check.py`를 돌려 실패 목록을 만든다. Windows 전용 검사(GPU 일치, DLL, 업데이트 교체, Lightroom 연동)는 건너뛰도록 표시하고, 나머지 실패는 고친다. 무엇을 건너뛰었는지 기록한다.
3. **색.** 모니터 프로필(ColorSync)과 lcms2. 넓은 색역 화면에서 sRGB 사진이 과포화로 보이지 않는지 확인한다.
4. **속도.** CPU 경로로 미리보기와 내보내기 시간을 잰다. 느리면 Metal 포팅을 한다. `native/gpu_compute.cpp`의 셰이더와 `luma/native_gpu.py`의 구조체가 기준이고, CPU 결과와의 일치를 검사로 고정한다(Windows는 차이 5e-7 이하).
5. **묶음.** PyInstaller로 `.app`, 그다음 `.dmg`. Apple Silicon과 Intel 중 무엇을 지원할지 먼저 정한다.
6. **서명·공증.** Apple 개발자 계정이 있어야 하고 서명에 이름이 드러난다. 소유자가 결정할 일이니 묻고 진행한다. 서명이 없으면 받은 사람이 직접 허용해야 실행된다.
7. **업데이트.** `.app` 교체 방식으로 따로 만든다.

## 알아 둘 동작

- 톤 처리에는 버전이 있다(`tone_version`): 1 클리핑, 2 하이라이트 롤오프, 3 국부 대비를 유지하는 하이라이트·그림자(`luma/tone_response.py`). 저장된 보정은 자기 버전을 유지한다. CPU 경로가 기준 구현이다.
- 자동 색(`luma/auto_color.py`), 자동 수평(`luma/upright.py`의 `_level`), 수평 맞추기 크롭 제한(`luma/straighten.py`)은 순수 Python/NumPy라 그대로 동작할 것으로 본다.
- 폴더 자동 반영(`luma/folder_sync.py`), 피드백(`luma/feedback.py`)도 플랫폼 코드가 없다.
- 줄바꿈이 CRLF와 LF로 섞인 파일이 있다. 파일 전체를 다시 쓰지 말고 바꿀 부분만 고친다. `.gitattributes`는 `* -text`다.

## 끝낼 때 남길 것

무엇이 되고 무엇이 안 되는지, 건너뛴 검사, 측정한 속도, 소유자가 결정해야 할 것(지원 기종, 서명)을 이 문서 아래에 날짜와 함께 덧붙인다.

## 2026-10-10 — 1단계(실행까지)

Apple Silicon, macOS 15.0.1, Homebrew Python 3.13.16, `requirements.txt`의 최신 호환 버전(PySide6 6.12.0, NumPy 2.5.3, OpenCV 5.0.0, rawpy 0.27.1, imagecodecs 2026.10.10, lensfunpy 1.18.0)으로 확인했다. 한 대에서 한 번 확인한 결과다.

**되는 것**
- `python main.py --self-test`가 통과한다(화면 없는 모드, 약 4초, 보고 항목 127개).
- 실제 창(Cocoa, 배율 2.0)이 뜨고 임시 보관함의 사진 한 장을 현상 화면에 표시한다. 오류 알림 없음. 메뉴는 macOS 메뉴 막대로 들어간다.
- 설정·보관함·Codex 로그인·카메라 프로필 위치가 `~/Library/Application Support/Grainy/`다(`luma/user_paths.py`). Windows의 위치는 그대로다. 카메라 프로필은 Adobe의 macOS 위치 두 곳도 찾는다.
- 글꼴은 macOS에서 Apple SD Gothic Neo를 쓴다.

**꺼 둔 것**
- 업데이트: macOS에서 설정 메뉴의 두 명령을 숨기고 시작 시 확인을 하지 않는다(`updater.SUPPORTED`).
- 자가 점검의 네이티브 라이브러리 확인 네 가지(색 변환, DCP 표, HDR 출력, GPU)는 Windows에서만 필수다. macOS에서는 `native_color_engine`·`native_dcp_engine`·`native_hdr_display_supported`가 false, `gpu_status`가 `off`로 기록된다. 나머지 항목은 그대로 실행된다.

**아직 안 한 것**
- 사람이 직접 창을 조작해 보는 확인(슬라이더 드래그, 실제 RAW 가져오기, 내보내기 대화상자, 단축키). 위 확인은 프로그램이 창을 띄워 캡처한 것이다.
- `tools/check.py` 전체 검사(2단계), 색(3단계), 속도 측정(4단계).
- 접이식 패널의 화살표가 화면 없는 모드보다 크게 그려진다. 원인은 보지 않았다.
- `Ctrl+…` 글자 표시, Google Drive 폴더 찾기, 모니터 색 프로필은 손대지 않았다.

## 2026-10-10 — 2~7단계 (위 1단계 기록의 "꺼 둔 것·안 한 것"은 아래로 대체)

같은 날 같은 기기(Apple Silicon M1 Pro, macOS 15.0.1)에서 이어서 했다. 패키지는 Windows 고정 목록과 같은 버전으로 맞췄다(`requirements-lock-macos.txt`: PySide6 6.11.2, imagecodecs 2026.8.16, tifffile 2026.9.15 등). 커밋은 모두 `mac` 브랜치에 있고 `main`에는 올리지 않았다. 릴리스는 만들지 않았다.

### 되는 것

- **전체 검사**: `tools/check.py` 통과. 1,243개 중 1,238개 통과, 5개 건너뜀. 깃헙의 macOS 14 Apple Silicon 빌드 서버에서도 같은 1,243개가 통과했다(워크플로의 `macos_tests` 선택 항목, 문서 커밋 직전의 코드).
- **건너뛴 검사 5개**: `tests/test_display_color.py`의 `test_windows_unicode_buffer_and_dc_cleanup` 5가지(Win32 `GetICMProfileW` 호출 자체의 검사). 그 밖에 건너뛴 것은 없다.
- **네이티브 라이브러리**: `tools/build_native_macos.py`가 Windows와 같은 소스로 `luma_lcms2.dylib`, `luma_dcp.dylib`를 만든다(arm64·x86_64 한 파일). LittleCMS는 imagecodecs의 변환과, DCP 표는 NumPy 기준과 비트 단위로 같다(arm64에서 확인. x86_64 쪽은 실행해 보지 않았다).
- **GPU(Metal)**: `native/gpu_compute_metal.mm`. D3D11 모듈과 같은 함수·구조체·패스 순서라 `luma/native_gpu.py`가 그대로 쓴다. GPU 검사 226개가 모두 통과한다(CPU 기준과의 차이는 기존 허용 범위 안).
- **모니터 색**: 자동 모드가 CoreGraphics에서 화면별 ICC 프로필을 읽는다. 창의 색 공간이 화면의 색 공간과 같음(macOS가 다시 변환하지 않음)을 확인했다. 내장 XDR 화면의 프로필을 읽어 적용하는 것까지 확인했고, 색을 계측기로 재지는 않았다.
- **앱 묶음**: `tools/build_macos.py`가 `release/Grainy.app`, 업데이트용 `Grainy-<버전>-macos-arm64.zip`, `Grainy-<버전>-macos-arm64.dmg`를 만든다. 만든 앱의 자가 점검이 통과한다(133개 항목, GPU 일치 포함). 깃헙 빌드 서버에서도 소스에서 빌드해 통과한다(`.github/workflows/build.yml`의 `macos` 작업).
- **업데이트**: macOS에서는 `Grainy.app` 묶음을 통째로 바꾼다. 만든 앱과 로컬 릴리스 정보로 내려받기·검증·교체·재시작을 끝까지 해 봤다. 실제 깃헙 릴리스로는 해 보지 않았다(macOS 릴리스가 없다).
- **파일 이름**: Finder로 만든 한글 폴더·파일 이름(분해형)으로 가져오기·검색·이동·폴더 이름 변경·다시 연결이 된다. 보관함에는 조합형으로 저장하고, 경로 비교는 macOS에서 대소문자와 조합 방식을 구분하지 않는다.
- **조작**: 트랙패드 스크롤은 사진 이동, 핀치는 확대, 두 손가락 두 번 탭은 맞춤↔100%. 휠 마우스는 Windows처럼 확대한다. 단축키는 Qt가 Ctrl을 ⌘로 바꾸고, 안내 문구도 ⌘로 표시한다.
- **그 밖에**: 위젯 스타일(Fusion), 한글 글꼴, Finder 문구, Google Drive 폴더(`~/Library/CloudStorage/GoogleDrive-…`), App Nap 해제, 피드백의 시스템 정보(macOS 버전), 숨겨진 Codex 연결의 실행 파일 찾기.
- `tests/ui_smoke.py`를 실제 창(Cocoa)으로 실행해 통과했다: 가져오기, 슬라이더 키보드 조작, 실행 취소, 마우스로 크롭, 일괄 복사, 별점, 16비트 TIFF 일괄 내보내기, 원본 해시 불변, 보관함 다시 열기.

### 고친 버그 (Windows에는 영향 없음)

- 하위 폴더 조회가 경로 구분자를 `\`로 고정해 macOS에서 하위 폴더 사진이 목록에 나오지 않았다(`luma/library_query.py`).
- 폴더 이동·다시 연결이 `Path.relative_to`의 정확한 일치에 기대고 있어, 저장된 폴더 키(소문자)와 실제 경로의 대소문자가 다르면 실패했다(`folders.below`).
- 라이선스 고지 수집이 setuptools의 `licenses` 코드 패키지를 고지문으로 알고 복사했고, 그 안의 `.pyc`에 빌드한 PC의 경로가 들어 있었다(`tools/package_notices.py`). Windows 배포본에도 같은 파일이 들어갔을 수 있으니 확인이 필요하다.
- `tests/test_library_browser.py`의 썸네일 검사가 첫 렌더의 썸네일 저장을 기다리지 않아 5번에 1번쯤 실패했다(대기 조건 수정).

### 측정한 속도 (M1 Pro, 24MP 합성 사진, 미리보기 1800×1200, 앱 안 시간, 각 1회 측정)

| 항목 | CPU만 | Metal |
|---|---|---|
| 노출 슬라이더 끄는 중, 다른 보정 없음 | 18.6ms/프레임 | 9.0ms/프레임 |
| 같은 조건, 손을 뗀 뒤 최종 화면 | 90ms | 51ms |
| 끄는 중, 노이즈·텍스처·명료도·선명도 보정 있음 | 47.9ms (19fps) | 25.2ms (33fps) |
| 같은 조건, 손을 뗀 뒤 최종 화면 | 837ms | 208ms |
| 24MP 전체 현상(같은 보정) | 8.2초 | 1.6초 |

- 가장 느린 부분은 색 노이즈 제거(가이드 필터)다. 가이드의 5×5 중앙값을 99번 교환하는 네트워크로 바꿔 83→59ms로 줄였다(값은 같다. 모든 0/1 입력으로 검증).
- Windows(D3D11)와 같은 조건으로 비교하지 않았다. 화면에 나타나기까지의 시간(화면 합성)은 재지 않았다.

### 안 되는 것·확인 못 한 것

- **회전(수평 맞추기)의 GPU 처리**: Metal에 double이 없어 `grainy_gpu_rotate`는 "미구현"을 돌려주고 CPU(PIL)가 회전한다. 미리보기 39ms, 24MP 464ms.
- **HDR**: 손대지 않았다(`native_hdr`는 Windows 전용, 기능 자체가 꺼져 있음).
- **실제 카메라 RAW 파일**: 아래 "실제 RAW 시험"에서 확인했다. Nikon Z 8의 고효율 압축 NEF는 열리지 않는다(LibRaw 0.22가 지원하지 않는 형식. "읽을 수 없는 사진" 안내가 뜬다).
- **사람이 직접 쓰는 확인**: 창 조작은 프로그램이 보낸 이벤트로만 확인했다. 실제 트랙패드 동작, 메뉴 막대, 파일 대화상자, 드래그 앤 드롭은 사람이 써 봐야 한다. 메뉴 막대는 시험 프로세스가 활성 앱이 될 수 없어 내용을 확인하지 못했다.
- **Intel Mac**: 네이티브 라이브러리는 x86_64를 포함하지만 앱은 빌드한 Python의 아키텍처(arm64)로만 만들어진다. Intel에서는 실행해 보지 않았다.
- **최소 macOS**: 묶인 라이브러리 중 가장 높은 요구 버전을 `Info.plist`에 적는다. 이 Mac의 Homebrew Python으로 만든 앱은 15.0이다. (여기에 "빌드 서버로 만든 앱은 14.0"이라고 적었던 것은 확인하지 않고 쓴 것이고 틀렸다. 아래 "0.5.84 설치 확인" 참고.)
- **Codex 연결**: 숨겨진 기능이고 실제 Codex 프로그램으로 확인하지 않았다.
- **여러 모니터, 외장 디스크(대소문자를 구분하는 볼륨 포함), 다른 사람의 Mac**: 확인하지 않았다.

### 실제 RAW 시험 (2026-10-10, raw.pixls.us의 CC0 샘플 15개, 저장소에는 넣지 않음)

Canon EOS R5(CR3)·5D Mark IV(CR2), Nikon Z 6·D850·Z 8(NEF), Fujifilm X-T4·X-T5(RAF, X-Trans), Leica M10·M (Typ 240)(DNG), Sony α7 III·α7 IV(ARW), Panasonic S5(RW2), Olympus E-M1 Mark III(ORF), Pentax K-3 Mark III(DNG), Ricoh GR III(DNG).

- 15개 중 14개가 전체 해상도로 열리고, 현상되고, JPEG로 내보내진다. 원본 해시는 그대로다. 열리지 않는 하나는 위의 Nikon Z 8이다.
- Nikon Z 8은 같은 저장소의 샘플 세 개를 따로 확인했다: 58MB 파일은 열리고(8280×5520, 2.5초), 34MB와 23MB 파일은 열리지 않는다. 크기로 보아 차례로 무손실 압축, 고효율★, 고효율이다(파일 안의 압축 표시값은 읽지 못했다). 기종이 아니라 압축 방식의 문제다.
- 14개 모두 Metal 결과와 CPU 결과의 차이가 8비트 1단계 이하다.
- 카메라가 RAW에 넣어 둔 JPEG와 나란히 놓고 눈으로 봤을 때 색상은 맞다(색상표, X-Trans 포함). 밝기는 기본값이 카메라 JPEG보다 어두운 사진이 많다. 카메라의 톤 곡선을 쓰지 않기 때문으로 보이며 macOS만의 차이인지는 Windows와 비교하지 않아 모른다.
- 앱 흐름(폴더 가져오기 → 한 장씩 열기 → 노출 변경 → 썸네일)으로 14장을 가져오는 데 9.8초, 한 장 여는 데 0.2~1.6초, 보정 반영 0.07~0.15초(화면 없는 모드, 1회).
- 전체 해상도 해석은 Bayer 센서가 0.6~2.2초, Fujifilm X-Trans가 11~17초다(40MP X-T5 내보내기 20초). 이 Mac의 rawpy는 OpenMP 없이 빌드돼 있다. Windows에서의 시간은 재지 않았다.
- 고친 것: 파일 이름에 `:` `"` `<` `>` `|` `?` `*`가 있으면(Finder에서 `/`로 보이는 `:` 포함) 내보내기가 "유효하지 않은 출력 이름"으로 실패했다. 내보낼 이름에서는 `_`로 바꾼다.

### 소유자가 결정할 것

1. **서명·공증**: 지금 앱은 임시(ad hoc) 서명이다. 내려받은 사람은 처음 한 번 `시스템 설정 → 개인정보 보호 및 보안`에서 "그래도 열기"를 눌러야 한다. Apple 개발자 계정으로 서명하면 서명에 이름이 드러난다.
2. **FFmpeg 처리 방식**: OpenCV의 macOS 휠은 FFmpeg(x264·x265가 든 GPL 빌드)를 직접 연결한다. Windows처럼 FFmpeg를 넣지 않으려고, 빌드에서 관련 라이브러리 62개를 지우고 OpenCV가 찾는 5개 자리에 빈 대체 라이브러리(호출되면 멈추는 함수 115개)를 둔다. 동영상 입출력은 쓰지 않아 자가 점검과 전체 검사는 통과한다. 이 방식을 받아들일지, OpenCV를 FFmpeg 없이 직접 빌드할지는 배포 전에 정할 일이다(법률 판단은 하지 않았다).
3. **지원 기종**: Apple Silicon만 할지 Intel도 할지, 최소 macOS를 얼마로 할지.
4. **릴리스에 macOS 파일 올리기**: 올리기로 했다(2026-10-10). 워크플로의 `macos-release` 작업이 태그로 시작된 빌드에서, 그 태그의 릴리스가 이미 공개돼 있을 때만 `Grainy-<버전>-macos-arm64.zip`과 `.dmg`를 그 릴리스에 붙인다. 릴리스를 만들지는 않는다. 태그 빌드는 태그가 가리키는 커밋의 워크플로를 쓰므로, `mac` 브랜치가 `main`에 합쳐진 뒤의 릴리스부터 동작한다. 실제 릴리스로는 아직 돌려 보지 않았다. Windows 쪽 이름(`…-windows-x64.zip`)과 겹치지 않는다.
   - **서명**: 하지 않기로 했다(2026-10-10). Apple 개발자 가입은 실명이나 법인명이 서명에 드러난다.
5. **아이콘**: macOS 격자에 맞춰 여백만 두었다(모서리는 직각 그대로).
6. **Windows 쪽에서 합칠 때**: `luma/updater.py`, `tools/package_notices.py`, `tools/compile_translations.py`(줄바꿈을 CRLF로 고정), 검사 파일 몇 개가 공통 코드다. Windows에서 전체 검사를 다시 돌려야 한다. 이 브랜치의 변경은 Windows에서 실행해 보지 않았다.

### 빌드 방법 (macOS)

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock-macos.txt
.venv/bin/python tools/build_macos.py        # release/Grainy.app, .zip, .dmg
```

Xcode 명령줄 도구(clang)가 필요하다. 전체 Xcode는 필요 없다(Metal 셰이더는 실행할 때 macOS가 컴파일한다).

## 2026-10-10 — Windows 쪽에서 `mac` 브랜치를 합침

`mac` 브랜치(fa3332a까지)를 Windows 작업 폴더의 0.5.83 위에 합쳤다. 충돌은 `tests/test_quick_export.py` 한 곳이었다. Windows에서 전체 검사를 다시 돌렸다(결과는 아래 "합친 뒤 Windows 확인").

**맥 쪽에서 이어서 할 것**

- **GPU 모듈 규격 17.** 0.5.83에서 Windows 모듈(`native/gpu_compute.cpp`)이 17이 됐다. 구조체는 16과 같고 함수 하나가 늘었다: `int grainy_gpu_trim(void* handle)`. 노이즈 제거용 큰 임시 버퍼(프레임 크기 4개)를 호출 사이에 유지하고, 이 함수가 불리면 푼다. `luma/native_gpu.py`가 GPU를 5초간 쓰지 않으면 부른다. 24MP 사진 12장 내보내기에서 사진마다 버퍼를 새로 만드는 시간이 GPU 시간의 3분의 1이었다.
  - Metal 모듈(`native/gpu_compute_metal.mm`)은 16 그대로다. `native_gpu.py`가 16과 17을 모두 받게 해 두었으므로 지금도 동작한다(16이면 trim을 부르지 않는다).
  - Metal에서도 같은 버퍼를 사진마다 만들고 있다면 같은 방식으로 유지하고 17로 올리면 된다. 유지할지의 기준은 Windows에서 "버퍼 합계가 그래픽 메모리의 4분의 1 이하"다.
- **동시 내보내기 상한이 3에서 4가 됐다**(`quick_export.export_concurrency`). Apple Silicon에서 맞는 값인지는 재 보지 않았다.
- **자동 수평 판정이 바뀌었다**(0.5.82, `luma/upright.py`의 `_level`). 세로선과 수평선만 기준으로 삼고, 근거가 약하면 돌리지 않는다.
- **배포본 검사 도구**: `tools/scan_release.py <폴더>`가 빌드한 기기의 폴더·홈 경로·사용자 이름이 배포본에 들어갔는지 모든 파일(중첩 압축 포함)에서 찾는다. macOS 빌드에서도 돌릴 것. Windows 배포본(0.5.74~0.5.83)에는 빌드 폴더 경로가 두 군데 들어가 있었다: 지적한 setuptools의 `.pyc`, 그리고 LittleCMS 라이브러리의 assert 문자열(소스 파일의 전체 경로). 사용자 이름이나 이메일은 없었다. 둘 다 고쳤다(`/d1trimfile`).
- Windows 빌드는 `assets` 안의 `*.dylib`, `*.macos.json`, `*.icns`를 배포본에서 뺀다.
- `main`의 최신 변경을 가져간 뒤 이어서 작업할 것. 릴리스는 계속 소유자가 말했을 때만.

## 2026-10-10 — 0.5.84: macOS 첫 릴리스

- `v0.5.84` 태그 빌드가 `Grainy-0.5.84-macos-arm64.dmg`와 `.zip`을 릴리스에 붙였다. 첨부 자체는 됐지만 `macos-release` 작업은 실패로 표시됐다: 실행 스크립트 끝에 `if-no-files-found: error` 한 줄(다른 액션의 옵션)이 섞여 있어 셸이 명령으로 실행했다. 그 줄을 지웠다.
- 릴리스 직전 `main`에서 수동 실행한 빌드(Windows, macOS + `macos_tests`)는 모두 통과했다.
- 아직 안 한 것: 사람이 Mac에서 이 dmg를 받아 여는 확인, 그리고 실제 릴리스를 통한 앱 안 업데이트(다음 버전이 나와야 시험할 수 있다).

## 2026-10-10 — 0.5.84 설치 확인과 최소 macOS 버전 오류

- 릴리스의 `Grainy-0.5.84-macos-arm64.dmg`(105,018,422바이트)를 `gh release download`로 받아 SHA-256이 깃헙의 값과 같음을 확인하고, 디스크 이미지에서 `/Applications`로 복사해 설치했다(M1 Pro, macOS 15.0.1). 서명 검증과 자가 점검(133개 항목, GPU 일치 포함)이 통과한다. 이 방법으로 받은 파일에는 격리 표시가 붙지 않아, 브라우저로 받았을 때의 "그래도 열기" 절차는 확인하지 못했다.
- **0.5.84의 macOS 앱은 `LSMinimumSystemVersion`이 15.0이다.** 릴리스 안내와 README는 "macOS 14 이상"이라고 한다. macOS 14에서는 Finder가 이 앱을 열지 않을 것이다(해 보지는 못했다. 빌드 서버의 자가 점검은 프로그램 파일을 직접 실행하므로 macOS 14에서도 통과한다).
  - 원인: `tools/build_macos.py`가 묶인 파일 하나하나의 빌드 버전 중 가장 높은 값을 적었다. PySide6 6.11.2의 바인딩 라이브러리(`QtCore.abi3.so`, `libshiboken6…dylib` 등 9개)는 휠이 `macosx_13_0`이라고 밝히면서도 파일에는 15.0이 들어 있다.
  - 고친 것(`mac` 브랜치): 묶인 패키지가 휠 태그로 밝힌 버전과 Python 자체의 빌드 버전 중 가장 높은 값을 적는다. 깃헙이 만든 0.5.84 앱에 이 규칙을 적용하면 14.0이 나온다(numpy·lensfunpy가 `macosx_14_0`, 빌드 서버의 Python이 11.0). 적을 값이 빌드하는 Mac의 macOS보다 높으면 빌드가 멈춘다.
  - 이미 나간 0.5.84 파일은 그대로다. 다음 릴리스부터 14.0으로 나간다. macOS 14에서 실제로 열리는지는 그 버전으로 누군가 확인해야 한다.
- `main`(b2e908a)을 `mac`에 합쳤다. `main`이 `mac`의 내용을 모두 담고 있어 충돌한 파일은 `main` 쪽을 따랐고, 합친 직후의 `mac`은 `main`과 내용이 같다.

## 2026-10-10 — 0.5.85: 하이라이트·그림자 반응 교체 (Windows 쪽에서)

- `luma/tone_response.py`의 하이라이트·그림자 반응을 새로 설계한 곡선으로 바꿨다. 값은 자체 기준으로만 고른다(다른 프로그램의 출력에 맞추지 않는다).
- 셰이더가 받는 자료(257칸 표와 계수 맵)의 형식은 그대로다. `analyse()`는 이제 기준점(`pivot`)을 돌려주지 않는다.
- **톤 셰이더에 한 줄이 늘었다**: 변화량을 `clamp(change, -luma*0.5, (1-luma)*0.5)`로 제한한다(`tone_response.REACH`). `native/gpu_compute.cpp`와 `native/gpu_compute_metal.mm`에 같은 줄을 넣었다. Metal 쪽은 Windows에서 컴파일해 볼 수 없어 GitHub의 macOS 빌드와 시험으로만 확인했다. Mac에서 다음에 열 때 `tests/test_tone_response.py`의 GPU 일치 시험을 실제 기기에서 한 번 돌려 볼 것.
- GPU 모듈 규격 번호는 바꾸지 않았다(구조체와 함수가 그대로다).
- 설정 메뉴에 `Grainy 정보…`가 생겼다(`MainWindow.show_about`, `desktop_session.py`에서 추가). macOS에서도 다른 명령처럼 설정 메뉴 안에 둔다.
- `mac` 브랜치의 최소 macOS 버전 수정(7fbc608, `tools/build_macos.py`)을 합쳤다. 0.5.85부터 들어간다.

## 2026-10-10 — 공개 저장소의 이력을 새로 시작함

- 0.5.85부터 공개 저장소의 `main`은 새 커밋 하나에서 다시 시작한다. 이전 커밋·태그·릴리스(0.5.75~0.5.84)는 지웠고, `mac` 브랜치도 새 `main`에서 다시 만들었다.
- **맥 쪽 작업 폴더는 그대로 이어 쓸 수 없다.** 새로 받거나, 커밋하지 않은 작업이 없는지 확인한 뒤 `git fetch origin` → `git checkout mac` → `git reset --hard origin/mac`으로 맞춘다. 예전 커밋 위에서 만든 작업을 그대로 push하면 지운 이력이 다시 올라가므로 하지 않는다.
- 일부 개발용 측정 도구(`tools/validate_*`, `benchmark_*`, `compare_*`)는 공개본에 넣지 않는다. 검사(`tools/check.py`)와 빌드에는 필요 없다.
- 문서와 주석은 Grainy 자체의 동작만 적는다. 다른 프로그램과의 비교나 "~처럼"이라는 설명은 쓰지 않는다(다른 프로그램의 카탈로그·프리셋 가져오기 기능 설명과 상표 고지는 예외).

## 2026-10-10 — README를 영어 첫 화면으로 바꾸고 화면 캡처를 넣음 (맥 쪽에서, `main`에 직접)

- 소유자의 지시로 이 변경은 `mac`을 거치지 않고 `main`에 바로 올렸다. **Windows 작업 폴더에도 가져가야 한다.** 가져가지 않고 `main`을 다시 내보내면 사라진다.
- `README.md`는 영어다. 한국어는 `README.ko.md`로 옮겼고 두 문서 맨 위에서 서로 연결된다. 코드 서명 정책(영문)은 `README.md`에 그대로 있고 한국어 문서는 그곳을 가리킨다.
- 두 문서에 추가한 것: 배지 세 개, 대표 화면, 직접 확인한 다섯 가지(무료·로컬, 공개 RAW 샘플 15종 중 14종, GPU 가속 시간, 필름 스캔 도구, 카탈로그 가져오기), 화면 네 장과 보정 전후 한 장. macOS 삭제 방법과 macOS 빌드 도구도 한 줄씩 넣었다. 피드백에 실리는 사양은 "운영체제 버전"으로 고쳤다(macOS에서는 macOS 버전이 간다).
- 화면은 `docs/screenshots/`에 있다(한국어·영어 화면 각 4장, 공통 1장, 합계 2.9MB). 실제 프로그램을 스크립트로 조작해 찍었고, 사진은 raw.pixls.us의 CC0 샘플이다. 배포본에는 들어가지 않는다(`assets` 밖).
- 먼지 제거 화면은 넣지 못했다. 공개할 수 있는 필름 스캔이 없다.
- 영어 화면에서 한글로 남던 두 곳(사진 목록의 "불러오는 중…", 새 마스크의 기본 이름)은 `mac` 브랜치에서 고쳤다(코드 변경이라 `main`에는 올리지 않았다). 0.5.85 소스로 다시 빌드한 Metal 라이브러리도 그 커밋에 있다. 0.5.85는 M1 Pro에서 전체 검사 1,251개(5개 건너뜀)와 GPU 검사를 통과한다.
- **실수 기록**: 위의 "이력을 새로 시작함" 안내를 읽기 전에 예전 `mac` 위에서 만든 커밋을 `mac`에 push해, 지운 이력이 몇 분 동안 `mac` 브랜치에 다시 연결됐다. 새 `main`에서 다시 만든 `mac`으로 강제로 덮어써 되돌렸다. 예전 태그는 push하지 않았다.
