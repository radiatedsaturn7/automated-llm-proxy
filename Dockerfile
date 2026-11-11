# Pin this tag by digest in controlled production releases.
FROM python:3.12-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY . .
RUN python -m pip install --prefix=/install --require-hashes -r requirements.lock \
    && python -m pip install --prefix=/install --no-deps .

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/usr/local/bin:$PATH
RUN groupadd --gid 10001 gateway && useradd --uid 10001 --gid gateway --no-create-home gateway
COPY --from=builder /install /usr/local
WORKDIR /app
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "gateway.cli", "--host", "0.0.0.0", "--port", "8080"]
