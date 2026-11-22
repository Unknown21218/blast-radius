FROM alpine/git:v2.49.1@sha256:c0280cf9572316299b08544065d3bf35db65043d5e3963982ec50647d2746e26 AS source
WORKDIR /checkout
RUN set -eu; set -o pipefail; \
    git init -q && \
    git fetch -q --depth=1 https://github.com/tobymao/sqlglot.git eb11aec4be8cb2122afe713a4816662c3f0fb2fa && \
    mkdir /baseline && \
    git archive FETCH_HEAD | tar -x -C /baseline

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 HOME=/tmp OPENBLAS_NUM_THREADS=1
COPY build_map.py /app/build_map.py
RUN python /app/build_map.py --requirements > /tmp/requirements-probe.txt \
    && python -m pip install --no-cache-dir -r /tmp/requirements-probe.txt \
    && rm /tmp/requirements-probe.txt

FROM dependencies
WORKDIR /app
COPY --from=source /baseline /baseline
# Probes on the read-only filesystem can reuse bytecode for untouched modules.
RUN python -m compileall -q /baseline/sqlglot /baseline/tests
COPY selector.py /app/selector.py
RUN chmod a+r /app/*.py
USER 65534:65534
CMD ["python", "/app/selector.py"]
