#!/usr/bin/env bash
#
# 星槎备份恢复演练。
#
#   备份 → 体检 → **真的把 data/ 挪走** → 恢复 → 重跑验收 → 复原
#
# 为什么要这个脚本：`data/backups/` 里躺着一堆 .db 文件，看着很齐全，这件事本身
# 什么都不证明。备份不可信的三种方式都不会在平时暴露：
#
#   1. WAL 模式下 `cp` 活库不是崩溃一致的（所以星槎用 VACUUM INTO）；
#   2. 备份**不含密钥环**——只恢复数据库会得到一库永久解不开的密文；
#   3. 备份文件本身可能是坏的，而你只会在灾难当天发现。
#
# 演练是唯一能同时排除这三条的手段。**每次改动部署方式之后跑一次**，
# 以及至少每季度一次。
#
# 用法：
#   ./drill.sh                默认演练（在当前部署上原地做，结束后复原）
#   ./drill.sh --keep         演练后保留恢复出来的数据（不复原）
#   ./drill.sh --no-keyring   只恢复数据库、故意不恢复密钥环，验证它**拒绝启动**
#
# 演练期间服务会**停机**（约一分钟）。不要在业务高峰跑。
# 全过程只动 data/ 与一个临时目录，不碰镜像也不碰 .env。

set -euo pipefail

KEEP=0
NO_KEYRING=0

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_cyn=$'\033[36m'; c_off=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$c_cyn" "$1" "$c_off"; }
ok()   { printf '%s✓ %s%s\n'   "$c_grn" "$1" "$c_off"; }
warn() { printf '%s! %s%s\n'   "$c_ylw" "$1" "$c_off"; }
die()  { printf '%s✗ %s%s\n'   "$c_red" "$1" "$c_off" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep)        KEEP=1; shift ;;
    --no-keyring)  NO_KEYRING=1; shift ;;
    -h|--help)     sed -n '2,25p' "$0"; exit 0 ;;
    *)             die "未知参数：$1（用 --help 看用法）" ;;
  esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

command -v docker >/dev/null || die "没有 docker。这个脚本是给已部署的机器用的。"
[[ -f .env ]] || die "没有 .env。先跑 ./deploy.sh。"
[[ -d data ]] || die "没有 data/。这台机器上还没有要演练的数据。"

dc() { docker compose "$@"; }
xc() { dc exec -T xingcha xingcha "$@"; }

# ---------------------------------------------------------------- 1 备份
step "1/6 备份数据库 + 密钥环"
dc ps --status running --services 2>/dev/null | grep -qx xingcha \
  || die "xingcha 容器没在跑。先 docker compose up -d。"

xc db backup --tag drill >/dev/null
LATEST="$(ls -t data/backups/*.db | head -1)"
[[ -n "$LATEST" ]] || die "备份没生成"
ok "数据库备份：$LATEST"

VAULT="$(mktemp -d)"
trap 'rm -rf "$VAULT"' EXIT
cp "$LATEST" "$VAULT/xingcha.db"
cp data/secret.key "$VAULT/secret.key"
ok "密钥环单独存到 $VAULT/secret.key（这一步是 A10 的关键：它不在库备份里）"

# ---------------------------------------------------------------- 2 体检
step "2/6 体检这份备份"
# 容器里 XINGCHA_DATA_DIR 已是 /data（见 docker-compose.yml），不传参就查最新那份
xc db verify || die "备份体检不过——演练到此为止，先修备份"

# ---------------------------------------------------------------- 3 灾难
step "3/6 停服并把 data/ 整个挪走（模拟磁盘丢失）"
dc down
CONDEMNED="data.drill-$(date -u +%Y%m%dT%H%M%SZ)"
mv data "$CONDEMNED"
ok "原 data/ 已挪到 $CONDEMNED（不删——演练失败时它是唯一的退路）"

# ---------------------------------------------------------------- 4 恢复
step "4/6 从备份重建"
mkdir -p data/backups
chmod 700 data
cp "$VAULT/xingcha.db" data/xingcha.db
chmod 600 data/xingcha.db
if [[ $NO_KEYRING -eq 1 ]]; then
  warn "--no-keyring：故意不恢复密钥环"
else
  cp "$VAULT/secret.key" data/secret.key
  chmod 600 data/secret.key
fi
# 容器里是非 root 用户（UID 10001），恢复出来的文件得归它
docker run --rm -v "$PWD/data:/data" alpine:3 chown -R 10001:10001 /data >/dev/null 2>&1 || \
  warn "chown 没跑成（本机可能拉不到 alpine 镜像）；若容器起不来先手动 chown 10001:10001 data"

# ---------------------------------------------------------------- 5 验收
step "5/6 重跑验收"
if [[ $NO_KEYRING -eq 1 ]]; then
  dc up -d >/dev/null 2>&1 || true
  sleep 8
  if dc logs xingcha 2>&1 | grep -q "拒绝启动"; then
    ok "预期行为：缺密钥环时**拒绝启动**，而不是静默生成一把新的"
  else
    die "缺密钥环却没有拒绝启动——这是最危险的失败：服务看着好了，而密文已永久解不开"
  fi
else
  dc up -d
  for _ in $(seq 1 30); do
    if dc ps --format '{{.Service}} {{.Health}}' 2>/dev/null | grep -q "xingcha healthy"; then
      break
    fi
    sleep 2
  done
  dc ps --format '{{.Service}} {{.Health}}' | grep -q "xingcha healthy" \
    || die "恢复后服务没起来。原数据还在 $CONDEMNED"

  PUBLIC_URL="$(grep -E '^PUBLIC_URL=' .env | cut -d= -f2-)"
  cat <<EOF

  下面三条要你自己确认（脚本拿不到你的 sk-xc- 明文）：

    curl -s ${PUBLIC_URL:-https://your.domain}/v1/models \\
      -H "Authorization: Bearer sk-xc-1-..." | head -c 300

    # 上游 key 解得开吗（这条才真正验证了密钥环与密文对得上）
    curl -s ${PUBLIC_URL:-https://your.domain}/v1/chat/completions \\
      -H "Authorization: Bearer sk-xc-1-..." \\
      -H 'Content-Type: application/json' \\
      -d '{"model":"openai/gpt-5","messages":[{"role":"user","content":"1+1"}]}'

    # 后台还能登录吗、Agent 与配额还在吗
    open ${PUBLIC_URL:-https://your.domain}/admin

EOF
  ok "服务已从备份恢复并通过健康检查"
fi

# ---------------------------------------------------------------- 6 复原
step "6/6 收尾"
if [[ $KEEP -eq 1 ]]; then
  warn "--keep：保留恢复出来的 data/。原数据留在 $CONDEMNED，确认无误后自行删除。"
else
  dc down
  rm -rf data
  mv "$CONDEMNED" data
  dc up -d
  ok "已复原到演练前的 data/（恢复出来的那份已丢弃）"
  warn "注意：复原用的是挪走的原目录，不是备份——演练本身没有改动你的生产数据。"
fi

printf '\n%s演练完成。%s\n' "$c_grn" "$c_off"
