# Report Output Rules (English)

Generate a structured, actionable report from the JSON input. Avoid generic wording.

## Required sections
1. Verdict
- One line only: `stable` / `attention` / `risk`.

2. Current Running Status (from sim_recorder)
- Focus only on `CSTR*_SNOx` and `EFF*`.
- Highlight top 1-3 key observations.

3. Prediction Brief (from sim_predict)
- Means only. Keep concise.

4. Warning Causes & Process Diagnosis
- For each warning include:
- level (low/medium/high)
- likely cause
- actionable process diagnosis

5. Actions
- Up to 3 items, prioritized as P1/P2/P3.
- Each action must be concrete.

## Constraints
- Do not fabricate facts beyond input data.
- If `sim_recorder` or `sim_predict` is insufficient/N/A:
- explain likely causes (empty day data, field mismatch, collection lag)
- still provide diagnosis using `carbon_opt_task / carbon_opt_schedule / carbon_opt_front`
- Keep output concise and operational.

## Engineering Constraints (Mandatory)
- English switch is translation-only behavior and must not trigger a second full reasoning pass.
- Full LLM regeneration is allowed only when provider changes or when user clicks `Update AI Analysis`.
- Do not claim large-scale missing data unless explicit missing facts are present.
- Sensor Validate follows Dash rule: drift should be represented by segmented clean_data coloring (avoid extra drift_prob/drift_degree trend lines unless explicitly requested).
- After changes, run minimum checks: `py_compile` + smoke calls for `/api/report` and `/api/diagnostics`.
