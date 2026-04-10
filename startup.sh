#!/usr/bin/env bash
# startup.sh — AI System v3.0 時間排程腳本
#
# 用法：
#   ./startup.sh morning   → 09:00 啟動（decay + wake）
#   ./startup.sh evening   → 15:00-16:00 盤後整合（sleep + deep + decay）
#   ./startup.sh status    → 顯示記憶系統狀態
#   ./startup.sh init      → 第一次使用：初始化所有記憶檔案
#   ./startup.sh analyze   → 分析 Git Diff Intel patterns
#
# 建議加入 crontab：
#   0  9 * * 1-5  /path/to/AI_system_v3/startup.sh morning  >> /tmp/ai_morning.log 2>&1
#   0 15 * * 1-5  /path/to/AI_system_v3/startup.sh evening  >> /tmp/ai_evening.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
DC="$PYTHON $SCRIPT_DIR/dream_cycle.py"
DI="$PYTHON $SCRIPT_DIR/git_diff_intel.py"

echo_header() {
    echo ""
    echo "══════════════════════════════════════════"
    echo "  AI System v3.0 — $1"
    echo "  $(date '+%Y-%m-%d %H:%M:%S')"
    echo "══════════════════════════════════════════"
}

# ─────────────────────────────────────────────
# morning：09:00 啟動程序
# ─────────────────────────────────────────────
cmd_morning() {
    echo_header "Morning Startup"

    echo ""
    echo "[1/3] Memory Decay Check..."
    $DC --decay

    echo ""
    echo "[2/3] Conflict Check（含 decay wake）..."
    $DC --wake

    echo ""
    echo "[3/3] 系統狀態..."
    $DC --status

    echo ""
    echo "✓ Morning 啟動完成，系統就緒"
    echo ""
}

# ─────────────────────────────────────────────
# evening：15:00 盤後整合
# ─────────────────────────────────────────────
cmd_evening() {
    echo_header "Evening Integration"

    echo ""
    echo "[1/4] DreamCycle --sleep（L3 → L4）..."
    $DC --sleep

    echo ""
    echo "[2/4] DreamCycle --deep（L4 → L5）..."
    $DC --deep

    echo ""
    echo "[3/4] Memory Decay Check..."
    $DC --decay

    echo ""
    echo "[4/4] Git Diff Intel Pattern 分析..."
    $DI --analyze

    echo ""
    echo "[狀態摘要]"
    $DC --status
    $DI --status

    echo ""
    echo "✓ Evening 整合完成"
    echo ""
}

# ─────────────────────────────────────────────
# init：第一次初始化
# ─────────────────────────────────────────────
cmd_init() {
    echo_header "系統初始化"

    echo ""
    echo "[1/2] 初始化記憶目錄與檔案..."
    $DC --init

    echo ""
    echo "[2/2] 確認 git_diff_records.jsonl..."
    RECORDS="$SCRIPT_DIR/memory/git_diff_records.jsonl"
    if [ ! -f "$RECORDS" ]; then
        touch "$RECORDS"
        echo "  建立 git_diff_records.jsonl"
    else
        echo "  已存在 git_diff_records.jsonl，略過"
    fi

    echo ""
    echo "✓ 初始化完成"
    echo ""
    echo "下一步："
    echo "  1. 執行 morning 啟動：./startup.sh morning"
    echo "  2. 開始任務：python main_loop.py --task '...' --harness 'pytest tests/'"
    echo ""
}

# ─────────────────────────────────────────────
# status：快速狀態查看
# ─────────────────────────────────────────────
cmd_status() {
    echo_header "系統狀態"
    $DC --status
    echo ""
    $DI --status
}

# ─────────────────────────────────────────────
# analyze：手動觸發 Git Diff Intel 分析
# ─────────────────────────────────────────────
cmd_analyze() {
    echo_header "Git Diff Intel 分析"
    $DI --analyze
}

# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────
CMD="${1:-help}"

case "$CMD" in
    morning)  cmd_morning  ;;
    evening)  cmd_evening  ;;
    init)     cmd_init     ;;
    status)   cmd_status   ;;
    analyze)  cmd_analyze  ;;
    help|*)
        echo ""
        echo "用法：./startup.sh [morning|evening|init|status|analyze]"
        echo ""
        echo "  init     → 第一次使用，初始化記憶目錄"
        echo "  morning  → 09:00 啟動（decay check + wake 注入）"
        echo "  evening  → 15:00 盤後整合（sleep + deep + decay）"
        echo "  status   → 顯示 L3/L4/L5 + DiffIntel 狀態"
        echo "  analyze  → 手動分析 Git Diff 模式"
        echo ""
        ;;
esac
