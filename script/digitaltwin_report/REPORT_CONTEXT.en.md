# Process and Data Context (English)

## Process background
- Wastewater digital twin with two phases and four biological lines.
- First 4 tanks are anaerobic/anoxic, last 6 are aerobic.
- Main focus: denitrification NOx behavior, effluent metrics, and carbon dosing strategy.

## Data scope
- Current operation source: `sim_recorder`
- Prediction source: `sim_predict`
- Report focus fields: `CSTR*_SNOx` and `EFF*`
- Peak rule: `daily_max >= 1.8 * daily_mean`

## Carbon strategy tables
1. `carbon_opt_task`: per-run summary
2. `carbon_opt_schedule`: 10-minute execution schedule
3. `carbon_opt_front`: current effective front/execution curve

## Style
- Actionable and engineering-oriented
- Explicit risk and priority

