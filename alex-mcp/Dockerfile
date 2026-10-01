ARG BUILD_FROM=ghcr.io/home-assistant/base:3.24
FROM $BUILD_FROM

ARG BUILD_VERSION=0.5.4
ARG BUILD_ARCH=amd64

LABEL \
  io.hass.version="$BUILD_VERSION" \
  io.hass.type="app" \
  io.hass.arch="$BUILD_ARCH"

RUN apk add --no-cache \
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
ENV ALEX_DATA_DIR="/data"
ENV PYTHONUNBUFFERED=1
