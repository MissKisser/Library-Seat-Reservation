FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/seatbot

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY seatbot ./seatbot
COPY config.example.yaml ./

RUN pip install --upgrade pip && pip install -e .

RUN playwright install --with-deps chromium || true

# 容器默认配置：面板绑定 0.0.0.0（容器内回环地址无法从宿主机/外部到达），
# 其余字段沿用示例配置。宿主侧自带 config.yaml 时以宿主文件为准。
RUN sed -e 's/^\( *\)web_host:.*/\1web_host: 0.0.0.0/' config.example.yaml > config.docker.yaml

# 非 root 运行；/data 为持久化卷，宿主侧目录属主需与此处 uid 10001 一致。
RUN groupadd --gid 10001 seatbot \
 && useradd --uid 10001 --gid 10001 --no-log-init --create-home \
        --shell /usr/sbin/nologin seatbot \
 && mkdir -p /data/logs \
 && chown -R seatbot:seatbot /data /app

USER seatbot

# 工作目录即数据目录：db_path / log_dir 的相对路径都落在此卷内
WORKDIR /data

EXPOSE 8080
VOLUME ["/data"]

CMD ["sh", "-c", "if [ ! -f /data/config.yaml ]; then cp /app/config.docker.yaml /data/config.yaml || echo 'WARN: 无法写入 /data/config.yaml，请检查数据目录权限（容器以 uid 10001 运行）'; fi; exec python -m seatbot run --config /data/config.yaml"]
