# Windows x64 Arena onedir build

This is a local build recipe for a ZIP containing `StockConda/StockConda.exe`.
After extraction, add that executable as a UCI engine in Arena. The target
computer needs neither Python nor a compiler. Keep the complete extracted
`StockConda` directory together; `_internal`, `opening_book.pkl`, and `licenses`
belong beside the executable. The book is off by default and is read from this
directory when `setoption name Book value true` is sent, regardless of Arena's
working directory.

## Build prerequisites

On a **Windows x64 build machine**, use Python 3.12 x64 and the MSVC C/C++
Build Tools compatible with that Python. Run commands in a Visual Studio
Developer PowerShell or another shell where the compiler is available. Build
in a new isolated virtual environment. The source export must come from the
same committed tree as the accompanying source ZIP. Do not build this Windows
artifact in the Linux container: PyInstaller does not cross-compile.

For the published `v0.2.3.5.dev1` binary, download its matching source ZIP from the release page and extract it beside the ZIP. Enter the extracted `StockConda-0.2.3.5.dev1` directory. The release source manifest and ZIP must remain byte-for-byte identical; later documentation commits on `main` are not inputs to this reproduction recipe. Use a Windows Python 3.12 x64 interpreter:

```powershell
py -3.12 -m venv ..\windows-build-venv
..\windows-build-venv\Scripts\python.exe -m pip install -e . "pyinstaller==6.22.3"
$msvcNotices = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Licenses\1033'
..\windows-build-venv\Scripts\python.exe scripts\build_windows_onedir.py --source-zip ..\StockConda-0.2.3.5.dev1-source.zip --msvc-notices $msvcNotices --output ..\windows-build-output
```

`pip install -e .` compiles the three Cython extensions in the export. The
builder checks that the resulting `.pyd` modules came from that same source
tree. It then runs PyInstaller in console/onedir mode, copies the book and
license material, attaches the complete source ZIP, and runs a real EXE UCI
smoke from another working directory before producing the final ZIP.
Set `$msvcNotices` to the installed Visual Studio Build Tools license folder
containing `Redist.txt` and `ThirdPartyNotices.txt`; its locale number depends
on the installation. The builder copies both Microsoft notices into the ZIP.
`windows-build-output/uci-smoke.log` retains its protocol trace. The script
refuses to emit a ZIP when native modules, license files, source hashes, or
the UCI smoke fail. Build dependencies are downloaded only at build time.
They are never downloaded by `StockConda.exe` at runtime.

The final archive is `windows-build-output/StockConda-Windows-x64-onedir.zip`.
Extract it, then point Arena to `StockConda/StockConda.exe`. Keep the source
ZIP and license folder inside the extracted directory when sharing it.

## Additional release acceptance

The published developer pre-release passed local checks of `BUILD_MANIFEST.json`, the collected
dependency license inventory, the full UCI trace, the three native `.pyd`
modules in `_internal`, and the archive hashes. Arena 3.5.1 registered and played with the engine. A clean Windows x64 VM check without Python or a compiler remains outstanding. For broader acceptance, check
`uci`, `isready`, book enabled/disabled, `go`, `stop`, `quit`, EOF, startup from
an unrelated working directory, and no network requirement. The scripted
smoke is a local gate and does not replace those clean-machine checks. No Elo
or performance assertion follows from this packaging work.
