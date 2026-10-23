@echo off
REM Build a Windows executable for server/app.py using PyInstaller.
REM Run from the risk_monitor/ directory on Windows (or under Wine) with Python installed.
REM
REM IMPORTANT -- why the build is deployed to an ASCII-only path:
REM This repo lives under a directory whose name contains Hangul.
REM Windows Defender Firewall stores its per-program
REM allow rules by executable path, and a non-ASCII path is mangled when the
REM rule is written, so the rule never matches the running process. The
REM firewall then silently drops inbound SYNs from the device client -- the
REM server starts fine, binds 0.0.0.0:5000 and answers on localhost, but the
REM raspberry-pi client can never connect. (Running `python -m server.app`
REM works because the matching process is python.exe under an ASCII-only
REM Anaconda path.) Copying the frozen app to an ASCII-only directory makes
REM the firewall rule match and the client connects.

set "DEPLOY_DIR=%USERPROFILE%\pressure-server"

pip install -r server\requirements-build.txt || goto :error
pyinstaller server.spec --noconfirm || goto :error

echo.
echo Build complete: dist\pressure-server\pressure-server.exe

echo Deploying to %DEPLOY_DIR% (ASCII-only path, required for the firewall) ...
REM Replace only the app files: the frozen server stores its data in
REM %DEPLOY_DIR%\press_warnings (next to the exe), which must survive a rebuild.
if exist "%DEPLOY_DIR%\_internal" rmdir /s /q "%DEPLOY_DIR%\_internal" || goto :error
if exist "%DEPLOY_DIR%\pressure-server.exe" del /q "%DEPLOY_DIR%\pressure-server.exe" || goto :error
if not exist "%DEPLOY_DIR%" mkdir "%DEPLOY_DIR%" || goto :error
xcopy /e /i /q /y "dist\pressure-server\*" "%DEPLOY_DIR%\" >nul || goto :error

REM Register the inbound firewall rule for the deployed exe. Also needed after
REM every rebuild: Windows disables an existing allow rule once the target
REM file's contents change. Requires admin -- skip with a warning if not.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo Deployed: %DEPLOY_DIR%\pressure-server.exe
    echo.
    echo WARNING: not running as Administrator, so the Windows Firewall rule
    echo could not be refreshed. If device clients can't reach the server,
    echo re-run this script as Administrator, or allow
    echo %DEPLOY_DIR%\pressure-server.exe in Windows Defender Firewall.
    goto :eof
)

echo Refreshing Windows Firewall rule ...
netsh advfirewall firewall delete rule name="pressure-server" >nul 2>&1
netsh advfirewall firewall add rule name="pressure-server" dir=in action=allow program="%DEPLOY_DIR%\pressure-server.exe" enable=yes protocol=TCP || goto :error
netsh advfirewall firewall add rule name="pressure-server" dir=in action=allow program="%DEPLOY_DIR%\pressure-server.exe" enable=yes protocol=UDP || goto :error
echo Firewall rule refreshed.

echo.
echo Run the server with: %DEPLOY_DIR%\pressure-server.exe
goto :eof

:error
echo Build failed.
exit /b 1
