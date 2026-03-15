FROM python:3.12-slim

# System dependencies for various OSINT tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    wget \
    jq \
    libimage-exiftool-perl \
    chromium \
    && rm -rf /var/lib/apt/lists/*

# PhoneInfoga (pre-built binary — go install is broken due to missing embedded web assets)
RUN ARCH=$(dpkg --print-architecture) && \
    case "$ARCH" in \
        amd64) PI_ARCH="x86_64" ;; \
        arm64) PI_ARCH="arm64" ;; \
        armhf) PI_ARCH="armv7" ;; \
        *) PI_ARCH="" ;; \
    esac && \
    if [ -n "$PI_ARCH" ]; then \
        curl -sL "https://github.com/sundowndev/phoneinfoga/releases/latest/download/phoneinfoga_Linux_${PI_ARCH}.tar.gz" \
        | tar xz -C /usr/local/bin phoneinfoga; \
    fi

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# Copy source code (not data)
COPY src/ src/
COPY config/ config/
COPY scripts/ scripts/

ENTRYPOINT ["python", "-m", "osint_agent"]
