# Pinned by digest (ADS-63): a tag moves, and two builds weeks apart would run different
# bases with no diff to show for it. To upgrade, look up the new digest and change it here.
FROM python:3.11.18-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535 AS armavita-runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /opt/armavita

# uv itself, pinned by version and digest, from Astral's image (no pip-installed, unpinned uv).
COPY --from=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c /uv /usr/local/bin/uv

# Dependencies come from uv.lock, hash-checked, so the image gets exactly the versions the
# tests ran against (ADS-63: the old `-e .` install re-resolved floors like requests>=2.32.3).
# Every dependency ships wheels, so no compiler is installed.
COPY pyproject.toml uv.lock README.md ./
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.lock \
    && uv pip install --system --require-hashes --requirement /tmp/requirements.lock \
    && rm /tmp/requirements.lock

# Then the project itself, without touching the locked dependencies.
COPY src ./src
COPY scripts ./scripts
RUN uv pip install --system --no-deps .

ENV PYTHONPATH=/opt/armavita/src

# Run as an unprivileged user rather than root.
RUN useradd --create-home --uid 10001 armavita
USER armavita

ENTRYPOINT ["python", "-m", "armavita_meta_ads_mcp"]
