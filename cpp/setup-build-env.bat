@echo off
setlocal EnableDelayedExpansion
rem ===========================================================================
rem  setup-build-env.bat - prepare this machine to build TS2Python.
rem
rem  Run once after cloning, and again whenever cpp\vcpkg.json changes.
rem  Safe to re-run: every step is idempotent and skips work already done.
rem
rem      cd cpp
rem      setup-build-env.bat            REM x86 only (what TradeStation loads)
rem      setup-build-env.bat --with-x64 REM also install x64 for local testing
rem
rem  What it does NOT do: `vcpkg integrate install`. That is a machine-global
rem  setting pointing at one vcpkg checkout, and this repo deliberately does
rem  not rely on it - see vcpkg-local.props. If you ran it previously for some
rem  other project it is ignored here, which is the point.
rem
rem  Check the result with verify-build-env.bat.
rem ===========================================================================

cd /d "%~dp0"
set "CPP_ROOT=%CD%"
set "VCPKG_DIR=%CPP_ROOT%\build-tools\vcpkg"
set "VCPKG_EXE=%VCPKG_DIR%\vcpkg.exe"
set "WITH_X64="
if /i "%~1"=="--with-x64" set "WITH_X64=1"

echo.
echo === TS2Python build environment setup ===
echo repo cpp\ : %CPP_ROOT%
echo.

rem ---------------------------------------------------------------------------
rem 1. Visual Studio with the C++ toolset
rem ---------------------------------------------------------------------------
echo [1/4] Visual Studio C++ toolset
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
    echo       FAIL: vswhere.exe not found - no Visual Studio installer present.
    echo             Install "Visual Studio 2022 or newer" with the workload
    echo             "Desktop development with C++".
    echo             https://visualstudio.microsoft.com/downloads/
    goto :fail
)
set "VS_PATH="
for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2^>nul`) do set "VS_PATH=%%i"
if not defined VS_PATH (
    echo       FAIL: Visual Studio is installed but the C++ toolset is not.
    echo             Open the Visual Studio Installer, Modify, and tick
    echo             "Desktop development with C++".
    goto :fail
)
echo       OK: %VS_PATH%

rem ---------------------------------------------------------------------------
rem 2. vcpkg submodule
rem ---------------------------------------------------------------------------
echo.
echo [2/4] vcpkg submodule
if not exist "%VCPKG_DIR%\bootstrap-vcpkg.bat" (
    echo       not checked out - running git submodule update --init
    pushd "%CPP_ROOT%\.."
    git submodule update --init --recursive
    set "GIT_RC=!ERRORLEVEL!"
    popd
    if not "!GIT_RC!"=="0" (
        echo       FAIL: git submodule update failed ^(rc=!GIT_RC!^).
        echo             Is git on PATH, and was this a git clone rather than
        echo             a downloaded .zip? A .zip has no submodules; clone with
        echo             git clone --recurse-submodules ^<url^>
        goto :fail
    )
)
if not exist "%VCPKG_DIR%\bootstrap-vcpkg.bat" (
    echo       FAIL: still missing after submodule update: %VCPKG_DIR%
    goto :fail
)
echo       OK: %VCPKG_DIR%

rem ---------------------------------------------------------------------------
rem 3. Bootstrap vcpkg.exe
rem ---------------------------------------------------------------------------
echo.
echo [3/4] vcpkg.exe
if exist "%VCPKG_EXE%" (
    echo       OK: already bootstrapped
) else (
    echo       bootstrapping ^(downloads a compiler-built vcpkg, ~1 min^)...
    call "%VCPKG_DIR%\bootstrap-vcpkg.bat" -disableMetrics
    if not exist "%VCPKG_EXE%" (
        echo       FAIL: bootstrap did not produce %VCPKG_EXE%
        goto :fail
    )
    echo       OK: %VCPKG_EXE%
)

rem ---------------------------------------------------------------------------
rem 4. Install the manifest dependencies
rem
rem     Each triplet gets its OWN install root, matching what MSBuild derives
rem     when VcpkgInstalledDir is left at its default. Hence the triplet name
rem     appearing twice in the resulting path - see vcpkg-local.props. Sharing
rem     one root across triplets makes the x86 and x64 builds delete each
rem     other's packages.
rem ---------------------------------------------------------------------------
echo.
echo [4/4] dependencies from vcpkg.json ^(cppzmq, zeromq^)
echo       triplet x86-windows - this is what TradeStation loads
"%VCPKG_EXE%" install --triplet x86-windows --x-install-root="%CPP_ROOT%\vcpkg_installed\x86-windows"
if not "%ERRORLEVEL%"=="0" (
    echo       FAIL: vcpkg install failed for x86-windows.
    goto :fail
)
if defined WITH_X64 (
    echo       triplet x64-windows - developer smoke testing only
    "%VCPKG_EXE%" install --triplet x64-windows --x-install-root="%CPP_ROOT%\vcpkg_installed\x64-windows"
    if not "!ERRORLEVEL!"=="0" (
        echo       FAIL: vcpkg install failed for x64-windows.
        goto :fail
    )
)

if not exist "%CPP_ROOT%\vcpkg_installed\x86-windows\x86-windows\include\zmq.hpp" (
    echo.
    echo       FAIL: vcpkg reported success but zmq.hpp is not where the build
    echo             expects it:
    echo             vcpkg_installed\x86-windows\x86-windows\include\
    goto :fail
)

echo.
echo === Setup complete ===
echo.
echo Build with either toolchain:
echo.
echo   Visual Studio :  open cpp\TS2Python.sln, pick Release^|x86, Build
echo   CMake         :  cmake --preset x86-release
echo                    cmake --build --preset x86-release
echo.
echo Confirm the environment any time with:  verify-build-env.bat
echo.
endlocal
exit /b 0

:fail
echo.
echo === Setup FAILED - see the message above ===
endlocal
exit /b 1
