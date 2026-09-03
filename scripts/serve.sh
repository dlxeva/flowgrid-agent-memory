#!/usr/bin/env bash
# 启动 AML Retriever 官方 Add/Search wrapper。
#
#   ./scripts/serve.sh                        # 127.0.0.1:8080，库落在 ./aml.db
#   AML_PORT=9000 ./scripts/serve.sh          # 换端口
#   AML_DB_PATH=/tmp/x.db ./scripts/serve.sh  # 换库
#   AML_AUTH_MODE=bearer AML_API_KEY=secret ./scripts/serve.sh
#
# 全部配置项见 config.example.json 与 README.md。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "找不到 python3，可用 PYTHON=/path/to/python3 指定。" >&2
  exit 1
fi

# FTS5 是硬依赖，起服务前先挡住"跑起来才发现不支持"的情况
"$PY" - <<'EOF' || { echo "当前 Python 的 sqlite3 未启用 FTS5，无法启动。" >&2; exit 1; }
import sqlite3, sys
try:
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
except Exception as exc:
    print(exc, file=sys.stderr); sys.exit(1)
EOF

export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
export AML_DB_PATH="${AML_DB_PATH:-$REPO/aml.db}"

echo "[serve] repo=$REPO"
echo "[serve] db=$AML_DB_PATH  auth=${AML_AUTH_MODE:-none}"
echo "[serve] 停止后如需清数据: $PY -m aml_retriever.cli purge --db \"$AML_DB_PATH\" --yes"

exec "$PY" -m aml_retriever.cli serve "$@"
