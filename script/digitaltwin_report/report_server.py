import html
import json
import math
import os
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from typing import Any
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent


def fmt(v: Any, digits: int = 3) -> str:
    if isinstance(v, (int, float)):
        return f"{v:.{digits}f}"
    if v is None:
        return "N/A"
    return str(v)


def to_num(v: Any) -> float | None:
    if not isinstance(v, (int, float)):
        return None
    f = float(v)
    if not math.isfinite(f):
        return None
    return f


def human_key(key: str, lang: str) -> str:
    m = re.search(r"CSTR(\d+)_(\d+).*SNOx", key, re.IGNORECASE)
    if m:
        line = m.group(1)
        tank = m.group(2)
        return f"{line}线-池{tank}-SNOx" if lang == "zh" else f"Line{line}-Tank{tank}-SNOx"
    if "EFF" in key.upper():
        return f"出水-{key}" if lang == "zh" else f"EFF-{key}"
    return key


def pretty_means(means: dict[str, Any], lang: str, limit: int = 24) -> str:
    items = sorted(means.items())[:limit]
    if not items:
        return "N/A"
    return "; ".join(f"{human_key(k, lang)}={fmt(v)}" for k, v in items)


class DB:
    def __init__(self):
        self.conn = None
        self.err = ""

    def connect(self) -> bool:
        try:
            import mysql.connector  # type: ignore
        except Exception as exc:
            self.err = f"mysql connector missing: {exc}"
            return False
        cfg = {
            "user": os.environ.get("REPORT_DB_USER", "pyuser"),
            "password": os.environ.get("REPORT_DB_PASSWORD", "hdu14150)"),
            "host": os.environ.get("REPORT_DB_HOST", "localhost"),
            "database": os.environ.get("REPORT_DB_NAME", "thd_plant"),
            "port": int(os.environ.get("REPORT_DB_PORT", "4417")),
            "auth_plugin": os.environ.get("REPORT_DB_AUTH_PLUGIN", "mysql_native_password"),
        }
        try:
            self.conn = mysql.connector.connect(**cfg)
            return True
        except Exception as exc:
            self.err = f"db connect failed: {exc}"
            return False

    def close(self) -> None:
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass

    def all(self, sql: str, params: tuple = ()) -> list[tuple]:
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()

    def one(self, sql: str, params: tuple = ()) -> tuple | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def one_dict(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        cur = self.conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params)
            return cur.fetchone()
        finally:
            cur.close()

    def all_dict(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = self.conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params)
            rows = cur.fetchall()
            return list(rows) if rows else []
        finally:
            cur.close()

    def table_exists(self, table: str) -> bool:
        row = self.one(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s",
            (table,),
        )
        return bool(row and row[0] > 0)

    def columns(self, table: str) -> list[str]:
        return [r[0] for r in self.all(f"SHOW COLUMNS FROM `{table}`")]

    def columns_detail(self, table: str) -> dict[str, str]:
        rows = self.all(f"SHOW COLUMNS FROM `{table}`")
        return {r[0]: str(r[1]).lower() for r in rows}

    def detect_time_col(self, cols: list[str]) -> str | None:
        keys = [
            "time",
            "record_time",
            "timestamp",
            "create_time",
            "created_at",
            "update_time",
            "exec_time",
            "valid_from",
            "datetime",
            "dt",
            "t",
        ]
        lower = {c.lower(): c for c in cols}
        for k in keys:
            if k in lower:
                return lower[k]
        for c in cols:
            if "time" in c.lower():
                return c
        if "longtime" in {x.lower() for x in cols}:
            for c in cols:
                if c.lower() == "longtime":
                    return c
        return None

    def detect_order_col(self, cols: list[str]) -> str | None:
        lower = {c.lower(): c for c in cols}
        if "id" in lower:
            return lower["id"]
        for k in ["record_id", "task_id", "create_time", "created_at", "update_time", "time", "timestamp"]:
            if k in lower:
                return lower[k]
        return None

    def _is_compact_time(self, table: str, time_col: str) -> bool:
        t = self.columns_detail(table).get(time_col, "")
        return any(x in t for x in ["char", "text", "int", "bigint", "decimal"])

    def _day_cond(self, table: str, time_col: str, report_date: str) -> tuple[str, tuple]:
        if self._is_compact_time(table, time_col):
            return f"LEFT(CAST(`{time_col}` AS CHAR), 8) = %s", (report_date.replace("-", ""),)
        return f"DATE(`{time_col}`) = %s", (report_date,)

    def _recent_24h_cond(self, table: str, time_col: str) -> tuple[str, tuple]:
        if self._is_compact_time(table, time_col):
            return f"CAST(`{time_col}` AS CHAR) >= DATE_FORMAT(DATE_SUB(NOW(), INTERVAL 24 HOUR), '%Y%m%d%H%i')", ()
        return f"`{time_col}` >= DATE_SUB(NOW(), INTERVAL 24 HOUR)", ()

    def mean(self, table: str, col: str, time_col: str, report_date: str) -> float | None:
        cond, params = self._day_cond(table, time_col, report_date)
        row = self.one(f"SELECT AVG(`{col}`) FROM `{table}` WHERE {cond}", params)
        return to_num(row[0] if row else None)

    def max(self, table: str, col: str, time_col: str, report_date: str) -> float | None:
        cond, params = self._day_cond(table, time_col, report_date)
        row = self.one(f"SELECT MAX(`{col}`) FROM `{table}` WHERE {cond}", params)
        return to_num(row[0] if row else None)

    def mean_recent(self, table: str, col: str, order_col: str, limit: int = 288) -> float | None:
        row = self.one(
            f"SELECT AVG(`{col}`) FROM (SELECT `{col}` FROM `{table}` ORDER BY `{order_col}` DESC LIMIT %s) x",
            (limit,),
        )
        return to_num(row[0] if row else None)

    def max_recent(self, table: str, col: str, order_col: str, limit: int = 288) -> float | None:
        row = self.one(
            f"SELECT MAX(`{col}`) FROM (SELECT `{col}` FROM `{table}` ORDER BY `{order_col}` DESC LIMIT %s) x",
            (limit,),
        )
        return to_num(row[0] if row else None)

    def mean_recent_24h(self, table: str, col: str, time_col: str) -> float | None:
        cond, params = self._recent_24h_cond(table, time_col)
        row = self.one(f"SELECT AVG(`{col}`) FROM `{table}` WHERE {cond}", params)
        return to_num(row[0] if row else None)

    def max_recent_24h(self, table: str, col: str, time_col: str) -> float | None:
        cond, params = self._recent_24h_cond(table, time_col)
        row = self.one(f"SELECT MAX(`{col}`) FROM `{table}` WHERE {cond}", params)
        return to_num(row[0] if row else None)


def classify_peak(table: str, col: str, avg_v: float, max_v: float, lang: str) -> dict[str, str]:
    ratio = max_v / avg_v if avg_v > 0 else 0
    if ratio >= 2.5:
        level = "紧急" if lang == "zh" else "high"
    elif ratio >= 2.0:
        level = "重要" if lang == "zh" else "medium"
    else:
        level = "一般" if lang == "zh" else "low"

    name = col.lower()
    if "snox" in name and "cstr" in name:
        reason = "反硝化负荷波动或碳源分配不足" if lang == "zh" else "denitrification load swing or insufficient carbon distribution"
        action = "检查碳投加曲线、内回流和进水波动" if lang == "zh" else "check carbon dosing curve, internal recycle, and influent swings"
    elif "eff" in name:
        reason = "出水端指标异常上冲，可能存在前端负荷冲击" if lang == "zh" else "effluent-side spike likely related to upstream load shock"
        action = "核查沉淀与生化段联动，必要时加严出水监控" if lang == "zh" else "verify clarifier-biological linkage and tighten effluent monitoring"
    else:
        reason = "指标波动超阈" if lang == "zh" else "indicator fluctuation exceeded threshold"
        action = "复核传感器与工况切换记录" if lang == "zh" else "review sensor status and operation switch logs"

    return {
        "table": table,
        "column": col,
        "level": level,
        "peak": fmt(max_v),
        "mean": fmt(avg_v),
        "ratio": fmt(ratio, 2),
        "reason": reason,
        "diagnosis": action,
    }


def build_report(report_date: str, lang: str) -> dict[str, Any]:
    report = {
        "date": report_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db_ok": False,
        "db_msg": "",
        "recorder_means": {},
        "predict_means": {},
        "warnings": [],
        "sim_notes": [],
        "carbon": {
            "task_count": "N/A",
            "latest_task": "N/A",
            "snox_target": "N/A",
            "snox_target_num": None,
            "front_current": "N/A",
            "front_snox": "N/A",
            "front_snox_num": None,
            "snox_gap": "N/A",
            "schedule_next_60m": "N/A",
        },
        "carbon_chain": {},
        "carbon_diagnosis": [],
        "lab": {
            "inlet": {},
            "outlet": {},
            "pool_lines": [],
            "alerts": [],
            "note": "",
        },
    }

    db = DB()
    if not db.connect():
        report["db_msg"] = db.err
        report["sim_notes"] = ["数据库连接失败，未获取 sim_recorder/sim_predict 数据。"]
        report["carbon_diagnosis"] = ["数据库连接失败，无法读取 carbon_opt_* 三表，建议先验证 DB 参数与网络连通性。"]
        return report
    report["db_ok"] = True
    report["db_msg"] = "connected"

    try:
        if db.table_exists("carbon_opt_task"):
            cols = db.columns("carbon_opt_task")
            tcol = "valid_from" if "valid_from" in cols else db.detect_time_col(cols)
            if tcol:
                row = db.one(f"SELECT COUNT(*) FROM `carbon_opt_task` WHERE DATE(`{tcol}`)=%s", (report_date,))
                report["carbon"]["task_count"] = row[0] if row else 0
            row = db.one("SELECT status, remark, snox_limit FROM `carbon_opt_task` ORDER BY id DESC LIMIT 1")
            if row:
                report["carbon"]["latest_task"] = f"{row[0]} ({row[1]})"
                target_num = to_num(row[2])
                report["carbon"]["snox_target_num"] = target_num
                report["carbon"]["snox_target"] = fmt(target_num)
        if db.table_exists("carbon_opt_front"):
            row = db.one("SELECT exec_time, mode, carbon_2_cal, snox_sim FROM `carbon_opt_front` ORDER BY exec_time DESC LIMIT 1")
            if row:
                report["carbon"]["front_current"] = f"time={row[0]}, mode={row[1]}, carbon={fmt(row[2])}, snox={fmt(row[3])}"
                front_num = to_num(row[3])
                report["carbon"]["front_snox_num"] = front_num
                report["carbon"]["front_snox"] = fmt(front_num)
        if db.table_exists("carbon_opt_schedule"):
            row = db.one(
                "SELECT COUNT(*), AVG(carbon_2_cal), MIN(carbon_2_cal), MAX(carbon_2_cal) "
                "FROM `carbon_opt_schedule` WHERE execute_time >= NOW() AND execute_time <= DATE_ADD(NOW(), INTERVAL 60 MINUTE)"
            )
            if row:
                report["carbon"]["schedule_next_60m"] = f"points={row[0]}, avg={fmt(row[1])}, min={fmt(row[2])}, max={fmt(row[3])}"

        target = report["carbon"]["snox_target_num"]
        current = report["carbon"]["front_snox_num"]
        if target is not None and current is not None:
            gap = current - target
            report["carbon"]["snox_gap"] = fmt(gap)
        report["carbon_chain"] = {
            "step_1_task": "carbon_opt_task records one optimization/manual task",
            "step_2_schedule": "carbon_opt_schedule expands strategy into 10-minute execution points",
            "step_3_front": "carbon_opt_front upserts final effective points (history + forecast)",
            "step_4_dispatch": "set_carbon_opc reads front effective values for control dispatch",
            "current_target_snox": report["carbon"]["snox_target"],
            "current_front_snox": report["carbon"]["front_snox"],
            "current_gap": report["carbon"]["snox_gap"],
        }

        for table, key in [("sim_recorder", "recorder_means"), ("sim_predict", "predict_means")]:
            if not db.table_exists(table):
                report["sim_notes"].append(f"{table} not found")
                continue
            cols = db.columns(table)
            tcol = db.detect_time_col(cols)
            targets = [c for c in cols if ("snox" in c.lower() and "cstr" in c.lower()) or ("eff" in c.lower())]
            if not targets:
                report["sim_notes"].append(f"{table} no CSTR*_SNOx/EFF* columns matched")
                continue

            order_col = db.detect_order_col(cols)
            used_recent = False
            for c in targets:
                avg_v = db.mean(table, c, tcol, report_date) if tcol else None
                max_v = db.max(table, c, tcol, report_date) if tcol else None

                if avg_v is None and tcol:
                    avg_v = db.mean_recent_24h(table, c, tcol)
                    max_v = db.max_recent_24h(table, c, tcol)
                    used_recent = True
                if avg_v is None and order_col:
                    avg_v = db.mean_recent(table, c, order_col)
                    max_v = db.max_recent(table, c, order_col)
                    used_recent = True

                if avg_v is None:
                    continue

                report[key][c] = avg_v
                if max_v is not None and avg_v > 0 and max_v / avg_v >= 1.8:
                    report["warnings"].append(classify_peak(table, c, avg_v, max_v, lang))

            if used_recent:
                report["sim_notes"].append(f"{table} fallback to recent data window (24h/latest) due to empty day slice")

        report["carbon_diagnosis"] = build_carbon_diagnosis(report["carbon"], lang)

        # Lab overview from water_quality and water_quality_pool.
        if db.table_exists("water_quality"):
            w_cols = db.columns("water_quality")
            w_order = db.detect_order_col(w_cols) or db.detect_time_col(w_cols) or w_cols[0]
            row = db.one_dict(f"SELECT * FROM `water_quality` ORDER BY `{w_order}` DESC LIMIT 1")
            if row:
                inlet = {}
                outlet = {}
                for k, v in row.items():
                    if not isinstance(k, str):
                        continue
                    kl = k.lower()
                    num = to_num(v)
                    if num is None:
                        continue
                    if kl.startswith("inlet_"):
                        inlet[k] = num
                    if kl.startswith("outlet_"):
                        outlet[k] = num
                report["lab"]["inlet"] = inlet
                report["lab"]["outlet"] = outlet

                # join pool lines by water_quality_id.
                wqid = row.get("water_quality_id")
                if wqid is None:
                    wqid = row.get("id")
                if wqid is not None and db.table_exists("water_quality_pool"):
                    pools = db.all_dict("SELECT * FROM `water_quality_pool` WHERE `water_quality_id`=%s ORDER BY `key`", (wqid,))
                    lines = []
                    alerts = []
                    for p in pools:
                        key = p.get("key")
                        mlss = to_num(p.get("mlss"))
                        mlvss = to_num(p.get("mlvss"))
                        lines.append({"key": key, "mlss": mlss, "mlvss": mlvss})
                        if mlss is not None and (mlss < 500 or mlss > 8000):
                            alerts.append(f"line{key} mlss abnormal: {fmt(mlss)}")
                        if mlvss is not None and (mlvss < 300 or mlvss > 7000):
                            alerts.append(f"line{key} mlvss abnormal: {fmt(mlvss)}")
                        if mlss is not None and mlvss is not None and mlss > 0:
                            ratio = mlvss / mlss
                            if ratio < 0.4 or ratio > 0.9:
                                alerts.append(f"line{key} mlvss/mlss ratio unusual: {fmt(ratio, 2)}")
                    report["lab"]["pool_lines"] = lines
                    report["lab"]["alerts"] = alerts
            else:
                report["lab"]["note"] = "water_quality has no rows"
        else:
            report["lab"]["note"] = "water_quality table not found"
    finally:
        db.close()
    return report


def build_carbon_diagnosis(carbon: dict[str, Any], lang: str) -> list[str]:
    out: list[str] = []
    task_count = carbon.get("task_count")
    latest = str(carbon.get("latest_task", "N/A"))
    front = str(carbon.get("front_current", "N/A"))
    sched = str(carbon.get("schedule_next_60m", "N/A"))
    gap = to_num(carbon.get("snox_gap"))

    if isinstance(task_count, int):
        if task_count == 0:
            out.append("当日无新的加碳优化任务，建议检查优化触发条件与调度器。" if lang == "zh" else "No new carbon optimization task today; check trigger conditions and scheduler.")
        elif task_count <= 2:
            out.append("当日加碳任务次数较少，策略稳定性较高，但需关注进水突变场景。" if lang == "zh" else "Few carbon tasks today; strategy is relatively stable, but watch influent shock scenarios.")
        else:
            out.append("当日加碳任务较频繁，可能存在负荷波动或控制参数偏紧。" if lang == "zh" else "Frequent carbon tasks today may indicate load fluctuations or tight control settings.")

    if "SUCCESS" not in latest and latest != "N/A":
        out.append("最新任务状态非 SUCCESS，建议核查优化日志与配置输入。" if lang == "zh" else "Latest task status is not SUCCESS; verify optimization logs and config inputs.")

    if "N/A" in front:
        out.append("front 表未形成有效当前点，前端下发可能退化为默认值。" if lang == "zh" else "No effective current point in front table; front-end dispatch may fall back to defaults.")
    if "N/A" in sched:
        out.append("schedule 表未来 60 分钟计划缺失，建议检查任务展开写入逻辑。" if lang == "zh" else "No schedule points for next 60 minutes; check strategy expansion write logic.")
    if gap is not None:
        if gap > 1.5:
            out.append("当前 NOX 高于目标值较明显，建议优先核查碳源投加与回流设置。" if lang == "zh" else "Current NOX is materially above target; prioritize carbon dosing and recycle checks.")
        elif gap < -1.5:
            out.append("当前 NOX 低于目标值较多，存在过量投加风险，可评估降碳空间。" if lang == "zh" else "Current NOX is materially below target; assess potential over-dosing and cost optimization.")
        else:
            out.append("当前 NOX 与目标值接近，策略控制处于可接受区间。" if lang == "zh" else "Current NOX is close to target; control looks acceptable.")

    if not out:
        out.append("加碳三表数据完整，任务-计划-生效链路看起来正常。" if lang == "zh" else "Carbon 3-table chain looks complete and healthy.")
    return out


def llm_profile(provider: str) -> dict[str, str]:
    p = provider.lower()
    if p == "glm":
        return {
            "provider": "glm",
            "base": os.environ.get("REPORT_GLM_BASE_URL", "https://api.z.ai/api/paas/v4").rstrip("/"),
            "model": os.environ.get("REPORT_GLM_MODEL", "glm-4.7"),
            "key": os.environ.get("REPORT_GLM_API_KEY", os.environ.get("REPORT_LLM_API_KEY", "")),
        }
    return {
        "provider": "deepseek",
        "base": os.environ.get("REPORT_DEEPSEEK_BASE_URL", os.environ.get("REPORT_LLM_BASE_URL", "https://api.deepseek.com/v1")).rstrip("/"),
        "model": os.environ.get("REPORT_DEEPSEEK_MODEL", os.environ.get("REPORT_LLM_MODEL", "deepseek-chat")),
        "key": os.environ.get("REPORT_DEEPSEEK_API_KEY", os.environ.get("REPORT_LLM_API_KEY", "")),
    }


def llm_analysis(report: dict[str, Any], lang: str, provider: str) -> str:
    enabled = os.environ.get("REPORT_LLM_ENABLED", "0") == "1"
    if not enabled:
        return "LLM 分析未开启（REPORT_LLM_ENABLED=1）" if lang == "zh" else "LLM analysis disabled (REPORT_LLM_ENABLED=1)."
    profile = llm_profile(provider)
    key = profile["key"]
    if not key:
        return "未配置 REPORT_LLM_API_KEY。" if lang == "zh" else "REPORT_LLM_API_KEY is not configured."
    base = profile["base"]
    model = profile["model"]
    rules = load_text(lang, "REPORT_REQUIREMENTS")
    context = load_text(lang, "REPORT_CONTEXT")
    prompt = (
        "你是污水处理厂数字孪生运行专家。请严格按规范输出报告分析，必须包含“AI加碳策略专项分析”小节。重点解释三表给出的策略建议和当前状态，不要展开讲三表生成流程。请结合 NOX 目标差距给出可执行建议。"
        if lang == "zh"
        else "You are a wastewater digital twin operations expert. Include a dedicated 'AI Carbon Strategy' section focused on actionable strategy recommendation and current status from the three tables, not pipeline-generation details."
    )
    body = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "Be concise, practical, and professional."},
            {
                "role": "user",
                "content": (
                    prompt
                    + "\n\n[REPORT_RULES]\n"
                    + rules
                    + "\n\n[PROCESS_CONTEXT]\n"
                    + context
                    + "\n\n[DATA_JSON]\n"
                    + json.dumps(report, ensure_ascii=False)
                ),
            },
        ],
    }
    req = urlrequest.Request(
        f"{base}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=50) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"]
    except Exception as exc:
        return f"LLM 调用失败: {exc}" if lang == "zh" else f"LLM call failed: {exc}"


def load_text(lang: str, stem: str) -> str:
    if lang == "en":
        p = BASE_DIR / f"{stem}.en.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
    p = BASE_DIR / f"{stem}.zh.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""


def render(report: dict[str, Any], lang: str) -> str:
    zh = lang != "en"
    title = "数字孪生生产报告（Opencode）" if zh else "Digital Twin Production Report (Opencode)"
    rec = pretty_means(report["recorder_means"], lang, 24)
    pre = pretty_means(report["predict_means"], lang, 24)
    warns = report["warnings"]
    warning_html = "".join(
        f"<li><b>{html.escape(w['level'])}</b> | {html.escape(w['table'])}.{html.escape(w['column'])} "
        f"(peak={html.escape(w['peak'])}, mean={html.escape(w['mean'])}, ratio={html.escape(w['ratio'])})"
        f"<br/>原因: {html.escape(w['reason'])}<br/>工艺诊断: {html.escape(w['diagnosis'])}</li>"
        for w in warns
    ) if warns else ("<li>无明显峰值异常</li>" if zh else "<li>No obvious peak anomalies.</li>")
    carbon_diag_list = report.get("carbon_diagnosis", [])
    carbon_diag = "".join(f"<li>{html.escape(x)}</li>" for x in carbon_diag_list) or ("<li>暂无诊断</li>" if zh else "<li>No diagnosis available.</li>")
    sim_note_html = "".join(f"<li>{html.escape(x)}</li>" for x in report.get("sim_notes", []))
    provider = report.get("llm_provider", os.environ.get("REPORT_LLM_PROVIDER", "deepseek"))
    profile = llm_profile(str(provider))
    analysis = html.escape(llm_analysis(report, lang, str(provider))).replace("\n", "<br/>")
    model_line = f"{profile['model']} @ {profile['base']}"
    zh_url = f"/?date={report['date']}&lang=zh&provider={provider}"
    en_url = f"/?date={report['date']}&lang=en&provider={provider}"
    ds_url = f"/?date={report['date']}&lang={lang}&provider=deepseek"
    glm_url = f"/?date={report['date']}&lang={lang}&provider=glm"
    lab = report.get("lab", {})
    inlet = lab.get("inlet", {})
    outlet = lab.get("outlet", {})
    pool_lines = lab.get("pool_lines", [])
    lab_alerts = lab.get("alerts", [])
    lab_note = lab.get("note", "")
    inlet_text = "; ".join(f"{k}={fmt(v)}" for k, v in sorted(inlet.items())[:12]) or "N/A"
    outlet_text = "; ".join(f"{k}={fmt(v)}" for k, v in sorted(outlet.items())[:12]) or "N/A"
    pool_text = "; ".join(f"line{p.get('key')}: mlss={fmt(p.get('mlss'))}, mlvss={fmt(p.get('mlvss'))}" for p in pool_lines[:8]) or "N/A"
    lab_alert_text = "".join(f"<li>{html.escape(x)}</li>" for x in lab_alerts) or ("<li>无明显异常</li>" if zh else "<li>No obvious abnormality.</li>")
    if lab_note:
        lab_alert_text += f"<li>{html.escape(lab_note)}</li>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>{title}</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;background:#f6f8fb;margin:0;padding:20px;color:#1d2939}}
.card{{background:#fff;border:1px solid #d0d5dd;border-radius:12px;padding:14px;margin-bottom:10px}}
h2{{margin:0 0 8px 0;color:#1453c2}} .muted{{color:#475467;font-size:14px}} ul{{margin:0 0 0 18px;padding:0}}
.lang{{display:inline-block;margin-left:8px;padding:4px 10px;border:1px solid #1453c2;border-radius:14px;text-decoration:none;color:#1453c2}}
</style></head><body>
<div class="card"><h2>{title}
<a class="lang" href="{zh_url}">中文</a><a class="lang" href="{en_url}">English</a>
<a class="lang" href="{ds_url}">DeepSeek</a><a class="lang" href="{glm_url}">GLM</a>
</h2>
<div class="muted">date={report['date']} | generated={report['generated_at']}</div>
<div class="muted">db={report['db_ok']} ({html.escape(report['db_msg'])})</div>
<div class="muted">provider={provider} | model={html.escape(model_line)}</div></div>
<div class="card"><h2>{'运行概况（实验室）' if zh else 'Lab Running Overview'}</h2>
<div>inlet_*: {html.escape(inlet_text)}</div>
<div>outlet_*: {html.escape(outlet_text)}</div>
<div>pool(mlss/mlvss): {html.escape(pool_text)}</div>
<ul>{lab_alert_text}</ul></div>
<div class="card"><h2>{'AI加碳策略' if zh else 'AI Carbon Strategy'}</h2>
<div>task_count: {report['carbon']['task_count']}</div>
<div>latest_task: {html.escape(str(report['carbon']['latest_task']))}</div>
<div>snox_target: {html.escape(str(report['carbon']['snox_target']))}</div>
<div>snox_current(front): {html.escape(str(report['carbon']['front_snox']))}</div>
<div>snox_gap(current-target): {html.escape(str(report['carbon']['snox_gap']))}</div>
<div>front_current: {html.escape(str(report['carbon']['front_current']))}</div>
<div>schedule_next_60m: {html.escape(str(report['carbon']['schedule_next_60m']))}</div>
<ul>{carbon_diag}</ul></div>
<div class="card"><h2>{'运行态 sim_recorder 均值' if zh else 'sim_recorder means'}</h2><div>{html.escape(rec)}</div></div>
<div class="card"><h2>{'预测态 sim_predict 均值' if zh else 'sim_predict means'}</h2><div>{html.escape(pre)}</div></div>
<div class="card"><h2>{'数据处理说明' if zh else 'Data Processing Notes'}</h2><ul>{sim_note_html or ('<li>无</li>' if zh else '<li>None</li>')}</ul></div>
<div class="card"><h2>{'预警原因与工艺诊断' if zh else 'Warning Causes & Process Diagnosis'}</h2><ul>{warning_html}</ul></div>
<div class="card"><h2>{'LLM 专业分析' if zh else 'LLM Professional Analysis'}</h2><div>{analysis}</div></div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload: dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str, code: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        report_date = q.get("date", [date.today().isoformat()])[0]
        lang = q.get("lang", ["zh"])[0]
        provider = q.get("provider", [os.environ.get("REPORT_LLM_PROVIDER", "deepseek")])[0].lower()
        if provider not in ("deepseek", "glm"):
            provider = "deepseek"
        if lang not in ("zh", "en"):
            lang = "zh"
        if u.path in ("/", "/report"):
            report = build_report(report_date, lang)
            report["llm_provider"] = provider
            self._html(render(report, lang))
            return
        if u.path == "/api/report":
            report = build_report(report_date, lang)
            report["llm_provider"] = provider
            report["llm_analysis"] = llm_analysis(report, lang, provider)
            self._json(report)
            return
        if u.path == "/health":
            self._json({"ok": True, "service": "opencode_digitaltwin_report"})
            return
        self._json({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    host = os.environ.get("REPORT_HOST", "127.0.0.1")
    port = int(os.environ.get("REPORT_PORT", "8003"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"digitaltwin report server listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
