"""交付物静态守卫：Dockerfile 与入口脚本的契约。

**这些测试不替代真跑容器。** 它们守的是「改动 Dockerfile 时不会静默破掉
某条已经想清楚的约定」——比如有人为了「简单」把 `USER app` 删掉，
或把探针从 `/ready` 换回 `/health`，或往第一行加回 `# syntax=`。

为什么值得单独建一个文件：Dockerfile 是唯一没有类型系统、没有编译器、
测试也不覆盖的交付物。它错了的表现不是测试红，而是「镜像构建到一半失败」
或「容器起来了但所有写请求 500」。前者至少立刻可见，后者最贵。

每条断言对应一个具体的失效场景，写在断言消息里。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
ENTRYPOINT = ROOT / "scripts" / "docker-entrypoint.sh"


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _instructions() -> list[str]:
    """去掉注释与空行后的指令列表（保留原始顺序）。"""
    lines = []
    for raw in _dockerfile().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


# ---------- 构建前置依赖 ----------


def test_no_syntax_directive():
    """不许出现 `# syntax=`。

    BuildKit 的 frontend 镜像要从 Docker Hub 拉。本文件没有用任何专有语法，
    加了这行只会换一条「构建第一步就依赖外网 registry」的失败路径——
    在一个连 python:3.13-slim 都拉不下来的环境里，这行会让构建死在第一行，
    而错误信息（`failed to resolve source metadata for docker/dockerfile:1`）
    完全不提「你的 Dockerfile 第 2 行有个可删的指令」。
    """
    text = _dockerfile()
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    assert not first.startswith("# syntax="), (
        "Dockerfile 第一条内容又变成了 # syntax=...：它会让构建先拉 Docker Hub 的 "
        "frontend 镜像。本文件未使用任何 BuildKit 专有语法，删掉即可。\n"
        "（注释里提到这个指令是允许的——Docker 只认文件开头的 parser directive。）"
    )


def test_base_image_is_overridable():
    """两阶段都必须从 ${PYTHON_IMAGE} 起，且 ARG 在第一个 FROM 之前声明。

    写死基础镜像会让「Docker Hub 不可达」变成死局：既不能构建，也不能只换
    一个来源验证其余流程。可覆盖才有替代路径（见 smoke.py --base-image）。
    """
    text = _dockerfile()
    arg_pos = text.find("ARG PYTHON_IMAGE")
    assert arg_pos != -1, "缺少 ARG PYTHON_IMAGE：基础镜像被写死了，Docker Hub 不可达时无法构建"

    froms = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)
    assert len(froms) == 2, f"预期两阶段（构建/运行），实际 {len(froms)} 个 FROM"
    for image in froms:
        assert image == "${PYTHON_IMAGE}", (
            f"FROM {image} 没有走 ARG：换基础镜像时要改两处，迟早漏一处"
        )
    first_from = text.find("\nFROM ")
    assert arg_pos < first_from, "ARG PYTHON_IMAGE 必须声明在第一个 FROM 之前，否则 FROM 取不到"


# ---------- 运行身份 ----------

def test_runs_as_non_root():
    """必须有 USER，且不是 root。

    「容器不是安全边界」是对的，但它决定了进程被打穿之后，攻击者拿到的是一个
    没有写权限的用户，还是一个能以 root 改镜像文件系统的用户。
    这条最容易被「调试时先加 USER root」然后忘了删。
    """
    users = re.findall(r"^USER\s+(\S+)", _dockerfile(), re.MULTILINE)
    assert users, "Dockerfile 没有 USER 指令：容器将以 root 运行"
    final_user = users[-1]
    assert final_user not in {"root", "0"}, f"最后的 USER 是 {final_user}，等于没降权"


def test_user_has_fixed_uid():
    """uid/gid 固定，不能让系统分配。

    卷挂载时宿主机要能对上同一个人；自动分配的 uid 在不同构建之间会变，
    表现为「某次重建之后挂载目录突然不可写」，且看不出和镜像有关。
    """
    text = _dockerfile()
    assert re.search(r"--uid\s+10001", text), "缺少固定 uid 10001"
    assert re.search(r"--gid\s+10001", text), "缺少固定 gid 10001"


# ---------- 探针 ----------

def test_healthcheck_hits_ready_not_health():
    """HEALTHCHECK 必须打 /ready。

    /health 只证明进程活着；/ready 还要求 schema 已到最新版本。
    用 /health 做探针的容器会「健康地」接流量，然后每个写请求 500——
    这种故障的排查成本远高于启动时直接拒绝。
    """
    text = _dockerfile()
    assert "HEALTHCHECK" in text, "缺少 HEALTHCHECK：容器死活无法被编排系统感知"

    line = next((ln for ln in text.splitlines() if "HEALTHCHECK" in ln), "")
    body = "\n".join(
        ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")
    )
    healthcheck_block = body[body.find("HEALTHCHECK"):]
    healthcheck_block = healthcheck_block[: healthcheck_block.find("\n\n") if "\n\n" in healthcheck_block else None]

    assert "/ready" in healthcheck_block, "探针没打 /ready"
    assert "/health" not in healthcheck_block.replace("/healthz", ""), (
        "探针打的是 /health：它不校验 schema，会让未迁移的容器被判定为健康"
    )
    assert line, "HEALTHCHECK 行未找到"


def test_healthcheck_has_start_period():
    """必须有 --start-period。

    入口要跑迁移。没有 start-period 时，首次启动的迁移耗时会被算进失败次数，
    探针连续失败 3 次 → 容器被杀 → 重启 → 再迁移 → 再被杀，形成重启循环。
    """
    text = _dockerfile()
    line = next((ln for ln in text.splitlines() if "HEALTHCHECK" in ln), "")
    assert "--start-period" in line, "HEALTHCHECK 缺少 --start-period，首次启动会被自己的探针判死"


# ---------- 入口顺序 ----------

def test_entrypoint_migrates_before_serving():
    """迁移必须在起服务之前，且用 exec 启动 uvicorn。

    顺序错（先 uvicorn 后迁移）的表现是：容器启动成功、探针在迁移完成前打进来、
    一批请求撞到缺表。用 exec 是为了让 SIGTERM 直达 uvicorn——否则 docker stop
    要等满超时才生效，而任务执行器可能正在写库。
    """
    assert ENTRYPOINT.exists(), "入口脚本不存在"
    script = ENTRYPOINT.read_text(encoding="utf-8")

    migrate_at = script.find("db.py")
    serve_at = script.find("uvicorn")
    assert migrate_at != -1, "入口脚本没有跑迁移"
    assert serve_at != -1, "入口脚本没有起服务"
    assert migrate_at < serve_at, "入口脚本先起服务后迁移：请求会撞到未迁移的库"
    assert re.search(r"\bexec\s", script), "启动 uvicorn 没有用 exec，SIGTERM 无法直达"


def test_entrypoint_is_marked_executable_in_image():
    """Dockerfile 里必须 chmod +x 入口脚本。

    Windows 上创建的文件默认没有可执行位。COPY 进来后不可执行的表现是
    `permission denied`，但那条信息不会提到「文件权限」——
    它只说你没权限运行一个你已经确认存在的文件。
    """
    assert "chmod +x" in _dockerfile(), "Dockerfile 没有 chmod +x 入口脚本"


def test_dockerfile_is_lf():
    """Dockerfile 必须是 LF。

    `.gitattributes` 里的 `Dockerfile text eol=lf` 只约束 git checkout，
    约束不了工作区里被脚本或编辑器写出来的文件——本次就真的踩到过一次：
    一个用 `Path.write_text()` 回写文件的脚本在 Windows 上把整个 Dockerfile
    翻成了 CRLF（Python 默认 newline=None，把 \\n 转成 os.linesep）。

    后果是 `RUN pip install -r /tmp/requirements.lock\\r` 这种指令，
    报错会指向一个看起来完全正常的路径。
    """
    raw = DOCKERFILE.read_bytes()
    crlf = raw.count(b"\r\n")
    assert crlf == 0, (
        f"Dockerfile 有 {crlf} 处 CRLF。"
        "注意：用 Python 写文件时要显式 newline='\\n'，否则 Windows 上会自动转 CRLF。"
    )


def test_entrypoint_is_lf():
    """入口脚本必须是 LF。

    CRLF 的 shell 脚本在 Linux 上的报错是
    `exec /app/scripts/docker-entrypoint.sh: no such file or directory`
    ——它说文件不存在，而文件明明就在那里。这是最容易被误判成 COPY 漏了的一类错误。
    """
    raw = ENTRYPOINT.read_bytes()
    assert b"\r\n" not in raw, "入口脚本是 CRLF：Linux 会报 no such file or directory"


# ---------- 交付内容 ----------


def test_copied_paths_exist_locally():
    """Dockerfile 里 COPY 的每个本地路径都必须存在。

    漏 COPY data/samples 时，构建不报错（目录不存在才报），
    而运行期表现为 /v1/datasets 空、/ready 失败——和「样本数据坏了」难以区分。
    """
    text = _dockerfile()
    for src in re.findall(r"^COPY\s+(?!--from)(\S+)\s+\S+", text, re.MULTILINE):
        if src.startswith("--"):
            continue
        assert (ROOT / src).exists(), f"COPY 的源路径不存在：{src}"


def test_dockerignore_keeps_build_inputs():
    """构建必需的文件不能被 .dockerignore 排除。

    这是「本地全绿、镜像里缺文件」的根源：.dockerignore 里一条 `*.md` 或
    `data/` 就会让 COPY 失败或者让运行期缺数据，而且两者表现完全不同。
    """
    assert DOCKERIGNORE.exists(), "缺少 .dockerignore：构建上下文会带上 .venv 与 .git"
    patterns = [
        ln.strip()
        for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    required = ["requirements.lock", "src", "scripts", "data/samples"]
    for name in required:
        negated = any(p == f"!{name}" or p == f"!{name}/" for p in patterns)
        # 被排除且没有被重新包含 → 构建会缺文件
        excluded = any(
            p == name
            or p == f"{name}/"
            or p == f"{name}/**"
            or (p.startswith(f"{name}/") and "*" in p)
            for p in patterns
        )
        assert not excluded or negated, f".dockerignore 排除了 {name} 但没有重新包含"


def test_requirements_lock_is_pinned():
    """构建用的是锁文件，且每行都带 == 版本。

    用 requirements.txt 构建意味着「今天构建的镜像」和「上周构建的镜像」
    依赖版本可能不同，而两者都自称同一个 tag。
    """
    assert "requirements.lock" in _dockerfile(), "构建用的是 requirements.txt，镜像不可复现"
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    pins = [ln for ln in lock.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert pins, "锁文件是空的"
    unpinned = [ln for ln in pins if "==" not in ln]
    assert not unpinned, f"锁文件里有未固定版本的条目：{unpinned[:3]}"


def test_no_worker_plural():
    """必须单 worker。

    SQLite 的写并发上限摆在那里；多 worker 不会更快，只会让
    「同一条任务被两个进程领走」的概率上升（租约能兜住，但那是兜底不是常态）。
    同时进程内跑着任务执行器，多 worker 等于多个执行器抢同一张表。
    """
    script = ENTRYPOINT.read_text(encoding="utf-8")
    assert "--workers 1" in script, "入口脚本没有显式指定 --workers 1"
