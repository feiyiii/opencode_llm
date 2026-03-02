# DigitalTwin 报告服务（Opencode 内）

该服务用于生产版测试：
- 读取 MySQL（`sim_recorder` / `sim_predict` / `carbon_opt_*`）
- 生成中文/英文 HTML 报告（默认中文）
- 输出预警原因与工艺诊断
- 调用 DeepSeek 生成专业分析段落
- 支持基础登录鉴权（默认 `admin / 123`）

## 目录
- `report_server.py` 报告服务
- `start_report_server.bat` Windows 启动脚本
- `REPORT_REQUIREMENTS.zh.md/.en.md` 报告输出规范（每次 LLM 调用自动加载）
- `REPORT_CONTEXT.zh.md/.en.md` 工艺与数据上下文（每次 LLM 调用自动加载）

## Windows 运行
1. 安装依赖
```bat
pip install mysql-connector-python
```

2. 设置环境变量（示例）
```bat
set REPORT_DB_HOST=localhost
set REPORT_DB_PORT=4417
set REPORT_DB_USER=pyuser
set REPORT_DB_PASSWORD=your_password
set REPORT_DB_NAME=thd_plant

set REPORT_LLM_ENABLED=1
set REPORT_LLM_BASE_URL=https://api.deepseek.com/v1
set REPORT_LLM_API_KEY=your_deepseek_key
set REPORT_LLM_MODEL=deepseek-chat
set REPORT_PORT=8003

set REPORT_AUTH_ENABLED=1
set REPORT_AUTH_USER=admin
set REPORT_AUTH_PASSWORD=123
```

3. 启动
```bat
start_report_server.bat
```

4. 访问
- 中文（默认）：`http://127.0.0.1:8003/`
- 英文：`http://127.0.0.1:8003/?lang=en`
- JSON：`http://127.0.0.1:8003/api/report`

## 报告内容规则
1. `sim_recorder`：重点展示 `CSTR*_SNOx` 与 `EFF*` 均值（当前运行态）
2. `sim_predict`：仅展示均值（预测态简述）
3. 峰值预警：
- 规则：`daily_max >= 1.8 * daily_mean`
- 输出：等级、原因、工艺诊断建议

## 规则与上下文如何生效
- 服务每次调用 LLM 时会自动读取：
- `REPORT_REQUIREMENTS.*.md`
- `REPORT_CONTEXT.*.md`
- `Data_structure_enriched.docx` / `Data_structure.docx` / `docs/references/data_structure.md`（存在即自动加载）
- 因此你后续只改这两个文件，就能更新报告写作要求，不需要改 Python 代码。

## EC2 最小部署（推荐）
仅复制以下目录即可，不需要整个 opencode：
- `script/digitaltwin_report`

Linux 启动示例：
```bash
cd script/digitaltwin_report
pip install mysql-connector-python

export REPORT_HOST=0.0.0.0
export REPORT_PORT=8003
export REPORT_AUTH_ENABLED=1
export REPORT_AUTH_USER=admin
export REPORT_AUTH_PASSWORD=123
export REPORT_LLM_ENABLED=1
export REPORT_DEEPSEEK_API_KEY=your_key

python report_server.py
```

## 安全建议
- 不要把 API Key 写入代码或提交到仓库。
- 使用环境变量注入 Key。
- 你之前暴露过的 key 建议立即轮换。
