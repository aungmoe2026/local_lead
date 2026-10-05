@echo off
REM Double-click this file to launch Local Leads.
REM First run takes a minute or two to set up; after that it starts in a few seconds.

cd /d "%~dp0backend"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python is not installed. Get it from https://www.python.org/downloads/ and try again.
    echo IMPORTANT: when installing, tick the box that says "Add Python to PATH".
    pause
    exit /b 1
)

if not exist ".venv" (
    echo First-time setup: creating a private Python environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

for /f "delims=" %%h in ('python -c "import hashlib;print(hashlib.md5(open('requirements.txt','rb').read()).hexdigest())"') do set "REQS_HASH=%%h"

set "STORED_HASH="
if exist ".venv\.deps_hash" set /p STORED_HASH=<".venv\.deps_hash"

if not "%REQS_HASH%"=="%STORED_HASH%" (
    echo First-time setup: installing dependencies, this can take a minute...
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo.
        echo Dependency install failed - see errors above. Not marking setup complete.
        echo Fix the issue above and run this file again.
        pause
        exit /b 1
    )

    echo First-time setup: downloading a headless browser for JS-heavy sites ^(~150-300MB, one-time^)...
    python -m playwright install chromium
    if %errorlevel% neq 0 (
        echo Browser download failed - owner/contact lookups on JS-heavy sites will be skipped this run.
        echo Retry any time with: python -m playwright install chromium
    )

    > ".venv\.deps_hash" echo %REQS_HASH%
)

if not exist ".env" (
    if exist ".env.example" copy ".env.example" ".env" >nul
    echo.
    echo Optional: Companies House owner-name lookups need a free API key.
    echo Get one at https://developer.company-information.service.gov.uk
    echo then paste it into backend\.env as COMPANIES_HOUSE_API_KEY=your-key
    echo Skip this and the tool still works fine without it.
    echo.
)

echo Starting Local Leads...
start "" cmd /c "timeout /t 2 >nul && start http://127.0.0.1:8000"
uvicorn app:app --host 127.0.0.1 --port 8000

pause
