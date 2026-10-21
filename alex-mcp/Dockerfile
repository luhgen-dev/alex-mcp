ARG BUILD_FROM=ghcr.io/home-assistant/base:3.24
FROM $BUILD_FROM

ARG BUILD_VERSION=0.5.44
ARG BUILD_ARCH=amd64
ARG BUILD_COMMIT=unknown

LABEL \
  io.hass.version="$BUILD_VERSION" \
  io.hass.type="app" \
  io.hass.arch="$BUILD_ARCH"

RUN apk add --no-cache \
      ca-certificates \
      unzip \
      nodejs \
      npm \
      python3 \
      py3-pip \
      py3-virtualenv \
      ffmpeg \
      tesseract-ocr \
      tesseract-ocr-data-eng \
      tesseract-ocr-data-msa \
      tesseract-ocr-data-tam \
      poppler-utils \
      font-dejavu \
      whisper.cpp

RUN test -n "$(find /usr/share/fonts -type f -name 'DejaVuSans.ttf' -print -quit)" \
 && test -n "$(find /usr/share/fonts -type f -name 'DejaVuSans-Bold.ttf' -print -quit)"

# Pinned official runtime-only tunnel client; no admin/provisioning CLI.
# Existing add-on supports amd64 only. Verify before extracting/executing.
RUN wget -q -O /tmp/tunnel.zip https://github.com/openai/tunnel-client/releases/download/v0.0.16/tunnel-client-runtime-v0.0.16-linux-amd64.zip \
 && echo "f8f48017e752810c2c6f02065cdc4ca1ad045b91a6d045cbab144b957fdcbdb1  /tmp/tunnel.zip" | sha256sum -c - \
 && mkdir -p /usr/local/share/tunnel-client \
 && unzip -q /tmp/tunnel.zip -d /usr/local/share/tunnel-client \
 && cp /usr/local/share/tunnel-client/tunnel-client-runtime /usr/local/bin/tunnel-client-runtime \
 && chmod 755 /usr/local/bin/tunnel-client-runtime \
 && /usr/local/bin/tunnel-client-runtime --version \
 && rm /tmp/tunnel.zip

WORKDIR /app

COPY app/package.json ./
RUN npm install --omit=dev

COPY app/requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/alex-venv \
 && /opt/alex-venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/alex-venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

COPY app/ ./
COPY rootfs/ /

RUN chmod -R a+x /etc/services.d /etc/cont-init.d

ENV PATH="/opt/alex-venv/bin:$PATH"
ENV ALEX_APP_VERSION="$BUILD_VERSION"
ENV ALEX_BUILD_COMMIT="$BUILD_COMMIT"
ENV ALEX_DATA_DIR="/data"
ENV PYTHONUNBUFFERED=1

