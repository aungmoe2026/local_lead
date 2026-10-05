#!/bin/bash
# Double-click this file (or run `./start_mac_linux.sh`) to launch Local Leads.
# First run takes a minute or two to set up; after that it starts in a few seconds.

set -e
cd "$(dirname "$0")/backend"

if ! command -v python3 &> /dev/null; then
    echo "Python 3 is not installed. Get it from https://www.python.org/downloads/ and try again."
    read -p "Press Enter to close..."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "First-time setup: creating a private Python environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

REQS_HASH=$(python3 -c "import hashlib;print(hashlib.md5(open('requirements.txt','rb').read()).hexdigest())")
STORED_HASH=""
if [ -f ".venv/.deps_hash" ]; then
    STORED_HASH=$(cat ".venv/.deps_hash")
fi

if [ "$REQS_HASH" != "$STORED_HASH" ]; then
    echo "First-time setup: installing dependencies (this can take a minute)..."
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt

    echo "First-time setup: downloading a headless browser for JS-heavy sites (~150-300MB, one-time)..."
    python3 -m playwright install chromium || echo "Browser download failed - owner/contact lookups on JS-heavy sites will be skipped this run. Retry any time with: python3 -m playwright install chromium"

    echo "$REQS_HASH" > ".venv/.deps_hash"
fi

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp ".env.example" ".env"
    fi
    echo ""
    echo "Optional: Companies House owner-name lookups need a free API key."
    echo "Get one at https://developer.company-information.service.gov.uk"
    echo "then paste it into backend/.env as COMPANIES_HOUSE_API_KEY=your-key"
    echo "Skip this and the tool still works fine without it."
    echo ""
fi

echo "Starting Local Leads..."
( sleep 1.5 && python3 -m webbrowser "http://127.0.0.1:8000" ) &

uvicorn app:app --host 127.0.0.1 --port 8000
