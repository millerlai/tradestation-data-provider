@echo off
setlocal EnableDelayedExpansion
rem ===========================================================================
rem  build.bat - build TS2Python.sln for x86 and x64 in one go.
rem
rem      build.bat                 Release, both platforms  (the default)
rem      build.bat Debug           Debug, both platforms
rem      build.bat all             Debug + Release, both platforms
rem      build.bat --x86           Release, x86 only
rem      build.bat Debug --x64     Debug, x64 only
rem      build.bat --rebuild       force a full rebuild rather than incremental
rem
rem  x86 is the one that matters: TradeStation is a 32-bit process and loads
rem  only the Win32 DLL. x64 exists to smoke-test the C++ itself and must
rem  never be deployed - see the warning printed at the end.
rem
rem  Dependencies install themselves. MSBuild runs vcpkg in manifest mode at
rem  the start of each build, so a triplet that has never been built just
rem  takes longer the first time. Run setup-build-env.bat only if this
rem  script tells you to.
rem ===========================================================================

cd /d "%~dp0"
set "CPP_ROOT=%CD%"
set "VCPKG_DIR=%CPP_ROOT%\build-tools\vcpkg"

set "CONFIGS=Release"
set "PLATFORMS=x86 x64"
set "TARGET=Build"

:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="Release"   ( set "CONFIGS=Release"       & shift & goto :parse_args )
if /i "%~1"=="Debug"     ( set "CONFIGS=Debug"         & shift & goto :parse_args )
if /i "%~1"=="all"       ( set "CONFIGS=Debug Release" & shift & goto :parse_args )
if /i "%~1"=="--x86"     ( set "PLATFORMS=x86"         & shift & goto :parse_args )
if /i "%~1"=="--x64"     ( set "PLATFORMS=x64"         & shift & goto :parse_args )
if /i "%~1"=="--rebuild" ( set "TARGET=Rebuild"        & shift & goto :parse_args )
if /i "%~1"=="-r"        ( set "TARGET=Rebuild"        & shift & goto :parse_args )
if /i "%~1"=="--help"    goto :usage
if /i "%~1"=="-h"        goto :usage
if /i "%~1"=="/?"        goto :usage
echo unknown option: %~1
echo.
goto :usage
:args_done

rem ---------------------------------------------------------------------------
rem  Locate MSBuild. Not assumed to be on PATH - it is not, unless the shell
rem  was opened as a Developer Command Prompt.
rem ---------------------------------------------------------------------------
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "MSBUILD="
if exist "%VSWHERE%" (
    for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.Component.MSBuild -find MSBuild\**\Bin\MSBuild.exe 2^>nul`) do (
        if not defined MSBUILD set "MSBUILD=%%i"
    )
)
if not defined MSBUILD (
    echo ERROR: MSBuild not found.
    echo        Run verify-build-env.bat - it reports what is missing and how
    echo        to install it.
    goto :fail
)

rem ---------------------------------------------------------------------------
rem  One prerequisite MSBuild cannot recover from on its own.
rem ---------------------------------------------------------------------------
if not exist "%VCPKG_DIR%\scripts\buildsystems\msbuild\vcpkg.props" (
    echo ERROR: the vcpkg submodule is not checked out.
    echo        FIX: run setup-build-env.bat
    goto :fail
)

echo.
echo === Building TS2Python.sln ===
echo msbuild        : %MSBUILD%
echo target         : %TARGET%
echo configurations : %CONFIGS%
echo platforms      : %PLATFORMS%
echo.

set "FAILED=0"
set "BUILT="
for %%c in (%CONFIGS%) do (
    for %%p in (%PLATFORMS%) do (
        echo --- %%c ^| %%p ---
        rem The SOLUTION platform is x86/x64. The projects call it Win32/x64;
        rem passing Win32 here fails with MSB4126.
        "%MSBUILD%" "%CPP_ROOT%\TS2Python.sln" /t:%TARGET% /p:Configuration=%%c /p:Platform=%%p /v:minimal /nologo /m
        if errorlevel 1 (
            echo     FAILED: %%c ^| %%p
            set /a FAILED+=1
        ) else (
            echo     ok: %%c ^| %%p
            set "BUILT=!BUILT! %%c/%%p"
        )
        echo.
    )
)

rem ---------------------------------------------------------------------------
echo === Output ===
for %%c in (%CONFIGS%) do (
    for %%p in (%PLATFORMS%) do (
        if /i "%%p"=="x86" ( set "OUTDIR=%CPP_ROOT%\%%c" ) else ( set "OUTDIR=%CPP_ROOT%\x64\%%c" )
        if exist "!OUTDIR!\TS2Python.dll" (
            echo   %%c ^| %%p
            for %%f in ("!OUTDIR!\*.dll" "!OUTDIR!\*.exe") do echo       %%~ff
        )
    )
)

echo.
if not "%FAILED%"=="0" (
    echo === BUILD FAILED - %FAILED% configuration^(s^) ===
    echo Read the first error above. If it mentions zmq.hpp or libzmq, run
    echo verify-build-env.bat for a diagnosis.
    goto :fail
)

echo === BUILD OK ===
echo.
echo Deploy to TradeStation from the x86 Release output ONLY:
echo   %CPP_ROOT%\Release\
echo The x64 binaries cannot be loaded by TradeStation, which is a 32-bit
echo process. Copying them there produces a "cannot find DLL" style failure
echo that says nothing about the real cause.
echo.
endlocal
exit /b 0

:usage
echo.
echo Usage: build.bat [Release^|Debug^|all] [--x86^|--x64] [--rebuild]
echo.
echo   ^(no args^)   Release, x86 and x64
echo   Debug        Debug, x86 and x64
echo   all          Debug and Release, x86 and x64
echo   --x86        restrict to x86 ^(what TradeStation loads^)
echo   --x64        restrict to x64 ^(developer smoke testing only^)
echo   --rebuild    full rebuild instead of incremental
echo.
endlocal
exit /b 2

:fail
echo.
endlocal
exit /b 1
