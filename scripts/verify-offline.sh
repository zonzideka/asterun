#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="${ASTERUN_VERIFY_TMP:-$(mktemp -d /tmp/asterun-verify.XXXXXX)}"
# 只继承验证参数；后端开关、凭据、代理认证、原生 HOME 均不进入测试进程。
if [[ "${ASTERUN_OFFLINE_CLEAN_ENV:-}" != 1 ]]; then
  exec /usr/bin/env -i PATH="$(dirname "$(command -v python3)"):/usr/bin:/bin:/usr/sbin:/sbin" \
    LANG=en_US.UTF-8 ASTERUN_OFFLINE_CLEAN_ENV=1 ASTERUN_VERIFY_TMP="$TMP" \
    PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-}" /bin/bash "$ROOT/scripts/verify-offline.sh"
fi
export HOME="$TMP/home"
export ASTERUN_HOME="$TMP/asterun-home"
mkdir -p "$HOME" "$ASTERUN_HOME"
# 默认检查不得看见开发者凭据路径
unset ASTERUN_CONFIG CODEX_HOME ANTHROPIC_API_KEY OPENAI_API_KEY GROK_API_KEY \
  ASTERUN_RUN_CODEX ASTERUN_RUN_GROK ASTERUN_RUN_CLAUDE \
  ASTERUN_RUN_ANTIGRAVITY ANTIGRAVITY_BIN RUN_ANTIGRAVITY_INTEGRATION \
  CODEX_BIN GROK_BIN CLAUDE_BIN GROK_MODELS_CACHE \
  RUN_CODEX_APP_SERVER_INTEGRATION RUN_GROK_INTEGRATION \
  ASTERUN_ACCOUNT_REF ASTERUN_RUNTIME_REF || true

if ! python3 -m venv "$TMP/venv"; then
  echo "python3 -m venv failed; install python3.12-venv (ensurepip). GitHub setup-python already has it." >&2
  exit 1
fi
"$TMP/venv/bin/pip" install -q -U pip
"$TMP/venv/bin/pip" install -q -e "${ROOT}[test]"
"$TMP/venv/bin/python" -c "import asterun; print(asterun.__version__)"
"$TMP/venv/bin/asterun" version >/dev/null
"$TMP/venv/bin/python" "$ROOT/packages/asterun-plugin-antigravity/scripts/verify-source.py"
"$TMP/venv/bin/pytest" -q "$ROOT/tests"
echo "offline verification passed in $TMP"
