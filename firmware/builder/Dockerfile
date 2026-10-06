# Firmware builder for the PaperMono shopping-list device (papermono-shopping-list/firmware, PlatformIO + espressif32).
# Keeps PlatformIO and its ~2 GB of toolchains out of the host: the image holds only Python and `pio`; the platform,
# toolchains and framework are downloaded on first build into the /cache volume and reused afterwards.
# Use through build.sh (or `just firmware` from the repo root). Nothing secret is baked in: the settings are passed as environment at run time.
FROM python:3.13-slim

ARG PLATFORMIO_VERSION=6.1.19
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "platformio==${PLATFORMIO_VERSION}"

# World-writable so a named volume mounted here is usable by whatever --user the wrapper passes.
RUN mkdir /cache /work /out && chmod 1777 /cache /work /out
ENV PLATFORMIO_CORE_DIR=/cache \
    PLATFORMIO_CHECK_PLATFORMIO_INTERVAL=0 \
    PLATFORMIO_SETTING_ENABLE_TELEMETRY=no \
    HOME=/tmp

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
