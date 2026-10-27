# meeleespod — Outlook, Teams en Parro alleen lezen, zonder de werk-pc (ontwerp 27-10-2026, wv1435/wv1710).
# Basis vast op digest: Ubuntu noble + Python + Playwright 1.64.1 met de bijbehorende Chromium (geen Google Chrome).
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble@sha256:330d401ae23c905896a0c26c66c36fc971b621519e7b11b435a8328d7bf6e031 AS basis

# ---- bouwstap: Node vast op versie + sha256, de vanger (alleen inlogvangst), noVNC vast op versie + sha256 ----------
FROM basis AS bouw
ARG NODE_VERSIE=22.23.4
ARG NODE_SHA256=b8492284c76a54baf58d61668dd0e41ee0fc98adabf9503d30f05d08f55b4406
ARG NOVNC_VERSIE=1.7.0
ARG NOVNC_SHA256=b1003a11b6e6e8d8f7f5e5586daae7f8ca651d8aee0aa155ff9ac841c48f52c6
RUN set -eux; \
    curl -fsSL -o /tmp/node.tgz "https://nodejs.org/dist/v${NODE_VERSIE}/node-v${NODE_VERSIE}-linux-x64.tar.gz"; \
    echo "${NODE_SHA256}  /tmp/node.tgz" | sha256sum -c -; \
    mkdir -p /opt/node && tar -xzf /tmp/node.tgz -C /opt/node --strip-components=1; \
    curl -fsSL -o /tmp/novnc.tgz "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSIE}.tar.gz"; \
    echo "${NOVNC_SHA256}  /tmp/novnc.tgz" | sha256sum -c -; \
    mkdir -p /tmp/nv /bouw/novnc && tar -xzf /tmp/novnc.tgz -C /tmp/nv --strip-components=1; \
    cp -r /tmp/nv/core /tmp/nv/vendor /tmp/nv/LICENSE.txt /bouw/novnc/
ENV PATH=/opt/node/bin:$PATH
WORKDIR /bouw
COPY vendor/ vendor/
COPY vanger/package.json vanger/package-lock.json vanger/tsconfig.json vanger/
COPY vanger/src/ vanger/src/
# --ignore-scripts: geen installatiescripts van pakketten; daarna alleen de runtime (playwright + playwright-core).
RUN cd vanger && npm ci --ignore-scripts --no-audit --no-fund && npx tsc -p tsconfig.json && npm prune --omit=dev --ignore-scripts

# ---- eindimage ---------------------------------------------------------------------------------------------------------
FROM basis
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends xvfb x11vnc xauth poppler-utils \
 && rm -rf /var/lib/apt/lists/*
# Python-Playwright (het basisimage levert alleen de browsers, niet de module): vast op hash, dan pip weer weg.
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir --break-system-packages --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt
# Alleen het node-programma (geen npm/npx in het eindimage).
COPY --from=bouw /opt/node/bin/node /usr/local/bin/node
COPY --from=bouw /bouw/vanger/dist /app/vanger/dist
COPY --from=bouw /bouw/vanger/node_modules /app/vanger/node_modules
COPY --from=bouw /bouw/novnc /app/novnc
COPY vendor/outlook-access/LICENSE /app/vanger/LICENSE.outlook-access
COPY vendor/teams-access/LICENSE /app/vanger/LICENSE.teams-access
# Browserbeleid (ontwerp § 6): geen ontwikkelaarsgereedschap, geen downloads, chrome:// en file:// dicht. Op alle plekken
# waar een Chromium-bouw beleid zoekt; de rooktest toetst dat het echt geldt.
COPY beleid/chromium-beleid.json /etc/chromium/policies/managed/meeleespod.json
COPY beleid/chromium-beleid.json /etc/opt/chrome/policies/managed/meeleespod.json
COPY beleid/chromium-beleid.json /etc/opt/chrome_for_testing/policies/managed/meeleespod.json
COPY meeleespod/ /app/meeleespod/
ARG GIT_SHA=onbekend
RUN echo "$GIT_SHA" > /app/GIT_SHA && python3 -c "import playwright, sys; print(sys.version)"
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp/thuis TZ=Europe/Amsterdam \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright MEELEES_VANGER=/app/vanger/dist/vanger/src/vanger.js
WORKDIR /app
USER 1000:1000
EXPOSE 8080 8081
ENTRYPOINT ["python3", "-m", "meeleespod"]
CMD ["pod"]
