# DigitalTwin Report Guideline Rules

## 1) Document Roles
- `README.zh.md`: usage/deployment/runbook only.
- `guideline_rule.md`: engineering guardrails and non-negotiable rules.

## 2) LLM & Language Switching Rules
- Chinese report is the primary generation result.
- `English` switch must be translation-only behavior.
- English translation must use local translator logic (no extra remote LLM API call).
- English switch must NOT trigger a second full report reasoning pass.
- English switch should avoid long blocking time.
- If provider is switched (`DeepSeek`/`GLM`), allow regeneration (with confirmation).
- `更新AI分析` means force-regenerate and ignore cache.

## 3) Missing-Data Reporting Rules
- Do not claim data missing unless there is explicit evidence in current data facts.
- If missing-facts list is empty, do not output “大面积缺失/运行数据缺失”.
- If missing exists, provide:
  - cause hypothesis
  - check steps
  - post-check likely result/risk

## 4) AI Carbon Strategy Section Rules
- Must keep section: `AI加碳策略专项分析（LLM）`.
- Must include 3 parts:
  - 现状判断
  - 未来1-3小时趋势判断
  - 可执行建议（动作、验证指标、风险）
- Focus on strategy recommendation/current status from 3 carbon tables.
- Do not over-explain internal table generation pipeline.

## 5) Sensor Validate Rules (Dash)
- Sensor view must be Dash-based and embedded in report sheet.
- Follow `Sensor_validate/sensor_check_dash.py` visual logic:
  - drift represented by colored `clean_data` segments
  - support UCL/LCL and outlier markers
  - include gauge
- `drift_prob/drift_degree` should not be shown as separate trend lines when style rule says segment-color mode.
- Grid style: keep horizontal grid only, no vertical grid.

## 6) UI/Style Rules
- Keep deep-blue industrial style.
- Section titles slightly larger; body font normal and consistent.
- Avoid accidental heading promotion for long narrative lines.
- `AI智慧分析综述` uses a subtle light-blue summary container.
- Highlight color (orange) is only for strongest key risk lines.
- Rule thresholds should be shown as inline `i` helper beside the exact abnormal sentence, labeled as `补充说明/Notes`.
- LLM text must be rendered as structured Markdown-like HTML (real headings/lists/strong/emphasis), not raw marker text.

## 7) RAG/Memory Rules
- RAG memory is enabled from S3 memory index/runs.
- Reports should explicitly leverage historical comparison where possible.
- Keep retention policy:
  - normal: 10 days
  - high confidence: 20 days
- Maintain daily cleanup and hot-summary compaction.

## 8) Testing Rules (Mandatory Before Deploy)
- Any behavior change must run at least:
  - Python syntax check (`py_compile`) for modified files.
  - One smoke call of `/api/report` and `/api/diagnostics`.
  - If UI changed, verify key HTML markers exist.
- If English switch behavior changes, test that it does not trigger second full generation.
- If sensor chart logic changes, verify Dash endpoint and visual mode assumptions.

## 9) Deployment Rules
- Sync only changed files first.
- Restart `ai_report` after sync.
- Verify service health and target endpoints after restart.
- Do not leave broken compose/service configuration.

## 10) Change Discipline
- Do not silently change agreed behavior.
- If a rule conflicts with a new request, explicitly confirm with user.
- Keep this file updated when new stable constraints are agreed.
