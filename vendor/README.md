# Native color conversion source

`lcms2-2.19` is the unmodified MIT-licensed LittleCMS 2.19.0 source used by the
imagecodecs baseline. `SOURCE.json` records the upstream tag, downloaded archive
SHA-256 and every retained source file hash. Only include/src and notices are
retained; no optional plugins are used.

`tools/build_native_color.py` validates these hashes and builds an x64 DLL using
the installed MSVC toolchain, `/O2 /MD /fp:precise`. The DLL and build signature go
to `assets/native`. `Build Luma.ps1` invokes it before packaging. Compatible
existing builds are reused only if their source signature and output hash match.

Runtime transforms use private LittleCMS contexts, relative colorimetric intent,
RGB float32 input/output, and `cmsFLAGS_NOCACHE`. The native library contains no
Luma edits. Python owns transform lifetime and limits the shared cache and worker
queue. Other precision formats keep the imagecodecs implementation. Runtime
notices are copied to `THIRD_PARTY_LICENSES/LittleCMS` in releases.
