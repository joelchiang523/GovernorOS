#!/usr/bin/env bash
# auto_train.sh — 每小時自動訓練腳本
# crontab: 0 * * * * <path-to>/AI_system_v3/auto_train.sh >> <path-to>/AI_system_v3/memory/auto_train.log 2>&1

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Use conda python if available (has requests), else fallback to system python3
if [ -z "${PYTHON:-}" ]; then
  if python3 -c "import requests" 2>/dev/null; then
    PYTHON="python3"
  else
    PYTHON="$(find /opt/conda /root/miniconda3 /home/*/miniconda3 /mnt/*/conda_envs -name python3 -maxdepth 5 2>/dev/null | head -1)"
    PYTHON="${PYTHON:-python3}"
  fi
fi
LOCK="$SCRIPT_DIR/memory/.auto_train.lock"
LOG="$SCRIPT_DIR/memory/auto_train.log"
MODEL="${OLLAMA_MODEL_AIDER:-ollama/qwen2.5-coder:14b}"

export OLLAMA_NO_CLOUD=1
export BROWSER=""
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

echo ""
echo "════════════════════════════════════════"
echo "  auto_train.sh 啟動  $(timestamp)"
echo "════════════════════════════════════════"

# ── 鎖定（防止重複執行） ─────────────────────────────────
if [ -f "$LOCK" ]; then
    LOCK_PID=$(cat "$LOCK" 2>/dev/null || echo "0")
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "[SKIP] 訓練已在執行中（PID=$LOCK_PID），跳過本次"
        exit 0
    fi
    echo "[WARN] 舊 lock 殘留，清除後繼續"
    rm -f "$LOCK"
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

# ── 確認 ollama 可用 ─────────────────────────────────────
if ! curl -sf "http://${OLLAMA_HOST}/api/tags" > /dev/null 2>&1; then
    echo "[SKIP] ollama server 未啟動，跳過本次"
    exit 0
fi

# ── Step 1: 重置 benchmark tasks ─────────────────────────
echo "[$(timestamp)] Step 1: 重置 benchmark tasks"
bash "$SCRIPT_DIR/benchmarks/reset_tasks.sh"

# ── Step 2: 執行 benchmark suite ────────────────────────
echo "[$(timestamp)] Step 2: 執行 coding_suite_v2（模型：$MODEL）"

SUITE_TMP=$(mktemp /tmp/suite_XXXXXX.json)
BENCH_TASKS="$SCRIPT_DIR/benchmarks/tasks"
python3 -c "
import json
suite = json.load(open('$SCRIPT_DIR/benchmarks/coding_suite_v2.json'))
for case in suite['cases']:
    case['aider_model'] = '$MODEL'
    case['work_dir'] = case['work_dir'].replace('\${BENCH_TASKS}', '$BENCH_TASKS')
json.dump(suite, open('$SUITE_TMP', 'w'), ensure_ascii=False, indent=2)
"

$PYTHON "$SCRIPT_DIR/benchmark_runner.py" \
  --suite "$SUITE_TMP" \
  --output-dir "$SCRIPT_DIR/benchmark_reports" \
  || echo "[WARN] benchmark 有題目未通過"

rm -f "$SUITE_TMP"

# ── Step 3: 記憶固化（L3 → L4 → L5） ──────────────────
echo "[$(timestamp)] Step 3: dream_cycle --sleep（L3→L4）"
$PYTHON "$SCRIPT_DIR/dream_cycle.py" --sleep 2>&1 | tail -5

echo "[$(timestamp)] Step 4: dream_cycle --deep（L4→L5）"
$PYTHON "$SCRIPT_DIR/dream_cycle.py" --deep 2>&1 | tail -5

echo "[$(timestamp)] Step 5: dream_cycle --wake（注入 L4/L5 → PROMPT.md）"
$PYTHON "$SCRIPT_DIR/dream_cycle.py" --wake 2>&1 | tail -5

# ── Step 6: 顯示狀態 ─────────────────────────────────────
echo "[$(timestamp)] 記憶系統狀態："
$PYTHON "$SCRIPT_DIR/dream_cycle.py" --status 2>&1 | grep -E "L[3-5]|active|validated|benchmark"

# ── Step 7: 比較本次 vs 上次 benchmark ─────────────────
echo "[$(timestamp)] benchmark 能力變化："
$PYTHON "$SCRIPT_DIR/benchmarks/compare_runs.py" 2>/dev/null || echo "（需 2 次以上 benchmark 才能比較）"

echo ""
echo "[$(timestamp)] ✓ auto_train 完成"
echo "════════════════════════════════════════"
