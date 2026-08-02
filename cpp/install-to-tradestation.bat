@echo off
setlocal EnableDelayedExpansion
rem ===========================================================================
rem  install-to-tradestation.bat - copy the built DLLs into the TradeStation
rem  install so the EasyLanguage indicator can load them.
rem
rem      cd cpp
rem      build.bat --x86
rem      install-to-tradestation.bat
rem
rem  Looks for TradeStation under the usual roots on C: and D:. If it finds
rem  nothing it asks for the path and shows an example of what it wants.
rem  Nothing is copied until you confirm.
rem
rem  Which build gets installed is decided by the PE header of the platform
rem  executable found in the target folder, NOT by the bitness of Windows:
rem  every shipping TradeStation is a 32-bit process on a 64-bit OS, so
rem  "install the x64 build because the machine is x64" is exactly the
rem  mistake this checks for. The DLL is checked the same way before it is
rem  copied, so a mixed-up output folder is caught here rather than as a
rem  load failure inside EasyLanguage that names no cause.
rem ===========================================================================

cd /d "%~dp0"
set "CPP_ROOT=%CD%"

rem  What marks a folder as a TradeStation Program folder. There is no
rem  TradeStation.exe: ORPlat.exe is the platform that hosts EasyLanguage,
rem  TSDev.exe the EasyLanguage editor. Any one of them is enough to
rem  recognise the folder and to read the architecture from.
set "TS_EXES=ORPlat.exe TSDev.exe TSCLUtil.exe"

if /i "%~1"=="--help" goto :usage
if /i "%~1"=="-h"     goto :usage
if /i "%~1"=="/?"     goto :usage
if not "%~1"=="" (
    echo unknown option: %~1
    echo.
    goto :usage
)

echo.
echo === Install TS2Python into TradeStation ===
echo.

rem ---------------------------------------------------------------------------
rem  [1] Destination: the TradeStation "Program" folder.
rem ---------------------------------------------------------------------------
set "PF86=%ProgramFiles(x86)%"
set "PF64=%ProgramFiles%"

set "NROOT=0"
call :add_root "%PF86%"
call :add_root "%PF64%"
call :add_root "C:\Program Files (x86)"
call :add_root "C:\Program Files"
call :add_root "C:"
call :add_root "D:\Program Files (x86)"
call :add_root "D:\Program Files"
call :add_root "D:"

set "NFOUND=0"
for /l %%i in (1,1,%NROOT%) do call :scan_root "!ROOT_%%i!"

set "DEST="
if %NFOUND% GTR 1 goto :pick_one
if %NFOUND% EQU 1 goto :only_one

echo No TradeStation installation was found under the usual folders on C: or D:.
call :ask_path
goto :have_dest

:only_one
set "DEST=!FOUND_1!"
echo Found TradeStation: !DEST!
goto :have_dest

:pick_one
echo Found more than one TradeStation installation:
for /l %%i in (1,1,%NFOUND%) do echo     [%%i] !FOUND_%%i!
echo.
set "PICK="
set /p "PICK=Pick a number (blank to cancel): "
if not defined PICK goto :cancelled
for /l %%i in (1,1,%NFOUND%) do if "!PICK!"=="%%i" set "DEST=!FOUND_%%i!"
if not defined DEST (
    echo     not one of the numbers listed above.
    echo.
    goto :pick_one
)

:have_dest
if not defined DEST goto :cancelled

rem ---------------------------------------------------------------------------
rem  [2] Which architecture does THAT TradeStation load?
rem ---------------------------------------------------------------------------
set "ARCH=unknown"
call :find_marker "%DEST%"
if defined MARKER call :pe_arch "!MARKER!"
set "TS_ARCH=!ARCH!"

if "%TS_ARCH%"=="arm64" (
    echo.
    echo ERROR: !MARKER! is an arm64 binary.
    echo        This project builds x86 and x64 only.
    goto :fail
)
if "%TS_ARCH%"=="x86" goto :arch_known
if "%TS_ARCH%"=="x64" goto :arch_known

rem  Nothing to read, or no PowerShell to read it with. x86 is the right
rem  guess - it is what every shipping TradeStation is - but say so rather
rem  than letting it look like a measurement.
echo.
echo NOTE: could not read the architecture of %TS_EXES% in
echo           %DEST%
echo       Assuming x86, which is what TradeStation has always shipped as.
set "TS_ARCH=x86"
goto :arch_assumed

:arch_known
echo TradeStation is %TS_ARCH%, read from !MARKER!
:arch_assumed
echo   ^(Windows here is %PROCESSOR_ARCHITECTURE%, but the process bitness is
echo   what decides the DLL, not the OS^)

rem ---------------------------------------------------------------------------
rem  [3] Source: the matching build output.
rem      build.bat / Visual Studio write x86 to cpp\Release and x64 to
rem      cpp\x64\Release; CMake writes x86 to cpp\build\x86-release\Release
rem      and has no x64 preset.
rem ---------------------------------------------------------------------------
set "SRC_PREBUILT=%CPP_ROOT%\prebuilt\%TS_ARCH%-windows"
if "%TS_ARCH%"=="x64" (
    set "SRC_VS=%CPP_ROOT%\x64\Release"
    set "SRC_CMAKE="
    set "BUILD_HINT=build.bat --x64"
) else (
    set "SRC_VS=%CPP_ROOT%\Release"
    set "SRC_CMAKE=%CPP_ROOT%\build\x86-release\Release"
    set "BUILD_HINT=build.bat --x86"
)

rem  A local build wins over the shipped binary: someone who just built is
rem  installing what they built, not what was committed months ago.
set "SRC="
set "USING_PREBUILT="
if exist "!SRC_VS!\TS2Python.dll" set "SRC=!SRC_VS!"
if not defined SRC if defined SRC_CMAKE if exist "!SRC_CMAKE!\TS2Python.dll" set "SRC=!SRC_CMAKE!"
if not defined SRC if exist "!SRC_PREBUILT!\TS2Python.dll" (
    set "SRC=!SRC_PREBUILT!"
    set "USING_PREBUILT=1"
)

if not defined SRC (
    echo.
    echo ERROR: no %TS_ARCH% DLL to install.
    echo        looked in: !SRC_VS!
    if defined SRC_CMAKE echo                   !SRC_CMAKE!
    echo                   !SRC_PREBUILT!
    echo        FIX: !BUILD_HINT!
    goto :fail
)

if defined USING_PREBUILT (
    echo.
    echo Using the prebuilt DLL shipped with the repo - nothing was built here.
    echo   see prebuilt\README.md
)

if /i "!SRC!"=="!SRC_VS!" if defined SRC_CMAKE if exist "!SRC_CMAKE!\TS2Python.dll" (
    echo.
    echo WARNING: both build outputs exist. Installing the solution build:
    echo              !SRC_VS!
    echo          The CMake output is ignored. Delete the one you are not
    echo          using, so a stale DLL can never be the one deployed.
)

rem  Confirm the DLL really is what the folder name claims. Catches a build
rem  that wrote to the wrong place, and a hand-copied file.
call :pe_arch "!SRC!\TS2Python.dll"
if "%ARCH%"=="unknown" (
    echo.
    echo NOTE: could not read the architecture of TS2Python.dll; trusting the
    echo       output folder.
) else if /i not "%ARCH%"=="%TS_ARCH%" (
    echo.
    echo ERROR: !SRC!\TS2Python.dll is %ARCH%, but this TradeStation is %TS_ARCH%.
    echo        Loading it fails with a "cannot find DLL" style message that
    echo        says nothing about the real cause.
    echo        FIX: delete that folder and run !BUILD_HINT!
    goto :fail
)

rem  The ZeroMQ runtime has a versioned filename that moves with the pinned
rem  vcpkg revision, so every .dll beside TS2Python.dll gets copied rather
rem  than a hard-coded list.
set "ZMQ_COUNT=0"
for %%f in ("!SRC!\libzmq*.dll") do set /a ZMQ_COUNT+=1

rem ---------------------------------------------------------------------------
rem  [4] Two things that make the copy fail halfway if not checked first.
rem      Order matters: the folder has to be writable before "this file cannot
rem      be opened for writing" can be read as "TradeStation has it loaded".
rem ---------------------------------------------------------------------------
set "PROBE=%DEST%\ts2python-install-probe.tmp"
break > "%PROBE%" 2>nul
if not exist "%PROBE%" (
    echo.
    rem  !DEST!, not %%DEST%%: the usual path holds "(x86)", and a bare
    rem  percent-expansion inside a block closes the block on that paren.
    echo ERROR: cannot write to
    echo            !DEST!
    echo        Program Files needs an elevated shell: right-click cmd.exe or
    echo        Windows Terminal, choose "Run as administrator", then run this
    echo        script again.
    goto :fail
)
del "%PROBE%" >nul 2>&1

rem  TradeStation being open is not on its own a reason to refuse. Windows
rem  locks the DLL only once EasyLanguage has actually loaded it - which
rem  happens when a chart or study importing it runs - so installing over a
rem  TradeStation that has not touched the indicator works. The files
rem  themselves are therefore tested, not the process list; the process list
rem  is only read to explain a lock when there is one.
set "RUNNING="
for %%e in (%TS_EXES%) do (
    tasklist /fi "imagename eq %%e" 2>nul | find /i "%%e" >nul && set "RUNNING=!RUNNING! %%e"
)

set "LOCKED="
for %%f in ("!SRC!\*.dll") do call :lock_check "%DEST%\%%~nxf"
if defined LOCKED (
    echo.
    echo ERROR: these files cannot be opened for writing, so replacing them
    echo        would fail halfway:
    echo           !LOCKED!
    if defined RUNNING (
        echo        TradeStation is running ^(!RUNNING! ^) and has the DLL
        echo        loaded. Close it - or just the charts using the indicator -
        echo        and run this again.
    )
    goto :fail
)
if defined RUNNING (
    echo.
    echo NOTE: TradeStation is running ^(!RUNNING! ^), but nothing holds the
    echo       DLL open, so it can still be replaced.
)

rem ---------------------------------------------------------------------------
rem  The DLL is linked against the dynamic CRT, so the Visual C++
rem  Redistributable is a hard requirement. When it is missing, EasyLanguage
rem  says only that the DLL could not be loaded and names nothing - which is
rem  why this is worth reporting here instead. 32-bit runtime DLLs live in
rem  SysWOW64 on a 64-bit Windows; 64-bit ones in System32.
rem ---------------------------------------------------------------------------
if "%TS_ARCH%"=="x64" (
    set "CRT_DIR=%SystemRoot%\System32"
    set "CRT_FILES=MSVCP140.dll MSVCP140_ATOMIC_WAIT.dll VCRUNTIME140.dll VCRUNTIME140_1.dll"
    set "CRT_URL=https://aka.ms/vs/17/release/vc_redist.x64.exe"
) else (
    set "CRT_DIR=%SystemRoot%\SysWOW64"
    set "CRT_FILES=MSVCP140.dll MSVCP140_ATOMIC_WAIT.dll VCRUNTIME140.dll"
    set "CRT_URL=https://aka.ms/vs/17/release/vc_redist.x86.exe"
)
if not exist "!CRT_DIR!\" set "CRT_DIR=%SystemRoot%\System32"
set "CRT_MISSING="
for %%f in (!CRT_FILES!) do if not exist "!CRT_DIR!\%%f" set "CRT_MISSING=!CRT_MISSING! %%f"
if defined CRT_MISSING (
    echo.
    echo WARNING: the Visual C++ %TS_ARCH% runtime looks incomplete. Missing from
    echo          !CRT_DIR! :
    echo             !CRT_MISSING!
    echo          Install "Microsoft Visual C++ 2015-2022 Redistributable":
    echo             !CRT_URL!
    echo          The DLLs below still copy fine; EasyLanguage is where it
    echo          would fail, with an error that names no cause.
)

rem ---------------------------------------------------------------------------
rem  [5] Show exactly what will happen, then ask.
rem ---------------------------------------------------------------------------
echo.
echo === Install plan ===
echo   arch : %TS_ARCH%
echo   from : !SRC!
echo   to   : %DEST%
echo.
for %%f in ("!SRC!\*.dll") do (
    if exist "%DEST%\%%~nxf" (
        echo     %%~nxf  -^> replaces the existing file
    ) else (
        echo     %%~nxf  -^> new
    )
)
if "%ZMQ_COUNT%"=="0" (
    echo.
    echo WARNING: no libzmq*.dll next to TS2Python.dll. Without the ZeroMQ
    echo          runtime, EasyLanguage fails to load the DLL with an error
    echo          that does not name the missing file.
)

rem  Overwriting the DLL a working TradeStation already loads is the one
rem  irreversible thing this script does, so it is asked as its own question
rem  with both files' dates in view - not folded into a generic yes/no.
set "REPLACING="
if exist "%DEST%\TS2Python.dll" set "REPLACING=1"
if defined REPLACING (
    echo.
    echo TS2Python.dll is already installed there:
    for %%f in ("%DEST%\TS2Python.dll") do echo     installed : %%~tf   %%~zf bytes
    for %%f in ("!SRC!\TS2Python.dll") do echo     new       : %%~tf   %%~zf bytes
)
echo.

set "GO="
if defined REPLACING (
    set /p "GO=Replace it, and copy the files listed above? [y/N]: "
) else (
    set /p "GO=Copy these files? [y/N]: "
)
if /i not "!GO!"=="y" goto :cancelled

rem ---------------------------------------------------------------------------
echo.
set "COPIED=0"
set "COPY_FAILED=0"
for %%f in ("!SRC!\*.dll") do (
    copy /y "%%~ff" "%DEST%\" >nul
    if errorlevel 1 (
        echo     FAILED: %%~nxf
        set /a COPY_FAILED+=1
    ) else (
        echo     ok: %%~nxf
        set /a COPIED+=1
    )
)

echo.
if not "%COPY_FAILED%"=="0" (
    echo === INSTALL FAILED - %COPY_FAILED% file^(s^) not copied ===
    echo   Nothing held those files when they were checked above, so something
    echo   took them in between - usually TradeStation loading the indicator.
    echo   Close it and run this again.
    goto :fail
)

echo === INSTALLED - %COPIED% file^(s^) ===
echo   %DEST%
echo.
echo NEXT - RECOMPILE THE INDICATOR TOO. The DLL and the indicator are one
echo   unit: the indicator binds EL_Init3 and checks EL_DllVersion, so a chart
echo   still running the previously compiled indicator stops publishing rather
echo   than sending anything wrong.
echo.
echo   Open EL\TS2Python_Exporter.el ^(EasyLanguage source^) in the TradeStation
echo   Development Environment, Verify ^(F3^) to compile it, then apply it to a
echo   chart. TradeStation loads this DLL on the next chart that uses it.
echo.
endlocal
exit /b 0

rem ===========================================================================
rem  Subroutines
rem ===========================================================================

rem  add_root <dir> - remember a folder to look for TradeStation* under,
rem  skipping duplicates. %ProgramFiles(x86)% is usually one of the literal
rem  C: paths listed as well.
:add_root
set "R=%~1"
if not defined R goto :eof
if "%R:~-1%"=="\" set "R=%R:~0,-1%"
if not exist "%R%\" goto :eof
set "DUP="
for /l %%i in (1,1,%NROOT%) do if /i "!ROOT_%%i!"=="!R!" set "DUP=1"
if defined DUP goto :eof
set /a NROOT+=1
set "ROOT_!NROOT!=!R!"
goto :eof

rem  scan_root <dir> - one level deep only. A full drive walk would take
rem  minutes; every real install is <root>\TradeStation <version>\Program.
:scan_root
for /d %%d in ("%~1\TradeStation*") do call :consider "%%~fd"
goto :eof

rem  consider <TradeStation install dir> - accept it only if a platform
rem  executable is really there, so a leftover empty folder is not offered.
:consider
call :find_marker "%~1\Program"
if not defined MARKER goto :eof
set "CAND=%~1\Program"
set "DUP="
for /l %%i in (1,1,%NFOUND%) do if /i "!FOUND_%%i!"=="!CAND!" set "DUP=1"
if defined DUP goto :eof
set /a NFOUND+=1
set "FOUND_!NFOUND!=!CAND!"
goto :eof

rem  lock_check <file> - add the file's name to LOCKED when it exists and
rem  cannot be opened for writing. Opening for append and closing writes
rem  nothing, so the file is left as it was; a DLL the loader has mapped
rem  refuses the open, which is exactly the case that would make the copy
rem  below fail halfway.
:lock_check
if not exist "%~1" goto :eof
2>nul (
    >>"%~1" (call )
) || set "LOCKED=!LOCKED! %~nx1"
goto :eof

rem  find_marker <dir> - set MARKER to the first TS_EXES entry that exists in
rem  <dir>, or leave it empty. Both "is this a TradeStation folder" and "what
rem  architecture is it" are answered from the same file.
:find_marker
set "MARKER="
for %%e in (%TS_EXES%) do if not defined MARKER if exist "%~1\%%e" set "MARKER=%~1\%%e"
goto :eof

rem  pe_arch <file> - set ARCH to x86 / x64 / arm64 / unknown from the PE
rem  Machine field: e_lfanew at 0x3C, then the word at PE+4. Batch cannot
rem  read binary, so PowerShell does it; ARCH stays "unknown" if that fails,
rem  and the caller decides what to do about it.
:pe_arch
set "ARCH=unknown"
if not exist "%~1" goto :eof
set "MACHINE="
for /f "usebackq delims=" %%m in (`powershell -NoProfile -Command "try{$f=[IO.File]::OpenRead('%~1');$b=New-Object byte[] 4;$f.Position=0x3C;[void]$f.Read($b,0,4);$f.Position=[BitConverter]::ToInt32($b,0)+4;[void]$f.Read($b,0,2);$f.Close();'{0:x4}' -f [BitConverter]::ToUInt16($b,0)}catch{'error'}" 2^>nul`) do set "MACHINE=%%m"
if /i "!MACHINE!"=="014c" set "ARCH=x86"
if /i "!MACHINE!"=="8664" set "ARCH=x64"
if /i "!MACHINE!"=="aa64" set "ARCH=arm64"
goto :eof

rem  ask_path - prompt until the answer points at a real folder, or the user
rem  gives up with a blank line.
:ask_path
echo.
echo Enter the TradeStation "Program" folder - the one holding ORPlat.exe.
echo For example:
echo     C:\Program Files ^(x86^)\TradeStation 10.0\Program
echo.
set "ANSWER="
set /p "ANSWER=Path (blank to cancel): "
if not defined ANSWER goto :eof
set ANSWER=!ANSWER:"=!
if not defined ANSWER goto :eof
if "!ANSWER:~-1!"=="\" set "ANSWER=!ANSWER:~0,-1!"

call :find_marker "!ANSWER!"
if defined MARKER (
    set "DEST=!ANSWER!"
    goto :eof
)
rem  Accept the install root too - that is the path most people have to hand.
call :find_marker "!ANSWER!\Program"
if defined MARKER (
    set "DEST=!ANSWER!\Program"
    echo     using !ANSWER!\Program
    goto :eof
)
if not exist "!ANSWER!\" (
    echo     no such folder: !ANSWER!
    goto :ask_path
)
echo     WARNING: that folder exists, but none of %TS_EXES% is in it.
set "ANYWAY="
set /p "ANYWAY=Install there anyway? [y/N]: "
if /i "!ANYWAY!"=="y" (
    set "DEST=!ANSWER!"
    goto :eof
)
goto :ask_path

rem ===========================================================================
:usage
echo.
echo Usage: install-to-tradestation.bat
echo.
echo   Copies the built DLLs into the TradeStation Program folder, picking
echo   the x86 or x64 output to match the platform executable found there
echo   ^(%TS_EXES%^).
echo   Build them first with:  build.bat --x86
echo   Takes no options; it asks before copying anything.
echo.
endlocal
exit /b 2

:cancelled
echo.
echo Cancelled - nothing was copied.
endlocal
exit /b 1

:fail
echo.
endlocal
exit /b 1
