# Blast Radius entry – builds an import-based file→tests map at image build time.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Pin exactly the commit the scorer labeled
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN git clone --depth 1 https://github.com/tobymao/sqlglot.git \
    && cd sqlglot \
    && git fetch --depth 1 origin ${SQLGLOT_COMMIT} \
    && git checkout ${SQLGLOT_COMMIT}

# Generate the import map (no test execution – stays well under the 10 min limit)
COPY build_map.py /build/build_map.py
RUN python /build/build_map.py /build/sqlglot /app/file_to_tests.json

# Runtime code
COPY selector.py /app/selector.py
RUN chmod 644 /app/selector.py /app/file_to_tests.json \
    && rm -rf /build

WORKDIR /app
# The scorer runs as nobody (65534)
USER 65534:65534
CMD ["python", "/app/selector.py"]
