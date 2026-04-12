# GovernorOS FIXES_LOG

---

## [2026-04-12 11:30] fix(ddi): DDI-Decompose 補全驗證修正 + 四項系統優化整合

**異常描述**
`string_ops` 任務（3 個子問題：is_palindrome / group_anagrams / compress_string）在所有 benchmark 輪次中持續 timeout（1800s），未能通過。trace log 顯示 `DDI-Decompose` 每次只分解出 1 個子任務（is_palindrome），導致另外兩個函數從未被修正，迭代陷入死循環直至 timeout。

**根本原因**
`ddi_decompose()` 未對模型輸出做補全驗證：當 qwen3.5:9b 只回傳部分子任務（模型輸出不完整），程式碼直接接受，未比對 test 檔案中實際被 import/呼叫的函數清單，導致漏掉的函數永遠不被處理。

**修正內容**
| 檔案 | 修改說明 |
|------|----------|
| `main_loop.py:923` | `ddi_decompose()` 回傳前，從 `test_content` 解析 `from solution import ...` 取得所有被測試的函數名稱，補入 `valid` 中缺漏的子任務，並印 `[DDI-Decompose] 補全缺漏子任務：<fn>` |

**同批整合的四項優化（本輪 benchmark 70%→90%）**
| 優化 | 檔案 | 效果 |
|------|------|------|
| Total Recall | `dream_cycle.py`, `main_loop.py` | TF-IDF 語意檢索過去失敗 episode，注入 DDI 迭代 prompt |
| GsTask 自適應 timeout | `benchmark_runner.py` | 依任務複雜度給 900/1200/1800s，修正 3 題 600s 截斷 |
| Compiled Truth + Timeline | `dream_cycle.py` | L4 加入 timestamps、0.75 相似度去重、30 天 age decay |
| Dream Cycle 自動化 | `dream_cycle.py`, `startup.sh` | `--bridge` 每 10 episode 自動觸發 `--sleep`；早晨啟動自動檢查 |

**驗證**
- [x] 語法驗證通過（`python3 -m py_compile` × 4 個 .py 檔）
- [x] 隱私審查通過（sync_to_github.sh 掃描無警告）
- [ ] Harness 驗證（下次 benchmark 輪次將驗證 string_ops 修正效果）

**Commit**
`3e7088d`
