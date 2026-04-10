#!/usr/bin/env bash
# run_training.sh — 完整系統層訓練流程
#
# 使用方式：
#   ./run_training.sh                        # 標準訓練（qwen2.5-coder:14b）
#   ./run_training.sh --model qwen2.5-coder:32b
#   ./run_training.sh --dry-run              # 不實際呼叫 ollama，測試流程
#   ./run_training.sh --skip-morning         # 略過 morning startup
#
# 流程：
#   1. morning startup（decay + score-strategies + wake）
#   2. reset benchmark tasks 到初始狀態
#   3. 執行 benchmark suite（5 題）
#   4. evening startup（sleep + deep + decay）→ 知識固化進 L4/L5
#   5. 匯出訓練資料快照

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 停用 ollama 雲端功能與瀏覽器彈窗
export OLLAMA_NO_CLOUD=1
export BROWSER=""
PYTHON="${PYTHON:-python3}"
SUITE="$SCRIPT_DIR/benchmarks/coding_suite_v1.json"
RESET_SCRIPT="$SCRIPT_DIR/benchmarks/reset_tasks.sh"
REPORT_DIR="$SCRIPT_DIR/benchmark_reports"

# ── 參數解析 ────────────────────────────────────────────
MODEL="ollama/qwen2.5-coder:14b"
DRY_RUN=""
SKIP_MORNING=false
SKIP_EVENING=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)       MODEL="$2"; shift 2 ;;
    --model=*)     MODEL="${1#--model=}"; shift ;;
    --dry-run)     DRY_RUN="--dry-run"; shift ;;
    --skip-morning) SKIP_MORNING=true; shift ;;
    --skip-evening) SKIP_EVENING=true; shift ;;
    *) echo "未知參數：$1"; exit 1 ;;
  esac
done

RUN_TS=$(date '+%Y%m%d_%H%M%S')
echo ""
echo "══════════════════════════════════════════════════"
echo "  GovernorOS 系統層訓練  $RUN_TS"
echo "  模型：$MODEL"
echo "══════════════════════════════════════════════════"

# ── Step 1: Morning startup ─────────────────────────────
if [ "$SKIP_MORNING" = false ]; then
  echo ""
  echo "[Step 1/5] Morning startup（decay + score-strategies + wake）..."
  bash "$SCRIPT_DIR/startup.sh" morning
else
  echo "[Step 1/5] Morning startup 已略過"
fi

# ── Step 2: Reset tasks ─────────────────────────────────
echo ""
echo "[Step 2/5] 重置 benchmark tasks 到初始狀態..."
bash "$RESET_SCRIPT"

# ── Step 3: 執行 benchmark suite ───────────────────────
echo ""
echo "[Step 3/5] 執行 coding_suite_v1（5 題）..."
echo "  使用模型：$MODEL"
echo "  報告輸出：$REPORT_DIR"
echo ""

# 動態替換 suite 中的 aider_model（若指定不同模型）
SUITE_TMP=$(mktemp /tmp/suite_XXXXXX.json)
BENCH_TASKS="$SCRIPT_DIR/benchmarks/tasks"
python3 -c "
import json, os, re
suite = json.load(open('$SUITE'))
bench_tasks = '$BENCH_TASKS'
for case in suite['cases']:
    case['aider_model'] = '$MODEL'
    # 展開 \${BENCH_TASKS} 佔位符
    case['work_dir'] = case['work_dir'].replace('\${BENCH_TASKS}', bench_tasks)
json.dump(suite, open('$SUITE_TMP', 'w'), ensure_ascii=False, indent=2)
print(f'suite 已套用模型：$MODEL')
"

$PYTHON "$SCRIPT_DIR/benchmark_runner.py" \
  --suite "$SUITE_TMP" \
  --output-dir "$REPORT_DIR" \
  $DRY_RUN

rm -f "$SUITE_TMP"

# ── Step 4: Evening startup ─────────────────────────────
if [ "$SKIP_EVENING" = false ]; then
  echo ""
  echo "[Step 4/5] Evening startup（sleep + deep + decay）→ L3→L4→L5 固化..."
  bash "$SCRIPT_DIR/startup.sh" evening
else
  echo "[Step 4/5] Evening startup 已略過"
fi

# ── Step 5: 匯出訓練資料 ────────────────────────────────
echo ""
echo "[Step 5/5] 匯出訓練資料快照..."
EXPORT_PATH="$SCRIPT_DIR/train_artifacts/dataset/training_${RUN_TS}.jsonl"
$PYTHON "$SCRIPT_DIR/dream_cycle.py" \
  --export-training-data \
  --include-failed \
  --output "$EXPORT_PATH" 2>/dev/null || echo "  (尚無可匯出樣本)"

# ── 結果摘要 ───────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════"
echo "  訓練完成"
echo ""
echo "  記憶系統狀態："
$PYTHON "$SCRIPT_DIR/dream_cycle.py" --status 2>&1 | grep -E "L[3-5]|active|score|benchmark" || true
echo ""
echo "  最新 benchmark 報告："
ls -t "$REPORT_DIR"/*.md 2>/dev/null | head -1 | xargs -I{} echo "  {}" || echo "  (無報告)"
echo ""
echo "  訓練資料：$EXPORT_PATH"
echo "══════════════════════════════════════════════════"
echo ""
