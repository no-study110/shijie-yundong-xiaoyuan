@echo off
rem Smoke check entry point.
rem Keep this file ASCII-only: cmd.exe parses batch files with the console code
rem page, so UTF-8 Chinese comments get mangled into stray commands.
rem Chinese output comes from smoke.py, which is why chcp 65001 is set first.
chcp 65001 >nul

rem %~dp0 ends with a backslash. Quoting it verbatim turns \" into an escaped
rem quote (the path itself becomes the command), so append "." to stay safe.
cd /d "%~dp0."

rem Args are passed through, e.g.  check.cmd --skip-tests
where python >nul 2>&1
if errorlevel 1 goto use_py
python smoke.py %*
exit /b %errorlevel%

:use_py
where py >nul 2>&1
if errorlevel 1 goto no_python
py -3 smoke.py %*
exit /b %errorlevel%

:no_python
echo [ERROR] Python not found. Install Python 3.8+ and add it to PATH.
exit /b 127
