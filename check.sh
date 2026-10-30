#!/usr/bin/env bash
# 提交前冒烟检查 —— 参数原样透传给 smoke.py，例如：./check.sh --skip-tests
set -uo pipefail
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  exec python3 smoke.py "$@"
elif command -v python >/dev/null 2>&1; then
  exec python smoke.py "$@"
fi

echo "[错误] 没找到 Python，请先安装 Python 3.8+" >&2
exit 127
