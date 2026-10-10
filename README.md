# Grainy

Windows와 macOS용 사진 보정·관리 프로그램입니다. 원본 사진은 건드리지 않고, 보정 내용을 보관함(카탈로그)에 따로 저장합니다.

*A non-destructive photo editor and library for Windows and macOS (Korean and English UI). Originals are never modified; edits live in a local catalog.*

## 받기와 실행

1. Releases에서 `Grainy-<버전>-windows-x64.zip`을 받아 원하는 폴더에 풉니다.
2. `Grainy.exe`를 실행합니다.
3. 코드 서명이 없어 처음 실행할 때 Windows가 "알 수 없는 게시자" 경고를 보여 줍니다.

- Windows 10/11 64비트가 필요합니다.
- 보관함은 `%LOCALAPPDATA%\Luma\Library`에 만들어집니다.

### macOS (Apple Silicon)

1. Releases에서 `Grainy-<버전>-macos-arm64.dmg`를 받아 열고, Grainy를 응용 프로그램 폴더로 끌어다 놓습니다.
2. 처음 실행하면 macOS가 확인되지 않은 개발자의 앱이라며 막습니다. `시스템 설정 → 개인정보 보호 및 보안`에서 "그래도 열기"를 한 번 누르면 됩니다. Apple 개발자 서명과 공증이 없기 때문입니다.

- Apple Silicon(M1 이후) Mac과 macOS 14 이상이 필요합니다. Intel Mac은 지원하지 않습니다.
- 보관함과 설정은 `~/Library/Application Support/Grainy/`에 만들어집니다.
- macOS 버전은 0.5.84가 첫 배포입니다. 자동 시험은 통과했지만 여러 사람의 Mac에서 써 본 것은 아닙니다. 문제는 피드백 버튼으로 알려 주세요.

## 삭제

- 압축을 푼 Grainy 폴더를 지우면 프로그램이 삭제됩니다. 레지스트리나 시스템 설정은 건드리지 않습니다.
- 보정 기록까지 지우려면 `%LOCALAPPDATA%\Luma`(보관함)와 `%LOCALAPPDATA%\Grainy`(설정) 폴더도 지웁니다. 사진 원본은 이 폴더들에 없습니다.

## 기능

- **보관함**: 폴더 가져오기, 등록한 폴더의 새 사진 자동 가져오기, 별점·색 라벨·키워드, 컬렉션과 스마트 컬렉션, 폴더 위치 복구
- **RAW 현상**: 주요 카메라 RAW, 카메라 프로파일(DCP), 렌즈 보정(Lensfun)
- **보정**: 노출·톤 커브·채널 커브·색상 혼합·컬러 그레이딩, 자동 톤, 자동 색(색 틀어짐 보정), 필름 색온도 변환(텅스텐↔데이라이트)
- **디테일**: 선명도, 노이즈 제거, 그레인, 비네팅
- **부분 보정**: 마스크(브러시, 선형·방사형 그레이디언트, 색상·광도 범위, 추가·빼기·교차), 힐링, 먼지 제거
- **내보내기**: JPEG, PNG, TIFF, AVIF, JPEG XL
- **피드백**: 화면 위쪽 버튼으로 문제점이나 개선 아이디어를 바로 보낼 수 있습니다
- **업데이트**: 프로그램 안에서 새 버전을 확인하고 바로 교체합니다
- **기타**: 편집 이력과 실행 취소, Lightroom 카탈로그·프리셋 가져오기, 카탈로그 백업(Google Drive 폴더 포함), GPS 지도

자세한 사용법은 프로그램에 들어 있는 도움말(`README.html`)에 있습니다.

## 개인정보

- 사용 통계를 수집하지 않습니다. 인터넷으로 나가는 것은 아래 두 가지뿐이고, 둘 다 직접 요청했을 때만 동작합니다.
- **피드백 보내기**: 보내기를 누를 때만 입력한 글, Grainy 버전, PC 사양 한 줄(Windows 버전, 그래픽카드 이름)이 전송됩니다. 사진, 파일 이름과 경로, 사용자 이름은 보내지 않습니다.
- **업데이트**: "업데이트 확인"을 누르거나 "새 버전 자동 확인"을 켰을 때만 GitHub에서 최신 버전 정보를 읽습니다(자동 확인은 처음 실행할 때 켤지 묻고, 설정에서 끌 수 있습니다). 사용자에 관한 정보는 보내지 않습니다.

## 소스에서 실행

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

그래픽카드 가속과 색 변환용 C++ 모듈은 `tools/build_native_*.py`로 빌드합니다(Visual Studio C++ 도구 필요). 테스트는 `python tools/check.py`입니다.

## 라이선스

Grainy 자체의 코드는 [MIT 라이선스](LICENSE)입니다. 사용·수정·재배포·상업적 이용이 자유롭고, 저작권 표시와 라이선스 문구만 유지하면 됩니다. "Grainy" 이름과 로고는 라이선스 대상이 아닙니다.

함께 배포되는 외부 부품은 각자의 라이선스를 따릅니다. Qt/PySide6(LGPL-3.0), LibRaw, OpenCV, Lensfun 등을 사용합니다. 전체 목록과 조건은 [DISTRIBUTION.md](DISTRIBUTION.md)에, 고지문은 배포본의 `THIRD_PARTY_LICENSES` 폴더에 있습니다.

Adobe, Lightroom, Photoshop은 Adobe Inc.의 상표입니다. Grainy는 Adobe가 만들거나 후원·보증한 프로그램이 아니며 Adobe와 관련이 없습니다.

*Adobe, Lightroom and Photoshop are trademarks of Adobe Inc. Grainy is an independent project: it is not made, sponsored or endorsed by Adobe and is not affiliated with it.*

## Code signing policy

Release binaries are **not signed yet**. This project has applied for free code signing for open source projects:

Free code signing provided by [SignPath.io](https://signpath.io), certificate by [SignPath Foundation](https://signpath.org).

- **Committers and reviewers:** [pastorius9](https://github.com/pastorius9)
- **Approvers:** [pastorius9](https://github.com/pastorius9)
- Only binaries built from this repository are signed. Third-party libraries shipped with Grainy keep their own publishers' signatures.
- Signed releases are built from source by GitHub Actions ([build.yml](.github/workflows/build.yml)) on a GitHub-hosted runner; the native libraries are compiled there as well.

**Privacy policy:** This program will not transfer any information to other networked systems unless specifically requested by the user or the person installing or operating it. There are two such requests. The Feedback dialog: pressing Send transmits the text you typed, the Grainy version and a one-line system description (Windows version and graphics adapter name). Updates: "Check for Updates" and, only if you switch it on when asked at first start (it can be switched off in Settings), a once-a-day check at start-up read the latest release information from GitHub; installing an update downloads the release file from GitHub. Photos, file names, paths and user names are never transmitted.
