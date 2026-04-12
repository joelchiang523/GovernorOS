# AI System v3.0 — v4.0 優化策略規劃（修正版）

> **版本**：v4.0-plan-revised  
> **撰寫日期**：2026-04-12  
> **基準評分**：7.0/10（見下方模組評分表）  
> **目標評分**：8.2/10  
> **核心原則**：保留五層記憶架構與雙模型分工的優點，補強現有短板，不大幅重寫。

---

## 一、現況評分與問題清單

### 模組評分表（基準）

| 模組 | 現況分 | 主要問題 |
|---|---|---|
| main_loop.py | 7.5 | DDI 補全 bug（已修）；Total Recall 已整合但 benchmark 下實際上被 FAST_MODE 間接跳過 |
| dream_cycle.py | 7.0 | L5 空白；L4 有重複項；現有去重只在 `--deep`，不會重編 id |
| benchmark_runner.py | 8.5 | 最穩定；無重大問題 |
| git_diff_intel.py | 6.5 | 資料收集完整但未被其他模組消費 |
| mempalace.py | 6.0 | benchmark 全跳過，影響力低 |
| task_intake.py | 5.5 | 在 benchmark 流程中被繞過 |
| **L5 策略庫** | 3.0 | **0 筆 active，功能形同虛設** |
| **L4 知識庫** | 6.0 | confidence 普遍 0.55–0.70，有 ID 重複 |
| Total Recall | 4.5 | 已整合但 benchmark 下實際未被驗證 |

### 根本問題歸類

```
問題 A：L5 永遠空白
  └─ 根因：L4 confidence 未超過 0.75（升 L5 門檻），dream cycle 跑太少
  
問題 B：L4 ID 重複與 confidence 偏低
  └─ 根因：現有去重只在 --deep 執行，且只做相似條目合併，不會做 id 正規化；scoring 參數偏保守
  
問題 C：Total Recall 無法在 benchmark 被驗證
  └─ 根因：FAST_MODE=1 為總開關，benchmark 與主迴圈沒有 recall 專屬開關
  
問題 D：DiffIntel 資料孤島
  └─ 根因：--analyze 結果只印出報表，未寫入 L4/L5，無法被 DDI 利用
  
問題 E：task_intake.py 存在感低
  └─ 根因：benchmark 直接呼叫 main_loop.py，不經過任務分派層
  
問題 F：字串任務（string_ops）timeout
  └─ 根因：DDI-Decompose 只回傳部分子任務（已修正：補全驗證）
```

---

## 二、優化策略總覽（保留優點）

### 保留不動的優點

| 優點 | 原因 |
|---|---|
| 五層記憶架構（L1-L5） | 分層門檻設計正確，防止錯誤知識固化 |
| 雙模型分工（Governor/Researcher） | 高風險節點雙層把關的思路正確 |
| DDI 4-stage pipeline | 架構完整，補全驗證修正後更健壯 |
| GsTask 自適應 timeout | 70%→90% 效果已驗證 |
| Harness 唯一真相原則 | 不可改動 |
| git_snapshot / last_good_snapshot | rollback 精度高 |
| bridge_schema_validate（純 Python） | 格式把關輕量可靠 |

### 六大優化方向（按優先級）

```
P0（本週）：修資料品質
  ├─ O1：L4 ID 去重與重新編號
  └─ O2：dream cycle 頻率提升（每 5 episodde 觸發一次 --sleep）

P1（下週）：啟動 L5
  ├─ O3：降低 L4→L5 升級門檻（0.75→0.65），先讓 L5 有資料
  └─ O4：L4 confidence 校準（初始值 0.65→0.70，evidence_count 加速加分）

P2（兩週後）：打通 DiffIntel
  └─ O5：DiffIntel → L4 candidate 寫入通道

P3（三週後）：Total Recall 獨立開關
  └─ O6：FAST_MODE 拆成三個子開關（mempalace / bridge / recall）
```

---

## 三、詳細改善規劃

---

### O1：L4 去重補強與 ID 正規化

**問題**：L4 目前有 k_009 和 k_010 各出現兩次，造成 confidence 計算混亂。

**修正方式**：保留現有 `cmd_deep()` 的 `_deduplicate_l4()`，再補一個 `normalize_l4_ids()`。  
不要再做第二套平行去重，避免邏輯分叉。

**檔案**：`dream_cycle.py`  
**位置**：

1. `_deduplicate_l4()` 之後
2. `cmd_deep()` 寫回 L4 前

**實作邏輯**：
```python
def _normalize_l4_ids(items: list[dict]) -> list[dict]:
    """在既有去重後重新編號 active L4，保證 id 唯一且連續。"""
    normalized = []
    counter = 1
    for item in items:
        if item.get("status") == "active":
            item["id"] = f"k_{counter:03d}"
            counter += 1
        normalized.append(item)
    return normalized
```

**驗收標準**：`--status` 顯示 L4 無重複 id；所有 id 按 k_001 連續編號。

---

### O2：Dream Cycle 觸發頻率提升

**問題**：目前 `--bridge` 每 10 episode 才觸發一次 `--sleep`，L4 更新緩慢，導致 L5 餓死。

**現況**：
```
validated episodde 達 356 筆，但 L4 只有 10 筆 active
原因：--sleep 觸發次數少，且閾值偏高（l3_to_l4_min_count=3）
```

**修正方式**：

1. **`dream_cycle.py --bridge`**：觸發門檻從 10 episode → 5 episode
2. **`run_training.sh`**：每輪 benchmark 結束後，若本輪新增 validated episodes >= 5，再執行一次 `--sleep` + `--deep`

**檔案**：`dream_cycle.py`

```python
# 現況：validated_count % 10 == 0
# 改為：validated_count % 5 == 0
```

**`run_training.sh` 新增 Step**：
```bash
# Step 4.5（介於 benchmark 結束與 evening 之間）
if [ "$NEW_VALIDATED_EPISODES" -ge 5 ]; then
  echo "[Step 4.5] 本輪新增 validated episodes >= 5，執行記憶固化..."
  python3 dream_cycle.py --sleep
  python3 dream_cycle.py --deep
  python3 dream_cycle.py --wake
fi
```

**驗收標準**：每次跑完 10 題 benchmark，L4 至少新增 2 筆；30 題後 L5 有 1+ 筆 active。

---

### O3：L4→L5 升級與注入門檻調整（分階段）

**問題**：L5 策略庫完全空白，根因是 L4 的 confidence 最高只有 0.70，低於升 L5 門檻 0.75。

**策略**：降低「升級門檻」，但不要同步放寬到完全相同的「注入門檻」。  
目標是先讓 L5 有資料，再用 `needs_validation` 與 success/fail 追蹤決定哪些策略能進 prompt。

**檔案**：`dream_cycle.py`

```python
# 現況：
L4_TO_L5_MIN_CONF = 0.75

# 改為（升級門檻）：
l4_to_l5_min_conf = 0.65

# wake 注入門檻同步調整，但保守一些：
wake_l5_min_conf = 0.70
```

**同步修改**：初始 L5 標記 `needs_validation=true`

```python
item["needs_validation"] = True
item["promotion_source"] = "mid_conf_l4"
```

**安全機制**（避免品質下降）：
- L5 每次被用於推理後，`success_count` / `fail_count` 追蹤
- fail_rate > 35%（從 40% 降）時提前降回 L4
- `needs_validation=true` 的 L5 一次只注入 1 條，避免 prompt 污染

**驗收標準**：2 週內 L5 有 3+ 筆 active；fail_rate < 35%。

---

### O4：L4 Confidence 校準

**問題**：目前 L4 confidence 初始值偏低（0.55–0.65），衰減快，導致知識剛生成就開始走向退休。

**根因分析**：
```
evidence_count=23 的 k_001（bubble sort bug），confidence 只有 0.60
→ 23 個 episode 支撐，應該更有信心
→ 說明 evidence_count 對 confidence 的加分效果不足
```

**修正方式**：在 `dream_cycle.py` 的 `cmd_sleep()` Phase 1 prompt 中，增加 evidence_count 對 confidence 的指導：

```
evidence_count 對 confidence 的參考：
  1-2 筆  → confidence 0.55–0.60
  3-5 筆  → confidence 0.62–0.68
  6-10 筆 → confidence 0.70–0.73
  11+ 筆  → confidence 0.73–0.75（上限 0.75）
```

同時在 L4 合併與寫回時，重新計算 confidence：

```python
def _recalc_confidence(item: dict) -> float:
    ev = item.get("evidence_count", 1)
    base = item.get("confidence", 0.60)
    # evidence_count 加成：每 5 筆 +0.02，上限 0.75
    bonus = min(0.15, (ev // 5) * 0.02)
    return min(0.75, base + bonus)
```

**驗收標準**：evidence_count >= 10 的 L4 items，confidence >= 0.70。

---

### O5：DiffIntel → L4 candidate 寫入通道

**問題**：`git_diff_intel.py --analyze` 已能萃取「高風險改動 pattern」，但結果只印報表，未寫入 L4。

**現況資料**：
```
356 筆 L3 episodes → 每筆都有 git diff 記錄
但 git_diff_records.jsonl 的分析結果從未回饋到 L4
```

**修正方式**：在 `git_diff_intel.py` 增加 `--export-l4` 子指令，將高信心 pattern 寫入 `L4_knowledge.json`，但先標記為 `candidate` 或 `needs_validation`，不要直接進 active：

**檔案**：`git_diff_intel.py`

```python
def cmd_export_l4(records: list[dict], l4_path: Path) -> int:
    """
    從 DiffIntel 分析結果萃取 L4 candidates，寫入 L4_knowledge.json。
    規則：
      - 同一檔案組合 fail_rate >= 50%，累積 >= 6 筆 → 生成 avoid-pattern L4 candidate
      - 單一檔案 pass_rate >= 80%，累積 >= 6 筆 → 生成 action-pattern L4 candidate
    """
    patterns = _analyze_patterns(records)
    candidates = []
    for p in patterns:
        if p["fail_rate"] >= 0.50 and p["count"] >= 6:
            candidates.append({
                "pattern": f"同時修改 {'+'.join(p['files'])} 時 fail_rate={p['fail_rate']:.0%}，需謹慎",
                "scope": "Git Diff Intelligence",
                "confidence": min(0.70, 0.50 + p["count"] * 0.02),
                "evidence_count": p["count"],
                "source": "diff_intel",
                "status": "candidate",
                "needs_validation": true,
            })
    # 寫入 L4（不覆蓋已有 source != diff_intel 的項目）
    ...
    return len(candidates)
```

**整合到 `startup.sh evening`**：
```bash
# Step 4.6：DiffIntel → L4
python3 git_diff_intel.py --export-l4
```

**驗收標準**：`startup.sh analyze` 執行後，L4 新增 `source=diff_intel` 且 `status=candidate` 的 pattern；經過後續 `--sleep/--deep` 驗證後才進入 active。

---

### O6：FAST_MODE 拆成三個子開關

**問題**：`GOVERNOR_FAST_MODE=1` 是大開關，跳過 MemPalace、Bridge、**Total Recall** 全部，導致 Total Recall 從未在任何情況下執行，無法驗證其效果。

**修正方式**：拆成三個獨立環境變數：

| 舊開關 | 新開關 | 預設 | 說明 |
|---|---|---|---|
| `GOVERNOR_FAST_MODE=1` | `SKIP_MEMPALACE=1` | 0 | 跳過 `--update-mempalace` |
| （同上） | `SKIP_BRIDGE=1` | 0 | 跳過 `--bridge` 任務後處理 |
| （同上） | `SKIP_RECALL=0` | 0 | **預設不跳過**，benchmark 也啟用 |

**`benchmark_runner.py` 修改**：
```python
# 現況：
bench_env = {**os.environ, "GOVERNOR_FAST_MODE": "1"}

# 改為：
bench_env = {
    **os.environ,
    "SKIP_MEMPALACE": "1",   # benchmark 跳過 mempalace（快）
    "SKIP_BRIDGE": "1",      # benchmark 跳過 bridge（快）
    "SKIP_RECALL": "0",      # benchmark 保留 Total Recall（驗證效果）
}
```

**`main_loop.py` 修改**：
```python
# 現況：
FAST_MODE = os.environ.get("GOVERNOR_FAST_MODE", "0") == "1"

# 改為：
SKIP_MEMPALACE = os.environ.get("SKIP_MEMPALACE", "0") == "1"
SKIP_BRIDGE    = os.environ.get("SKIP_BRIDGE", "0") == "1"
SKIP_RECALL    = os.environ.get("SKIP_RECALL", "0") == "1"

# recall_past_failures() 中：
if SKIP_RECALL:
    return ""
```

**驗收標準**：benchmark 執行時 log 出現 `[Recall] 找到相似失敗案例，已注入 context`（或 `[Recall] 無相關過去失敗`），不再全部跳過。

---

## 四、實作排程

| 週次 | 優化項目 | 預計影響 | 驗收方式 |
|---|---|---|---|
| **Week 1** | O1（L4 去重）+ O2（觸發頻率）| L4 結構乾淨；新 benchmark 後 L4 增加 | `--status` 無重複 id |
| **Week 1** | O3（L5 升級/注入分階段）+ O4（confidence 校準）| L5 首次有資料且可控 | L5 active >= 3 |
| **Week 2** | O5（DiffIntel → L4）| 來自 diff 的 pattern 自動入庫 | L4 新增 source=diff_intel |
| **Week 2** | O6（FAST_MODE 拆分）| Total Recall 在 benchmark 啟用 | log 出現 `[Recall]` |
| **Week 3** | 跑完整 benchmark 驗證所有優化 | 預計通過率 90%→95%（string_ops 補全修正 + recall 輔助）| benchmark pass_rate |

---

## 五、不建議做的方向

以下是「看起來合理但實際會破壞系統優點」的方向，明確排除：

| 方向 | 排除原因 |
|---|---|
| 把 Governor 換成更小的模型 | 27b 在 integrate 步驟的品質優勢不可替代 |
| 移除 bridge_schema_validate | 純 Python 把關是最穩定的防線，移除後 L3 資料品質會下降 |
| 合併 L4 和 L5 為單層 | 兩層分離的設計使「規律」與「策略」職責清晰，合併會造成 Conflict Resolver 失效 |
| 把 total_recall 改為每次迭代注入 | 只有 iter=1 注入是刻意設計（避免 context 污染），不應改為每輪 |
| 直接把 L3 全部餵給 Governor 做 --sleep | MAX_SLEEP_GROUPS=10 的限制是 Cost Governor 的核心，移除會造成 token 超支 |
| 大幅增加 max_iter | 根因是 DDI decompose 不完整（已修），timeout 靠 GsTask，不靠增加迭代 |

---

## 六、預期優化後系統評分

| 模組 | 修正前 | 修正後預期 | 差值 | 主要原因 |
|---|---:|---:|---:|---|
| main_loop.py | 7.5 | 8.2 | +0.7 | Recall 可獨立驗證，context 注入更可控 |
| dream_cycle.py | 7.0 | 8.1 | +1.1 | L4 id 正規化、觸發頻率提升、L5 啟動 |
| benchmark_runner.py | 8.5 | 8.8 | +0.3 | FAST_MODE 子開關後可測 recall 效益 |
| git_diff_intel.py | 6.5 | 7.6 | +1.1 | 不再是資料孤島，但仍需後續驗證鏈 |
| mempalace.py | 6.0 | 6.4 | +0.4 | 受益於 recall / wake 品質，但不是主提升點 |
| task_intake.py | 5.5 | 5.8 | +0.3 | 仍非 benchmark 主路徑，提升有限 |
| L5 策略庫 | 3.0 | 6.8 | +3.8 | 門檻調整後開始有資料且可被注入 |
| L4 知識庫 | 6.0 | 7.8 | +1.8 | 去重、id 正規化、confidence 校準 |
| Total Recall | 4.5 | 7.2 | +2.7 | benchmark 可真正驗證，不再被總開關遮蔽 |

**整體目標評分：8.2/10**

> 註：這是保守預估。若 O5 直接寫 active、或 O3 只降升級門檻不管 wake 門檻，短期分數可能反而下降。

---

## 七、成功指標

```
短期（2 週）
  ✓ L4 無重複 id
  ✓ L4 evidence_count >= 10 的項目 confidence >= 0.70
  ✓ L5 active >= 3 筆
  ✓ benchmark log 中出現 [Recall] 字樣

中期（1 個月）
  ✓ benchmark pass_rate 穩定 >= 92%–95%
  ✓ string_ops 通過（DDI 補全驗證修正後）
  ✓ L5 被 PROMPT.md 注入至少一條策略
  ✓ DiffIntel 貢獻至少 2 筆 L4 pattern

長期（3 個月）
  ✓ L5 fail_rate < 35%（策略有效）
  ✓ L4 knowledge 持續累積至 20+ 筆
  ✓ 不同任務類型的 Total Recall 命中率 > 40%
```

---

## 附錄：完成進度總表

### 已完成修正

| 修正 | 狀態 | commit |
|---|---|---|
| keep_alive="0"（VRAM 競爭修正） | ✅ | 7a5573e |
| GOVERNOR_FAST_MODE→子開關（SKIP_MEMPALACE/BRIDGE/RECALL）| ✅ | 3e7088d |
| GsTask 自適應 timeout（600s→900/1200/1800s） | ✅ | 3e7088d |
| DDI-Decompose 補全驗證（test import 比對，string_ops 根因）| ✅ | 3e7088d |
| Total Recall TF-IDF + embedding recall 整合 | ✅ | 3e7088d |
| L4 ID 去重（O1：`_normalize_l4_ids`）| ✅ | 已在 dream_cycle.py |
| L5 升級門檻 0.75→0.65（O3）+ SelfOptimize 自調 | ✅ | 已在 dream_cycle.py |
| L5 啟動（0→6 筆 active，fail_rate 0–21%）| ✅ | 已驗證 |
| CompiledTruth 19 條 / RegressionGate / Timeline | ✅ | 已在 dream_cycle.py |
| Dream Cycle 自動化（bridge 每 5 ep 觸發 --sleep）| ✅ | 已在 dream_cycle.py |

### 自我感知三階段實作（2026-04-12）

| Phase | 項目 | 狀態 | commit |
|---|---|---|---|
| Phase 1 | `dream_cycle.py --diagnose`：掃描 traces，萃取 pipeline-level 模式，寫 L4 scope=pipeline | ✅ | 0b2206a |
| Phase 1 | 首次執行結果：發現 P001（136 次無差異死循環）、P002（39 次 string_ops 反覆失敗）| ✅ | 已驗證 |
| Phase 2 | `--diagnose --suggest`：Researcher 對 pipeline 問題生成修正建議 → `memory/system_improvements.json` | ✅ | 0b2206a |
| Phase 3 | meta-benchmark fixtures（ddi_coverage / ddi_no_change / recall_hit）| ✅ | 0b2206a |
| Phase 3 | `benchmarks/meta_suite.json` + `run_meta_benchmark.sh`（worktree 隔離執行）| ✅ | 0b2206a |

### 待執行項目

| 項目 | 說明 | 優先 |
|---|---|---|
| P0 S1+S2 實作 | Harness 錯誤注入 Stage 3 + 無差異 fallback（main_loop.py）| **P0** |
| O5 DiffIntel→L4 | `git_diff_intel.py --export-l4`，356 筆 diff 資料回饋知識庫 | P2 |
| meta-benchmark 實跑 | 執行 `run_meta_benchmark.sh` 驗證 Phase 3 三個 case | 下輪 |
| coding_suite_v2 全跑 | 驗證 P0 修正後 string_ops 能通過，達成 10/10 | 下輪 |
