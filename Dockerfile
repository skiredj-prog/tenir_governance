# TENIR-Gov — Governance Middleware
# Reproducibility container for SoftwareX submission R5.0.0 (IRON OMEGA R5)
#
# Build:  docker build -t tenir-gov .
# Test:   docker compose exec tenir-middleware pytest -v --cov=tenir_governance
# Run:    docker compose up -d

FROM python:3.11-slim

LABEL maintainer="Abdelaziz Skiredj <skiredj@gmail.com>"
LABEL version="R5.0.0"
LABEL description="TENIR Governance Middleware — Deterministic Policy Engine + Merkle Audit Ledger"

WORKDIR /app

# ── System deps ────────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Copy project ───────────────────────────────────────────────────────────────
COPY . .

# ── Install Python packages ────────────────────────────────────────────────────
# 1. Core governance package (tenir_governance)
RUN pip install --no-cache-dir -e .

# 2. R4 package (tenir_v4_test)
RUN pip install --no-cache-dir -e ./r4

# 3. R5 runtime dependencies
RUN pip install --no-cache-dir \
        fastapi==0.111.* \
        uvicorn[standard]==0.29.* \
        pydantic==2.* \
        networkx \
        websockets \
        httpx \
        neo4j

# 4. Test and coverage tooling
RUN pip install --no-cache-dir \
        pytest \
        pytest-cov \
        pytest-asyncio

# ── Ledger directory (writable at runtime) ─────────────────────────────────────
RUN mkdir -p /app/ledger /app/audit

# ── Default command: full validation suite ─────────────────────────────────────
# Runs all 445 tests across four test suites with coverage on tenir_governance.
# Override with: docker compose exec tenir-middleware pytest [args]
CMD ["pytest", \
     "tests/", \
     "r4/tests/", \
     "r5_hardened/IRON_OMEGA_R5/test_r5_all.py", \
     "r5_hardened/IRON_OMEGA_R5/test_institutional_hardening.py", \
     "r5_wired/test_r5_governance_integration.py", \
     "--cov=tenir_governance", \
     "--cov-report=term-missing", \
     "-v", "--tb=short"]
