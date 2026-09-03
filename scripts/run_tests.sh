#!/usr/bin/env bash
# 本地总验收：环境探测 → 全量单测 → 三个接口 smoke → 治理产品门。
#
#   ./scripts/run_tests.sh              # base 安装（MCP SDK 测试按预期 skip）
#   ./scripts/run_tests.sh --with-mcp   # 额外要求并实测官方 MCP SDK stdio
#   ./scripts/run_tests.sh --with-eval  # 追加旧检索合成评测消融（数分钟）
#
# 全程不联网、只用合成数据；临时库跑完即删。--with-mcp 需要先安装
# 项目的可选 ``mcp>=2,<3`` extra，不会把 SDK 变成 base 依赖。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="${PYTHON:-python3}"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

WITH_EVAL=0
WITH_MCP=0
for argument in "$@"; do
  case "$argument" in
    --with-eval) WITH_EVAL=1 ;;
    --with-mcp) WITH_MCP=1 ;;
    *) printf 'unknown argument: %s\n' "$argument" >&2; exit 2 ;;
  esac
done

section() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

section "0. 环境"
"$PY" - <<'EOF'
import platform, sqlite3, sys
print(f"python  : {sys.version.split()[0]} ({platform.python_implementation()})")
print(f"platform: {platform.platform()}")
print(f"sqlite  : {sqlite3.sqlite_version}")
con = sqlite3.connect(":memory:")
try:
    con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    print("fts5    : available")
except Exception as exc:
    print(f"fts5    : MISSING ({exc})"); raise SystemExit(1)
for mod in ("numpy", "sentence_transformers", "faiss"):
    try:
        __import__(mod); print(f"vector  : {mod} importable")
        break
    except Exception:
        continue
else:
    print("vector  : unavailable (向量档位将被跳过，属预期)")
EOF

section "1. 全量单元测试"
"$PY" -m unittest discover -s tests

section "2. CLI 端到端自检"
"$PY" -m aml_retriever.cli selfcheck

section "3. AML Add/Search 官方契约 smoke"
"$PY" scripts/smoke_api.py

section "4. Governed REST v1 smoke"
"$PY" scripts/smoke_rest_v1.py

section "5. FLG + AML product profile"
"$PY" scripts/run_governance_eval.py --profile product

if [[ "$WITH_MCP" == "1" ]]; then
  section "6. 官方 MCP SDK v2 stdio smoke"
  if ! "$PY" -c 'import mcp' >/dev/null 2>&1; then
    printf 'MCP SDK missing; install the project with the mcp extra first.\n' >&2
    exit 1
  fi
  "$PY" scripts/smoke_mcp.py
else
  printf '\n(跳过可选 MCP stdio；在安装 mcp extra 的环境中加 --with-mcp 可验证)\n'
fi

if [[ "$WITH_EVAL" == "1" ]]; then
  section "7. 旧检索合成评测消融"
  "$PY" scripts/run_eval.py --scale medium --difficulty mixed --seed 20260806 --top-k 100
  section "8. 旧检索参数扫描"
  "$PY" scripts/run_scan.py --scan all --scale medium --difficulties plain,paraphrase,mixed
else
  printf '\n(跳过旧检索消融；加 --with-eval 可一并执行)\n'
fi

printf '\n\033[1;32m全部通过。\033[0m\n'
