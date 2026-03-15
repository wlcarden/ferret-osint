#!/usr/bin/env bash
# Bootstrap the OSINT agent workspace.
# Run this after cloning the repo on a new machine.
set -euo pipefail

echo "=== OSINT Agent Bootstrap ==="

# --- Check prerequisites ---
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 required"; exit 1; }
command -v docker >/dev/null 2>&1 || echo "WARNING: docker not found — Neo4j/ArchiveBox won't run"
command -v curl >/dev/null 2>&1 || { echo "ERROR: curl required"; exit 1; }

# --- Python environment ---
echo "[1/5] Creating Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip

echo "[2/5] Installing Python dependencies..."
pip install -e ".[dev]"

# --- System tools ---
echo "[3/5] Installing system tools..."
if command -v apt-get >/dev/null 2>&1; then
    echo "  Checking ExifTool..."
    command -v exiftool >/dev/null 2>&1 || sudo apt-get install -y libimage-exiftool-perl
    echo "  Checking jq..."
    command -v jq >/dev/null 2>&1 || sudo apt-get install -y jq
elif command -v brew >/dev/null 2>&1; then
    command -v exiftool >/dev/null 2>&1 || brew install exiftool
    command -v jq >/dev/null 2>&1 || brew install jq
fi

# --- PhoneInfoga (pre-built binary, go install is broken) ---
echo "[4/5] Installing PhoneInfoga..."
if command -v phoneinfoga >/dev/null 2>&1; then
    echo "  PhoneInfoga already installed: $(phoneinfoga version)"
else
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  PI_ARCH="x86_64" ;;
        aarch64) PI_ARCH="arm64" ;;
        armv7l)  PI_ARCH="armv7" ;;
        *)       PI_ARCH="" ;;
    esac
    OS=$(uname -s)
    if [ -n "$PI_ARCH" ]; then
        PI_URL="https://github.com/sundowndev/phoneinfoga/releases/latest/download/phoneinfoga_${OS}_${PI_ARCH}.tar.gz"
        echo "  Downloading from $PI_URL"
        curl -sL "$PI_URL" | tar xz -C /tmp phoneinfoga
        sudo mv /tmp/phoneinfoga /usr/local/bin/
        echo "  Installed: $(phoneinfoga version)"
    else
        echo "  Unsupported architecture: $ARCH — skipping PhoneInfoga"
    fi
fi

# --- Environment ---
echo "[5/5] Setting up environment..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "  Created .env from .env.example — fill in your API keys"
else
    echo "  .env already exists — skipping"
fi

# --- Data directories ---
mkdir -p data/{raw,processed,exports} evidence screenshots logs

echo ""
echo "=== Bootstrap complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your API keys"
echo "  2. Run: docker compose up -d    (starts Neo4j + ArchiveBox)"
echo "  3. Neo4j browser: http://localhost:7474"
echo "  4. ArchiveBox:    http://localhost:8000"
echo "  5. Activate env:  source .venv/bin/activate"
