# 交付镜像：两阶段构建 + 非 root 运行 + 就绪探针。
#
# 刻意不写 `# syntax=docker/dockerfile:1`：这行会让构建第一步就去 Docker Hub 拉
# frontend 镜像，而本文件没有用到任何 BuildKit 专有语法（没有 heredoc、没有
# --mount=type=cache）。为一个用不上的语法能力引入一条「构建前置依赖网络」，
# 代价是在不可达 Docker Hub 的环境里整条构建直接死在第一行。
#
# 三个刻意的选择，都有代价，写在这里免得后人以为是随手写的：
#
# 1. 依赖装进独立 venv 再整目录拷贝，而不是 `pip install --user` 或直接把
#    site-packages 抄过去。venv 是最不容易在拷贝后路径解析出错的形态，
#    代价是镜像里多一层目录。
# 2. 运行阶段不装编译器。numpy 在 linux/amd64 与 linux/arm64 都有 wheel，
#    如果哪天某个依赖只剩源码包，构建会失败而不是悄悄变慢——
#    失败是更好的结果。
# 3. 单 worker。进程内跑着任务执行器，而 SQLite 的写并发上限摆在那里；
#    多 worker 不会更快，只会让「同一条任务被两个进程领走」的概率上升
#    （租约能兜住，但那是兜底不是常态）。

# 基础镜像可换。默认用 Docker Hub 官方 slim；在 Docker Hub 不可达的网络里
# 用同版本的另一份 Python 3.13 镜像顶上（例如 MCR 的 devcontainers 镜像），
# 换的只是发行方，不是 Python 版本——所以下面的构建步骤不需要跟着改。
ARG PYTHON_IMAGE=python:3.13-slim

# ---------- 构建阶段 ----------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 先只拷依赖清单：改代码不该让依赖层缓存失效，这是构建速度的全部来源。
COPY requirements.lock /tmp/requirements.lock
RUN pip install -r /tmp/requirements.lock


# ---------- 运行阶段 ----------
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    RUN_MODE=mock \
    LOG_LEVEL=INFO \
    DB_PATH=data/app.sqlite3 \
    AUTH_REQUIRED=true

COPY --from=builder /opt/venv /opt/venv

# 非 root。容器不是安全边界，但它决定了「进程被打穿之后，攻击者拿到的是
# 一个没有写权限的用户，还是一个能以 root 改镜像文件系统的用户」。
# 固定 uid/gid 而不是让系统分配：卷挂载时宿主机要能对上同一个人。
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY src ./src
COPY scripts ./scripts
# 样本数据必须在镜像里：/v1/datasets 与看板都从这里读，/ready 也会检查目录存在。
COPY data/samples ./data/samples

# data/ 必须由 app 用户持有。SQLite 会在同目录写 -wal / -shm，
# 只给数据库文件写权限是不够的——这是「本地跑得好、容器里报 readonly」的常见原因。
RUN mkdir -p /app/data \
 && chown -R app:app /app/data \
 && chmod +x /app/scripts/docker-entrypoint.sh

USER app

EXPOSE 8000

# 探针打 /ready 而不是 /health：/health 只证明进程活着，
# /ready 还要求库已迁移到最新版本。用 /health 做探针的容器会「健康地」接流量然后 500。
# start-period 给迁移留出时间，否则首次启动会被探针判死并重启，形成循环。
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/ready' % os.getenv('APP_PORT','8000'), timeout=3)"

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
