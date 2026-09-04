# 星槎镜像。
#
# 两阶段：builder 装依赖，runtime 只带运行时——最终镜像里没有编译器、没有 git、
# 没有构建缓存。攻击面小一圈，`docker save | zstd` 传上 VPS 也快。
#
# 这个镜像**不映射任何宿主端口**（见 docker-compose.yml）：对外只有 Caddy。

FROM python:3.13-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# uv 从官方镜像拷过来，比在容器里跑安装脚本快且可复现
COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /uvx /bin/

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# `uv sync --frozen` 而不是 `uv pip install .`：**按锁文件装，不重新解析依赖。**
#
# 不锁的话，同一个 commit 在两天里构建出的镜像可以带着不同的传递依赖——而这个项目
# 的头号承诺是「升级对用户无感」。一次静默的间接依赖变更（httpx2、openai SDK 的行为
# 改动）会表现成"什么都没改，但线上行为变了"，那是最难排查的一类事故。
#
# --frozen 还会在锁文件与 pyproject 不一致时**直接失败**，而不是悄悄按新的装。
# --no-dev：镜像里不需要 pytest / ruff / pyright。
# --no-editable：**必须有。** uv sync 默认把项目装成 editable，指向 /build/src——
#   而那个目录在 runtime 阶段不存在，于是构建照样成功、镜像里 `import xingcha`
#   直接 ModuleNotFoundError。实测踩过。
# UV_PROJECT_ENVIRONMENT：直接建在最终路径上——venv 里的 shebang 是绝对路径，
#   建完再拷到别处会让 `xingcha` 这个入口脚本失效。
RUN UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-dev --no-editable --no-cache

# ---------------------------------------------------------------------------

FROM python:3.13-slim-bookworm AS runtime

# curl 用于容器健康检查。这是唯一额外装的东西。
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# 非 root 运行。UID 固定 10001，让宿主上 data/ 的属主可预测——用随机 UID 的话，
# 宿主 chown 时不知道该给谁，而那正是 bind mount 权限问题最常见的来源。
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin xingcha

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    XINGCHA_DATA_DIR=/data \
    XINGCHA_HOST=0.0.0.0

# 容器内监听 0.0.0.0 是安全的：它不映射宿主端口，只有同一 docker 网络里的 Caddy
# 能连上。宿主上的默认值仍然是 127.0.0.1（见 config.py），两者不冲突。

# 构建期自检。一个装坏了的镜像必须在**构建时**失败，而不是等它上了 VPS、
# healthcheck 红了才发现——上面那条 --no-editable 就是这样被发现的。
RUN xingcha --help >/dev/null \
 && python -c "import xingcha, opentelemetry.sdk; print(xingcha.__version__)"

RUN mkdir -p /data && chown xingcha:xingcha /data
VOLUME ["/data"]
USER xingcha
WORKDIR /home/xingcha

EXPOSE 8720

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8720/healthz || exit 1

ENTRYPOINT ["xingcha"]
CMD ["serve"]
