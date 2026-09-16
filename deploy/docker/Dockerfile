# Insight Flow 私有化部署 · Dockerfile
FROM python:3.12-slim

WORKDIR /app

# 系统依赖（cryptography 编译需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# 依赖层（利用缓存）
COPY pyproject.toml README.md ./
COPY insflow ./insflow
RUN pip install --no-cache-dir .

# 内置资产（随镜像分发）
COPY plugins ./plugins
COPY skills ./skills
COPY templates ./templates

# 运行时数据卷挂载点
VOLUME ["/app/data"]

EXPOSE 8400

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8400/health')" || exit 1

CMD ["insflow", "serve", "--host", "0.0.0.0", "--port", "8400"]
