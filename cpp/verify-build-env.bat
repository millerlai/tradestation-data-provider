@echo off
setlocal EnableDelayedExpansion
rem ===========================================================================
rem  verify-build-env.bat - check every prerequisite for building TS2Python
rem  and say exactly what to do about anything that is missing.
rem
rem      cd cpp
rem      verify-build-env.bat
rem
rem  Exit code 0 = ready to build, 1 = something needs fixing.
rem  Changes nothing; run setup-build-env.bat to fix what this reports.
rem
rem  (Named verify-build-env rather than verify because VERIFY is a cmd.exe
rem  builtin - typing `verify` would run that instead of this script.)
rem ===========================================================================

cd /d "%~dp0"
set "CPP_ROOT=%CD%"
set "VCPKG_DIR=%CPP_ROOT%\build-tools\vcpkg"
set "FAILED=0"
set "WARNED=0"

echo.
echo === TS2Python build environment ===
echo repo cpp\ : %CPP_ROOT%
echo.

rem ---------------------------------------------------------------------------
echo [1] Visual Studio with the C++ toolset
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "VS_PATH="
if exist "%VSWHERE%" (
    for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2^>nul`) do set "VS_PATH=%%i"
)
if defined VS_PATH (
    echo     [ OK ] !VS_PATH!
) else (
    echo     [FAIL] no Visual Studio with "Desktop development with C++"
    echo            FIX: install it, or run the Visual Studio Installer,
    echo                 choose Modify, and tick that workload.
    set /a FAILED+=1
)

rem ---------------------------------------------------------------------------
echo.
echo [2] platform toolset required by the project
set "WANT_TOOLSET=v145"
if defined TS2PythonToolset set "WANT_TOOLSET=%TS2PythonToolset%"
set "TOOLSET_FOUND="
set "TOOLSETS_AVAILABLE="
if defined VS_PATH (
    for /d %%v in ("!VS_PATH!\MSBuild\Microsoft\VC\*") do (
        for /d %%t in ("%%~fv\Platforms\Win32\PlatformToolsets\*") do (
            set "TOOLSETS_AVAILABLE=!TOOLSETS_AVAILABLE! %%~nxt"
            if /i "%%~nxt"=="!WANT_TOOLSET!" set "TOOLSET_FOUND=1"
        )
    )
)
if defined TOOLSET_FOUND (
    echo     [ OK ] !WANT_TOOLSET! is installed
) else (
    if defined VS_PATH (
        echo     [FAIL] the project asks for !WANT_TOOLSET!, which is not installed
        echo            available:!TOOLSETS_AVAILABLE!
        echo            FIX: build with one you have, e.g.
        echo                 msbuild TS2Python.sln /p:TS2PythonToolset=v143 /p:Configuration=Release /p:Platform=x86
        echo                 ^(v145 ships with Visual Studio 2026; v143 with 2022^)
        set /a FAILED+=1
    ) else (
        echo     [SKIP] no Visual Studio to check against
    )
)

rem ---------------------------------------------------------------------------
echo.
echo [3] vcpkg submodule
if exist "%VCPKG_DIR%\scripts\buildsystems\msbuild\vcpkg.props" (
    echo     [ OK ] %VCPKG_DIR%
) else (
    echo     [FAIL] not checked out: %VCPKG_DIR%
    echo            FIX: git submodule update --init --recursive
    echo                 ^(or run setup-build-env.bat, which does it for you^)
    set /a FAILED+=1
)

rem ---------------------------------------------------------------------------
echo.
echo [4] vcpkg.exe bootstrapped
if exist "%VCPKG_DIR%\vcpkg.exe" (
    echo     [ OK ] %VCPKG_DIR%\vcpkg.exe
) else (
    echo     [FAIL] not bootstrapped
    echo            FIX: run setup-build-env.bat
    set /a FAILED+=1
)

rem ---------------------------------------------------------------------------
rem  The check that maps directly onto
rem      error C1083: Cannot open include file: 'zmq.hpp'
rem ---------------------------------------------------------------------------
echo.
rem  The triplet appears twice on purpose: the outer directory is the
rem  per-triplet install root, the inner is the triplet folder within it.
rem  See vcpkg-local.props for why the roots must stay separate.
echo [5] dependency headers for x86-windows
set "ZMQ_HPP=%CPP_ROOT%\vcpkg_installed\x86-windows\x86-windows\include\zmq.hpp"
if exist "%ZMQ_HPP%" (
    echo     [ OK ] vcpkg_installed\x86-windows\x86-windows\include\zmq.hpp
) else (
    echo     [FAIL] zmq.hpp missing - this is what produces
    echo            "error C1083: Cannot open include file: 'zmq.hpp'"
    echo            FIX: run setup-build-env.bat
    set /a FAILED+=1
)

rem ---------------------------------------------------------------------------
rem  A stale machine-global integration is not fatal any more - the projects
rem  import vcpkg from the submodule and set VCPkgLocalAppDataDisabled - but
rem  it is worth naming, because it silently breaks every OTHER vcpkg project
rem  on the machine and it is what broke this one before vcpkg-local.props.
rem ---------------------------------------------------------------------------
echo.
echo [6] machine-global vcpkg integration ^(informational^)
set "GLOBAL_PROPS=%LOCALAPPDATA%\vcpkg\vcpkg.user.props"
if not exist "%GLOBAL_PROPS%" (
    echo     [ OK ] none - this repo does not need it
) else (
    rem The generated file is one <Import Condition="Exists('<path>') ..."
    rem line; the path sits between the first pair of single quotes, which is
    rem the only part of it that can be split out reliably in batch.
    set "GLOBAL_TARGET="
    for /f "usebackq tokens=2 delims='" %%p in (`findstr /c:"Exists(" "%GLOBAL_PROPS%"`) do set "GLOBAL_TARGET=%%p"
    if not defined GLOBAL_TARGET (
        echo     [ OK ] present but points nowhere in particular
    ) else (
        if exist "!GLOBAL_TARGET!" (
            echo     [ OK ] present and valid, and ignored here by design
            echo            ^(points at !GLOBAL_TARGET!^)
        ) else (
            echo     [WARN] present but points at a path that no longer exists:
            echo            !GLOBAL_TARGET!
            echo            Harmless for THIS repo - vcpkg-local.props resolves
            echo            vcpkg from the submodule instead. But any other
            echo            vcpkg project on this machine will fail with C1083
            echo            until you re-run `vcpkg integrate install` from a
            echo            vcpkg checkout that exists, or `vcpkg integrate
            echo            remove` to clear it.
            set /a WARNED+=1
        )
    )
)

rem ---------------------------------------------------------------------------
rem  Same family of problem as [6]: a VCPKG_ROOT left over from another
rem  project. vcpkg detects its own root from the exe path and overrides it,
rem  so the build is correct either way - but it prints a warning on every
rem  invocation, which is noise that looks like a real problem.
rem ---------------------------------------------------------------------------
echo.
echo [7] VCPKG_ROOT environment variable ^(informational^)
if not defined VCPKG_ROOT (
    echo     [ OK ] unset - vcpkg resolves its root from the submodule
) else (
    if /i "%VCPKG_ROOT%"=="%VCPKG_DIR%" (
        echo     [ OK ] points at this repo's submodule
    ) else (
        echo     [WARN] points somewhere else:
        echo            %VCPKG_ROOT%
        echo            vcpkg ignores it and uses the submodule anyway, so the
        echo            build is correct - but it prints a "mismatched
        echo            VCPKG_ROOT" warning every run. Clear it with:
        echo                setx VCPKG_ROOT ""
        echo            then open a new terminal.
        set /a WARNED+=1
    )
)

rem ---------------------------------------------------------------------------
echo.
echo [8] CMake ^(optional - only for the preset build^)
where cmake >nul 2>&1
if "%ERRORLEVEL%"=="0" (
    for /f "usebackq tokens=3" %%v in (`cmake --version 2^>nul ^| findstr /r /c:"^cmake version"`) do echo     [ OK ] cmake %%v on PATH
) else (
    if defined VS_PATH (
        set "VS_CMAKE=!VS_PATH!\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
        if exist "!VS_CMAKE!" (
            echo     [ OK ] bundled with Visual Studio ^(not on PATH^)
            echo            !VS_CMAKE!
        ) else (
            echo     [WARN] not found. Only needed for `cmake --preset x86-release`;
            echo            the Visual Studio solution build does not use it.
            set /a WARNED+=1
        )
    ) else (
        echo     [WARN] not found on PATH
        set /a WARNED+=1
    )
)

rem ---------------------------------------------------------------------------
echo.
if not "%FAILED%"=="0" (
    echo === NOT READY - %FAILED% check^(s^) failed ===
    echo Run setup-build-env.bat, then this script again.
    endlocal
    exit /b 1
)
if not "%WARNED%"=="0" (
    echo === READY to build - %WARNED% warning^(s^) above, none blocking ===
) else (
    echo === READY to build ===
)
echo.
echo   Visual Studio :  open TS2Python.sln, pick Release^|x86, Build
echo   CMake         :  cmake --preset x86-release
echo                    cmake --build --preset x86-release
echo.
endlocal
exit /b 0
