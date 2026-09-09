ARG BUILD_FROM
FROM ${BUILD_FROM}

# Vendored bambu-bridge source. HA builds the add-on with this folder as the
# Docker build context, so the reviewed source and notices are copied
# in by homeassistant/addon/sync-bridge-source.sh before building.
COPY bridge/ /opt/bridge/

# Pin the build toolchain to avoid non-deterministic resolution of hatchling
# on every build.  These versions must stay in sync with pyproject.toml's
# [build-system] requires = ["hatchling>=1.27,<2"].
# Upgrading: bump the version pins here AND in pyproject.toml together.
#
# musl build deps are installed as a virtual package and removed afterward so
# they don't bloat the final image.  They are needed only if any runtime
# dependency lacks a pre-built musllinux wheel (e.g. cryptography's cffi layer).
ARG PIP_VERSION=24.3.1
ARG HATCHLING_VERSION=1.27.0

RUN apk add --no-cache --virtual .build-deps \
        gcc musl-dev python3-dev libffi-dev \
    && pip install --no-cache-dir \
        "pip==${PIP_VERSION}" \
        "hatchling==${HATCHLING_VERSION}" \
    && pip install --no-cache-dir --no-build-isolation /opt/bridge \
    && apk del .build-deps

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
