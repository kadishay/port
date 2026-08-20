# Bug Triage & Solve Agent — Railway deployment image
#
# Single image, single Railway service: Vikunja backend (Go) + Vikunja
# frontend (Vue3/Vite) + this repo's Python agent, all started together by
# start.sh (see Task 3) at container startup. The Vikunja *source* is not
# baked into this image — it's cloned into a mounted volume at container
# startup so `git push` has a real, writable, credentialed clone (see
# start.sh). Only this repo's `agent/` code ships in the image itself.

# --- Step 1: Base image ---------------------------------------------------
# Microsoft's official Playwright Python image ships Chromium + all of its
# system-library deps (libnss3, fonts, etc.) preinstalled, which is exactly
# why this plan chose a single Dockerfile over Railway's Nixpacks builder
# (Nixpacks has no clean apt-get equivalent for those deps).
#
# Tag pinned to v1.62.0-jammy: as of this writing, pip's latest published
# `playwright` package is also 1.62.0 (checked via `pypi.org/pypi/playwright/json`),
# and this repo's requirements.txt pins only a floor (`playwright>=1.45.0`),
# so `pip install -r requirements.txt` resolves to the newest release available
# at build time. Keeping this tag's version in sync with whatever that
# resolves to is what avoids Playwright re-downloading a mismatched browser
# build at runtime (the base image bakes browser binaries into
# /ms-playwright at exactly the tag's playwright version). If requirements.txt
# ever pins `playwright` to an exact version, update this tag to match.
FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

# apt-get installs below (Go's build-essential, Node via NodeSource) run
# non-interactively; the base image's own ARG DEBIAN_FRONTEND does not
# persist into this derived image (ARG, unlike ENV, doesn't carry across
# `FROM`), so it's set again here.
ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# --- Step 2: Install Go + mage ---------------------------------------------
# Version pinned to the exact `go 1.26.4` directive in
# /Users/kadishay/Code/vikunja/go.mod (checked directly against the fork this
# deployment targets). build-essential (gcc) is required because Vikunja
# depends on github.com/mattn/go-sqlite3, a CGO-based SQLite driver — `mage
# build` will fail without a C compiler on PATH.
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/* && \
    curl -fsSL https://go.dev/dl/go1.26.4.linux-amd64.tar.gz -o /tmp/go.tar.gz && \
    tar -C /usr/local -xzf /tmp/go.tar.gz && \
    rm /tmp/go.tar.gz

ENV PATH="/usr/local/go/bin:/root/go/bin:${PATH}"
ENV GOPATH="/root/go"

RUN go install github.com/magefile/mage@latest

# --- Step 3: Install Node + pnpm --------------------------------------------
# Node major version 24 matches Vikunja frontend's package.json
# ("engines": { "node": ">=24.0.0" }); pnpm version matches package.json's
# "packageManager": "pnpm@11.21.0" exactly, activated via corepack.
# corepack is installed/updated explicitly (not just `corepack enable`)
# because Node has been shrinking what ships bundled by default across
# recent majors — installing it explicitly avoids depending on exactly what
# a given Node 24.x point release bundles.
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/* && \
    npm install -g corepack@latest && \
    corepack enable && \
    corepack prepare pnpm@11.21.0 --activate

# --- Step 4: Install Python deps --------------------------------------------
# The `playwright` pip package installs into the base image's existing
# /ms-playwright browser binaries (PLAYWRIGHT_BROWSERS_PATH is already set
# by the base image) rather than re-downloading Chromium, as long as the
# resolved pip version matches the base image tag's version — see the Step 1
# comment above for how that's kept in sync.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Step 5: Copy agent source ----------------------------------------------
# Only this repo's agent/ code goes into the image. Vikunja source is cloned
# onto the mounted volume at container startup instead (see start.sh) so
# `git push` has a real, writable, credentialed clone.
COPY agent/ ./agent/

# --- Step 6: Entrypoint ------------------------------------------------------
# start.sh (Task 3) coordinates starting the Vikunja backend, Vikunja
# frontend, and the agent webhook server together at container boot.
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

ENTRYPOINT ["/app/start.sh"]
