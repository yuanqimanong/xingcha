#!/usr/bin/env bash
#
# 星槎一键部署 / 更新。
#
#   拉代码 → 检查 .env → 构建镜像 → docker compose up -d → 等健康检查
#
# 形状照抄 finance-data-crawler/deploy：前置依赖检查给可直接执行的安装命令而不是
# 只报错；幂等的 clone-or-update；首次生成 .env 后主动停下来提示填写；--no-run
# 把「更新」与「启动」解耦。
#
# 用法：
#   ./deploy.sh                  在已克隆的仓库里更新并启动
#   ./deploy.sh --no-run         只拉代码 + 构建，不启动
#   ./deploy.sh --repo-dir /opt/xingcha    全新机器：克隆到指定目录
#   ./deploy.sh --branch master
#
# 更新对已存在的仓库执行 `git reset --hard origin/<branch>`：
# **部署机以远程为准，丢弃本地代码改动**（.env 与 data/ 是 untracked，不受影响）。
# 部署机上不要手改代码。

set -euo pipefail

REPO_URL="${REPO_URL:-git@github.com:yuanqimanong/xingcha.git}"
BRANCH="master"
REPO_DIR=""
NO_RUN=0

# ---------------------------------------------------------------- 输出
c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_cyn=$'\033[36m'; c_off=$'\033[0m'
step() { printf '%s==> %s%s\n' "$c_cyn" "$1" "$c_off"; }
ok()   { printf '%s✓ %s%s\n'   "$c_grn" "$1" "$c_off"; }
warn() { printf '%s! %s%s\n'   "$c_ylw" "$1" "$c_off"; }
die()  { printf '%s✗ %s%s\n'   "$c_red" "$1" "$c_off" >&2; exit 1; }

# ---------------------------------------------------------------- 参数
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-run)    NO_RUN=1; shift ;;
    --repo-dir)  REPO_DIR="${2:?--repo-dir 需要一个路径}"; shift 2 ;;
    --repo-url)  REPO_URL="${2:?--repo-url 需要一个地址}"; shift 2 ;;
    --branch)    BRANCH="${2:?--branch 需要一个分支名}"; shift 2 ;;
    -h|--help)   sed -n '2,26p' "$0"; exit 0 ;;
    *)           die "未知参数：$1（用 --help 看用法）" ;;
  esac
done

# 默认取脚本上级目录：在已克隆仓库的 deploy/ 下直接跑时就是仓库根
if [[ -z "$REPO_DIR" ]]; then
  REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

# ---------------------------------------------------------------- 前置依赖
# 缺什么就给出**可以直接粘贴执行**的安装命令。只说"缺少 docker"没有用。
need() {
  local cmd="$1" hint="$2"
  command -v "$cmd" >/dev/null 2>&1 || {
    printf '%s✗ 缺少命令：%s%s\n' "$c_red" "$cmd" "$c_off" >&2
    printf '%s  %s%s\n' "$c_ylw" "$hint" "$c_off" >&2
    exit 1
  }
}

step "检查前置依赖"
need git    "apt-get update && apt-get install -y git"
need docker "curl -fsSL https://get.docker.com | sh"

# compose v2 是 docker 的子命令；v1 的 docker-compose 已经 EOL，不支持。
docker compose version >/dev/null 2>&1 \
  || die "需要 Docker Compose v2。安装：apt-get install -y docker-compose-plugin"

docker info >/dev/null 2>&1 \
  || die "连不上 Docker 守护进程。是不是没启动（systemctl start docker），或当前用户不在 docker 组？"
ok "git · docker · compose 就绪"

# ---------------------------------------------------------------- 拉代码
if [[ -d "$REPO_DIR/.git" ]]; then
  REPO_DIR="$(cd "$REPO_DIR" && pwd)"
  step "更新已存在的仓库：$REPO_DIR"
  git -C "$REPO_DIR" fetch --all --prune
  git -C "$REPO_DIR" checkout "$BRANCH"
  # 以远程为准。.env 与 data/ 是 untracked，reset 不会动它们。
  git -C "$REPO_DIR" reset --hard "origin/$BRANCH"
else
  step "克隆仓库到：$REPO_DIR"
  git clone -b "$BRANCH" "$REPO_URL" "$REPO_DIR"
  REPO_DIR="$(cd "$REPO_DIR" && pwd)"
fi
ok "代码已在 $(git -C "$REPO_DIR" rev-parse --short HEAD)"

cd "$REPO_DIR"

# ---------------------------------------------------------------- 配置
# .env 含域名等运行必需项且不在版本库。缺则生成模板并**中止**——带着空配置继续
# 只会在几步之后以一个更难懂的错误失败。
if [[ ! -f .env ]]; then
  cp deploy/.env.example .env
  warn "已从 deploy/.env.example 生成 .env"
  printf '\n  请填写后重新运行：%s\n\n' "$REPO_DIR/.env"
  printf '  必填：XINGCHA_DOMAIN（已解析到本机的域名，Caddy 要用它申请证书）\n'
  printf '  可选：ACME_EMAIL（证书到期提醒）\n\n'
  exit 1
fi

# shellcheck disable=SC1091
set -a; source .env; set +a
[[ -n "${XINGCHA_DOMAIN:-}" ]] || die ".env 里的 XINGCHA_DOMAIN 是空的。Caddy 需要它来申请证书。"
ok "配置就绪（域名 $XINGCHA_DOMAIN）"

# ---------------------------------------------------------------- 数据目录
# 容器里以 UID 10001 运行，所以宿主目录必须属于它，否则容器起来后写不进去。
# 这是 bind mount 最常见的失败方式，而报错（Permission denied）离根因很远。
step "准备数据目录"
mkdir -p data
if [[ "$(stat -c '%u' data)" != "10001" ]]; then
  if [[ $EUID -eq 0 ]]; then
    chown -R 10001:10001 data
    ok "data/ 属主已设为 10001（容器内的 xingcha 用户）"
  else
    warn "data/ 的属主不是 10001，容器可能写不进去。执行：sudo chown -R 10001:10001 $REPO_DIR/data"
  fi
fi
chmod 700 data

# ---------------------------------------------------------------- 构建
step "构建镜像"
docker compose build

if [[ $NO_RUN -eq 1 ]]; then
  ok "完成（--no-run，未启动）。手动启动：docker compose up -d"
  exit 0
fi

# ---------------------------------------------------------------- 启动
step "启动（up -d 就是已接受的那 1–2 秒中断）"
docker compose up -d

step "等待健康检查"
for i in $(seq 1 60); do
  status="$(docker compose ps --format json xingcha 2>/dev/null | grep -o '"Health":"[^"]*"' | head -1 || true)"
  case "$status" in
    *healthy*) ok "星槎已就绪"; break ;;
    *unhealthy*) die "容器不健康。看日志：docker compose logs xingcha" ;;
  esac
  [[ $i -eq 60 ]] && die "等待超时。看日志：docker compose logs xingcha"
  sleep 2
done

# ---------------------------------------------------------------- 收尾
printf '\n'
ok "部署完成"
printf '\n'
printf '  后台        https://%s/admin\n' "$XINGCHA_DOMAIN"
printf '  API         https://%s/v1\n' "$XINGCHA_DOMAIN"
printf '  健康检查    curl -s https://%s/healthz\n' "$XINGCHA_DOMAIN"
printf '\n'
printf '  接下来：\n'
printf '    1. 打开后台设置管理员密码（首次访问会引导）\n'
printf '    2. 在「设置」里填 OpenRouter key\n'
printf '    3. 在「密钥」里签发一把 sk-xc- 交给业务代码\n'
printf '\n'
warn "v1 不执行配额。请到 OpenRouter 后台给这把上游 key 单独设一个信用上限——"
warn "那是目前唯一真正的钱刹车。"
printf '\n'
