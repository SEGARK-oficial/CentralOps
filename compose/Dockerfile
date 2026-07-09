# =============================================================================
# CentralOps multi-stage image with Cython-compiled backend.
#
# Pipeline:
# frontend-deps  → frontend-build      (Vite/pnpm; unchanged)
# backend-deps   → backend-compile     (Cython compiles app/*.py → app/*.so)
# → backend-test        (pytest sweep against the .so tree;
# build aborts on any failure)
# → final               (slim image; ships .so only, no .py
# source for compiled subtrees)
#
# Why: the final image is what gets distributed (on-prem or registry).
# Anyone who pulls it can ``docker run -it ... sh`` and inspect /app.
# Compiling the IP-bearing modules (collectors, services, providers,
# routers, core) to .so means a casual reader sees no Python source
# for those — only ELF binaries. Models/schemas/main.py stay as .py
# (low-IP-value, high metaclass/introspection risk).
#
# Build gate: ``final`` COPYs from ``backend-test``, so ``docker build``
# fails if the test sweep fails — there is no path to a released image
# whose compiled tree breaks behavior.
# =============================================================================

# ── Stage 1: Frontend dependencies ───────────────────────────────────────────
FROM node:20 AS frontend-deps
WORKDIR /app
COPY frontend/package.json ./
COPY frontend/pnpm-lock.yaml* ./
RUN npm install -g pnpm
RUN pnpm install --no-frozen-lockfile

# ── Stage 2: Frontend build ──────────────────────────────────────────────────
FROM frontend-deps AS frontend-build
ARG VITE_BACKEND_URL=/api
ENV VITE_BACKEND_URL=${VITE_BACKEND_URL}
COPY frontend/ .
RUN pnpm run build

# ── Stage 3: Backend runtime dependencies ────────────────────────────────────
# Slim image; produces the site-packages that the test/final stages reuse.
FROM python:3.11-slim AS backend-deps
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# INSTALL_OTEL: bakeia os extras OTel (requirements-otel.txt) na imagem por
# padrão, para a imagem PUBLICADA suportar observabilidade "plug and play"
# (no-op em runtime quando OTEL_ENABLED=false → zero overhead). Build com
# --build-arg INSTALL_OTEL=false para uma imagem mínima sem opentelemetry-*.
ARG INSTALL_OTEL=true
COPY backend/requirements.txt backend/requirements.lock* backend/requirements-otel.txt ./
RUN if [ -f requirements.lock ]; then \
      pip install --no-cache-dir -r requirements.lock; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi
RUN if [ "$INSTALL_OTEL" = "true" ]; then \
      pip install --no-cache-dir -r requirements-otel.txt; \
    fi
# Ferramentas de build que VÊM na imagem base (não estão no lock) e que o Trivy
# acusa como HIGH no site-packages copiado para a imagem final: força as versões
# CORRIGIDAS. wheel CVE-2026-24049 (escalada via wheel malicioso) → 0.46.2;
# jaraco.context CVE-2026-23949 (path traversal em tar) → 6.1.0.
RUN pip install --no-cache-dir --upgrade "setuptools" "wheel>=0.46.2" "jaraco.context>=6.1.0"

# ── Stage 4: Cython compile ──────────────────────────────────────────────────
# Bullseye (not slim) — Cython needs gcc/g++/python3-dev to build .so.
# Source layout preserved as /build/backend/... so the compiled modules
# can be imported as either ``app.X`` (final image, cwd=/app) or as
# ``backend.app.X`` (test stage, cwd=/build, used by the test suite).
FROM python:3.11-bullseye AS backend-compile
WORKDIR /build
COPY backend/requirements-build.txt /tmp/requirements-build.txt
# Pull runtime site-packages so any compile-time import probe (decorators,
# typing) can resolve — does not bloat this stage's image, it is discarded.
COPY --from=backend-deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r /tmp/requirements-build.txt
COPY backend/ ./backend/
COPY compose/cython-build.sh /usr/local/bin/cython-build.sh
RUN chmod +x /usr/local/bin/cython-build.sh && BUILD_ROOT=/build/backend /usr/local/bin/cython-build.sh

# ── Stage 5: Compiled-image test sweep ───────────────────────────────────────
# Runs the 127-file pytest suite against the .so tree. Build fails on any
# regression — there is no path to ``final`` without this stage succeeding.
# Discarded from the final image graph (only its existence as a build
# dependency matters; nothing here is COPYed into the runtime image).
FROM python:3.11-slim AS backend-test
WORKDIR /build
COPY --from=backend-deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY --from=backend-compile /build/backend ./backend
# Top-level scripts/ contains reencrypt_secrets.py (a one-shot CLI), and
# backend/tests/test_reencrypt_secrets_script.py imports it as a bare
# module. Copy it here so the test resolves; not shipped to ``final``.
COPY scripts ./scripts
# test_e5_bulkhead.py::test_compose_dispatcher_consumes_all_shards lê
# ``parents[4]/compose/docker-compose.yml`` (= /build/compose/...) p/ machine-
# enforce o contrato dispatcher -Q ↔ shards. O stage só copiava backend/ →
# FileNotFoundError quebrava o gate (pré-existente em main). Copia o compose
# p/ o gate rodar no .so tree também. NÃO vai p/ ``final``.
COPY compose/docker-compose.yml ./compose/docker-compose.yml
COPY compose/cython-tests.sh /usr/local/bin/cython-tests.sh
RUN chmod +x /usr/local/bin/cython-tests.sh && /usr/local/bin/cython-tests.sh

# ── Stage 6: Final runtime image — API  ─────────────────────────
# Slim Python, uvicorn PURO. NÃO embarca mais nginx nem o SPA (dist) — isso
# foi para a imagem `frontend` (stage 7). Workers Celery rodam desta mesma
# imagem (build-once/run-many), só trocando o command/entrypoint. Backend code
# em /app (layout achatado que ``app.main:app`` / ``app.collectors.celery_app``
# esperam).
FROM python:3.11-slim AS final
WORKDIR /app

# curl: healthcheck do container (probe /readyz no uvicorn :8000). openssl:
# fallback de geração da APP_MASTER_KEY (start-api.sh) em dev. gettext-base e
# nginx saíram — não há mais nginx nesta imagem .
# apt-get upgrade: aplica patches de SO da base (Trivy-gate) — ex. libssh2-1t64
# CVE-2026-55200/CVE-2026-7598 (HIGH, fixáveis). Sem isso, os pacotes ficam na
# versão da imagem base mesmo após o apt-get install.
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends openssl curl && \
    rm -rf /var/lib/apt/lists/*

# Ferramentas de empacotamento do Python do SISTEMA (/usr/local, da imagem base)
# que o Trivy acusa como HIGH: wheel CVE-2026-24049 e jaraco.context CVE-2026-23949
# (vendado no setuptools). O runtime usa /opt/venv, mas o scanner varre o FS todo
# — então corrigimos a cópia do sistema também. `python` aqui ainda é /usr/local
# (o PATH do venv só entra abaixo).
RUN python -m pip install --no-cache-dir --upgrade pip setuptools "wheel>=0.46.2"

COPY --from=backend-deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# IMPORTANT: COPY from backend-test, not backend-compile. This wires the
# pytest sweep as a build-time gate — Docker BuildKit only reaches this
# COPY after backend-test exits 0.
# We copy from /build/backend (preserved layout) into /app (flattened
# layout used at runtime). The trailing slash on the source path is the
# key bit: it copies CONTENTS of /build/backend into /app.
COPY --from=backend-test /build/backend/ /app/

# Drop test artifacts and Python-source-only files that should not ship.
# /app/.venv: o venv de DEV (backend/.venv, py3.12) vaza para o contexto porque o
# .dockerignore só exclui `.venv` na raiz, não `backend/.venv`. Ele incha a imagem
# e injeta CVEs (msgpack 1.2.0). NÃO é usado em runtime (o app roda de /opt/venv).
RUN rm -rf /app/tests /app/conftest.py /app/pytest.ini /app/scripts \
           /app/requirements.txt /app/requirements.lock /app/requirements-build.txt \
           /app/.venv /app/.pytest_cache /app/.mypy_cache /app/.ruff_cache \
    && find /app/app -type d -name tests -prune -exec rm -rf {} + \
    && find /app -depth -type d -name '__pycache__' -exec rm -rf {} +

COPY scripts/start-api.sh /usr/local/bin/start-api.sh
COPY scripts/start-collector.sh /usr/local/bin/start-collector.sh
COPY scripts/cops-celery.sh /usr/local/bin/cops-celery
RUN sed -i 's/\r$//' /usr/local/bin/start-api.sh /usr/local/bin/start-collector.sh /usr/local/bin/cops-celery \
    && chmod +x /usr/local/bin/start-api.sh /usr/local/bin/start-collector.sh /usr/local/bin/cops-celery

# roda NON-ROOT (uid 10001, grupo 0 — compatível com runAsNonRoot
# do k8s e plataformas de uid arbitrário/OpenShift). Em prod a APP_MASTER_KEY vem
# do env e o DB é Postgres, então a API/worker NÃO escreve no volume. /app/data e
# o dir de logs são chown'd + g=u para que volumes nomeados NOVOS herdem a posse
# (dev SQLite/keygen seguem funcionando como non-root); volumes já existentes
# root-owned não são tocados, mas a API non-root não os escreve em prod (env key).
RUN useradd -r -u 10001 -g root -s /usr/sbin/nologin -d /app appuser \
    && mkdir -p /app/data /var/log/centralops/collectors \
    && chown -R 10001:0 /app /var/log/centralops/collectors \
    && chmod -R g=u /app /var/log/centralops/collectors
USER 10001

EXPOSE 8000
HEALTHCHECK NONE
# uvicorn como PID 1 (start-api.sh faz exec) → SIGTERM drena nativamente (O4).
CMD ["/usr/local/bin/start-api.sh"]

# ── Stage 7: Frontend runtime image  ──────────────
# nginx servindo o SPA estático (Vite dist) + reverse-proxy de /api → a API
# (uvicorn) via ${API_UPSTREAM}. Base nginx-unprivileged: roda NON-ROOT (uid 101)
# por design, escutando em 8080/8443 (portas altas — non-root não bind <1024).
# As portas do host (3000/3443) são mapeadas para 8080/8443 no compose.
# Release de UI desacoplado do backend: mudar a UI re-publica SÓ esta imagem.
FROM nginxinc/nginx-unprivileged:alpine AS frontend
USER root
# gettext = envsubst (render dos templates com lista explícita de vars);
# openssl = self-signed em ENABLE_HTTPS sem cert fornecido.
RUN apk add --no-cache gettext openssl
COPY --from=frontend-build /app/dist/ /usr/share/nginx/html/
RUN mkdir -p /etc/nginx/templates /etc/nginx/ssl /etc/nginx/conf.d \
    && chown -R 101:0 /etc/nginx/ssl /etc/nginx/conf.d /usr/share/nginx/html \
    && chmod -R g=u /etc/nginx/ssl /etc/nginx/conf.d
COPY frontend/nginx.single.conf /etc/nginx/templates/nginx.http.conf.template
COPY frontend/nginx.single.https.conf /etc/nginx/templates/nginx.https.conf.template
COPY scripts/start-frontend.sh /usr/local/bin/start-frontend.sh
RUN sed -i 's/\r$//' /usr/local/bin/start-frontend.sh && chmod +x /usr/local/bin/start-frontend.sh
USER 101
EXPOSE 8080 8443
HEALTHCHECK NONE
CMD ["/usr/local/bin/start-frontend.sh"]
