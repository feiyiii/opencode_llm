import html
import json
import math
import os
import base64
import hmac
import gzip
import hashlib
from io import BytesIO
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import zipfile
from typing import Any
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlparse
try:
    import boto3  # type: ignore
except Exception:
    boto3 = None

BASE_DIR = Path(__file__).resolve().parent
DT_ROOT = BASE_DIR.parents[3] if len(BASE_DIR.parents) > 3 else BASE_DIR
ANALYSIS_CACHE: dict[str, str] = {}
LAST_GOOD_ANALYSIS: dict[str, str] = {}
_UI_MESSAGES_CACHE: dict[str, dict[str, Any]] = {}
PROCESS_START_TS = datetime.now()

DEFAULT_UI_MESSAGES: dict[str, dict[str, Any]] = {
    "zh": {
        "loading_title": "报告生成中",
        "loading_steps": [
            "正在读取 S3 最新数据包...",
            "正在连接 LLM 引擎（DeepSeek/GLM）...",
            "正在生成 AI 工艺分析，请稍候...",
            "正在整理报告页面，即将展示...",
        ],
        "hint_slow": "若超过 2 分钟，请检查网络和模型接口状态。",
        "timeout_title": "报告生成超过 5 分钟，任务可能阻塞。",
        "timeout_items": [
            "检查 LLM API 是否可用，是否超时或配额不足。",
            "检查 S3 latest/manifest 是否存在且可读取。",
            "检查 report_server 进程或 Docker/进程管理器状态。",
            "必要时切换模型后重试，或先生成基础报告。",
        ],
        "error_prefix": "报告生成失败：",
        "diag_label": "运维排查建议",
    },
    "en": {
        "loading_title": "Generating Report",
        "loading_steps": [
            "Reading latest S3 datasets...",
            "Connecting LLM provider (DeepSeek/GLM)...",
            "Generating AI process analysis, please wait...",
            "Preparing report rendering...",
        ],
        "hint_slow": "If this takes over 2 minutes, check network and model API status.",
        "timeout_title": "Generation exceeded 5 minutes; the task may be blocked.",
        "timeout_items": [
            "Check LLM API availability, timeout and quota.",
            "Check whether S3 latest/manifest exists and is readable.",
            "Check report_server process or Docker/process manager status.",
            "Try switching model, or generate base report first.",
        ],
        "error_prefix": "Report generation failed: ",
        "diag_label": "Ops Checklist",
    },
}


def load_env_from_json() -> None:
    path = os.environ.get("REPORT_CONFIG_JSON", str(BASE_DIR / "report_config.json")).strip()
    p = Path(path)
    if not p.exists():
        return
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    env_obj = cfg.get("env") if isinstance(cfg, dict) and isinstance(cfg.get("env"), dict) else cfg
    if not isinstance(env_obj, dict):
        return
    for k, v in env_obj.items():
        if not isinstance(k, str):
            continue
        if v is None:
            continue
        os.environ.setdefault(k, str(v))


def load_ui_messages(lang: str) -> dict[str, Any]:
    key = "en" if lang == "en" else "zh"
    if key in _UI_MESSAGES_CACHE:
        return _UI_MESSAGES_CACHE[key]
    msg = dict(DEFAULT_UI_MESSAGES[key])
    path = BASE_DIR / f"messages.{key}.json"
    if path.exists():
        try:
            file_obj = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(file_obj, dict):
                msg.update(file_obj)
        except Exception:
            pass
    _UI_MESSAGES_CACHE[key] = msg
    return msg


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


def parse_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    s = str(v).strip() if v is not None else ""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def compute_adjustment_stats(values: list[float], eps: float = 0.02) -> tuple[int, float, float]:
    vals = [v for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))]
    if len(vals) < 2:
        return 0, 0.0, (vals[0] - vals[0] if vals else 0.0)
    events = 0
    for i in range(1, len(vals)):
        if abs(vals[i] - vals[i - 1]) >= eps:
            events += 1
    ratio = events / max(1, len(vals) - 1)
    span = max(vals) - min(vals)
    return events, ratio, span


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


def build_report_mysql(report_date: str, lang: str) -> dict[str, Any]:
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
            "adjust_events": "N/A",
            "adjust_ratio": "N/A",
            "adjust_span": "N/A",
        },
        "carbon_chain": {},
        "carbon_diagnosis": [],
        "lab": {
            "inlet": {},
            "outlet": {},
            "pool_lines": [],
            "alerts": [],
            "insights": [],
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
            rows = db.all(
                "SELECT carbon_2_cal FROM `carbon_opt_schedule` "
                "WHERE execute_time >= NOW() AND execute_time <= DATE_ADD(NOW(), INTERVAL 60 MINUTE) "
                "ORDER BY execute_time ASC"
            )
            series = [to_num(r[0]) for r in rows if r and to_num(r[0]) is not None]
            if series:
                ev, rt, sp = compute_adjustment_stats([x for x in series if x is not None])
                report["carbon"]["adjust_events"] = ev
                report["carbon"]["adjust_ratio"] = fmt(rt, 2)
                report["carbon"]["adjust_span"] = fmt(sp)

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
                try:
                    hist_rows = db.all_dict(
                        f"SELECT * FROM `water_quality` ORDER BY `{w_order}` DESC LIMIT 30"
                    )
                except Exception:
                    hist_rows = [row]
                report["lab"]["insights"] = _compute_lab_insights(hist_rows, row, lang)
                report["lab"]["plotly"] = _build_lab_plotly(hist_rows, max_days=10)

                # join pool lines by water_quality_id.
                wqid = row.get("water_quality_id")
                if wqid is None:
                    wqid = row.get("id")
                if db.table_exists("water_quality_pool"):
                    pools = db.all_dict("SELECT * FROM `water_quality_pool` ORDER BY `water_quality_id` DESC LIMIT 2000")
                    day_map: dict[str, str] = {}
                    latest_day_id = _as_day_id(_ci_get(row, "local_day_id", "day_id", "date_id"))
                    lines, notes = _build_pool_lines_with_fallback(pools, wqid, day_map, latest_day_id)
                    alerts = []
                    for item in lines:
                        key = item.get("key")
                        mlss = item.get("mlss")
                        mlvss = item.get("mlvss")
                        if mlss is not None and (mlss < 500 or mlss > 8000):
                            alerts.append(f"line{key} mlss abnormal: {fmt(mlss)}")
                        if mlvss is not None and (mlvss < 300 or mlvss > 7000):
                            alerts.append(f"line{key} mlvss abnormal: {fmt(mlvss)}")
                        if mlss is not None and mlvss is not None and mlss > 0:
                            ratio = mlvss / mlss
                            if ratio < 0.4 or ratio > 0.9:
                                alerts.append(f"line{key} mlvss/mlss ratio unusual: {fmt(ratio, 2)}")
                    missing_mlvss = [str(x.get("key")) for x in lines if x.get("mlvss") is None]
                    if missing_mlvss:
                        alerts.append(
                            "mlvss 缺失线路: "
                            + ",".join(missing_mlvss)
                            + "。可能原因：实验室该批次未回填 parm_mlvss 或同步字段未写入；建议核对 water_quality_pool.parm_mlvss 与采样回填链路。"
                        )
                    report["lab"]["pool_lines"] = lines
                    report["lab"]["alerts"] = alerts
                    if notes:
                        report["lab"]["note"] = "; ".join(notes)
            else:
                report["lab"]["note"] = "water_quality has no rows"
        else:
            report["lab"]["note"] = "water_quality table not found"
    finally:
        db.close()
    return report


def _base_report(report_date: str) -> dict[str, Any]:
    return {
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
            "adjust_events": "N/A",
            "adjust_ratio": "N/A",
            "adjust_span": "N/A",
        },
        "carbon_chain": {},
        "carbon_diagnosis": [],
        "lab": {
            "inlet": {},
            "outlet": {},
            "pool_lines": [],
            "alerts": [],
            "insights": [],
            "note": "",
            "plotly": {"x": [], "inlet": {}, "outlet": {}},
        },
        "history_baseline": [],
    }


def _try_float(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return to_num(v)
    if isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in {"nan", "null", "none", "n/a"}:
            return None
        try:
            return to_num(float(s))
        except Exception:
            return None
    return None


def _ci_get(d: dict[str, Any], *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    lower_map = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        kl = k.lower()
        if kl in lower_map:
            return lower_map[kl]
    return None


def _compute_lab_insights(
    rows: list[dict[str, Any]],
    current: dict[str, Any],
    lang: str,
) -> list[str]:
    insights: list[str] = []

    def fv(d: dict[str, Any], key: str) -> float | None:
        return _try_float(_ci_get(d, key))

    in_cod = fv(current, "inlet_cod")
    in_nh3 = fv(current, "inlet_nh3n")
    day_id = _as_day_id(_ci_get(current, "local_day_id", "day_id", "date_id"))

    if in_cod is not None and in_cod > 0 and in_nh3 is not None:
        ratio = in_nh3 / in_cod
        if ratio < 0.03:
            r_note = "偏低，可能碳源相对过量或氨氮负荷较轻。"
        elif ratio > 0.12:
            r_note = "偏高，氨氮负荷较重，需关注硝化与碳源分配。"
        else:
            r_note = "处于城镇污水常见范围。"
        insights.append(f"inlet NH3/COD={ratio:.3f}，{r_note}")

    # Compare current values against previous days mean (up to 3 days).
    if rows:
        recent_by_day: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = _as_day_id(_ci_get(r, "local_day_id", "day_id", "date_id"))
            if not d:
                continue
            if d not in recent_by_day:
                recent_by_day[d] = r
        days = sorted(recent_by_day.keys(), reverse=True)
        prev_days = [d for d in days if d != day_id][:3]
        if prev_days:
            prev_in_cod = [fv(recent_by_day[d], "inlet_cod") for d in prev_days]
            prev_in_nh3 = [fv(recent_by_day[d], "inlet_nh3n") for d in prev_days]
            prev_in_cod = [x for x in prev_in_cod if x is not None]
            prev_in_nh3 = [x for x in prev_in_nh3 if x is not None]
            if in_cod is not None and prev_in_cod:
                m = sum(prev_in_cod) / len(prev_in_cod)
                if m > 0:
                    dp = (in_cod - m) / m * 100
                    insights.append(f"inlet COD 较前{len(prev_in_cod)}日均值变化 {dp:+.1f}%。")
            if in_nh3 is not None and prev_in_nh3:
                m = sum(prev_in_nh3) / len(prev_in_nh3)
                if m > 0:
                    dp = (in_nh3 - m) / m * 100
                    insights.append(f"inlet NH3-N 较前{len(prev_in_nh3)}日均值变化 {dp:+.1f}%。")

    # Removal efficiency quick check.
    checks = [
        ("COD", "inlet_cod", "outlet_cod", 80.0),
        ("NH3-N", "inlet_nh3n", "outlet_nh3n", 90.0),
        ("TN", "inlet_tn", "outlet_tn", 50.0),
        ("TP", "inlet_tp", "outlet_tp", 80.0),
        ("SS", "inlet_ss", "outlet_ss", 90.0),
    ]
    for name, kin, kout, th in checks:
        vi = fv(current, kin)
        vo = fv(current, kout)
        if vi is None or vo is None or vi <= 0:
            continue
        rr = (vi - vo) / vi * 100.0
        mark = "（偏低，建议关注）" if rr < th else ""
        insights.append(f"{name} 去除率约 {rr:.1f}% {mark}".strip())

    return insights[:10]


def _build_history_baseline(report: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    lab = report.get("lab", {})
    insights = [str(x) for x in lab.get("insights", []) if isinstance(x, str)]
    for x in insights:
        if ("较前" in x) or ("去除率" in x) or ("NH3/COD" in x):
            lines.append(x)
    rec = report.get("recorder_means", {})
    pre = report.get("predict_means", {})
    for k in ("CSTR3_4_SNOx", "EFF_TN", "EFF_TCOD"):
        rv = _try_float(rec.get(k)) if isinstance(rec, dict) else None
        pv = _try_float(pre.get(k)) if isinstance(pre, dict) else None
        if rv is None or pv is None or rv == 0:
            continue
        dp = (pv - rv) / abs(rv) * 100.0
        lines.append(f"{k} 预测较运行态变化 {dp:+.1f}%（run={fmt(rv)} -> pred={fmt(pv)}）")
    carbon = report.get("carbon", {})
    gap = _try_float(carbon.get("snox_gap")) if isinstance(carbon, dict) else None
    tgt = _try_float(carbon.get("snox_target")) if isinstance(carbon, dict) else None
    cur = _try_float(carbon.get("front_snox")) if isinstance(carbon, dict) else None
    if gap is not None and tgt is not None and cur is not None:
        lines.append(f"NOX目标差距：当前 {fmt(cur)} vs 目标 {fmt(tgt)}，gap={fmt(gap)}")
    dedup: list[str] = []
    seen = set()
    for x in lines:
        if x not in seen:
            dedup.append(x)
            seen.add(x)
    return dedup[:8]


def _build_lab_plotly(rows: list[dict[str, Any]], max_days: int = 10) -> dict[str, Any]:
    by_day: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = _as_day_id(_ci_get(r, "local_day_id", "day_id", "date_id"))
        if not d:
            continue
        if d not in by_day:
            by_day[d] = r
    days = sorted(by_day.keys())[-max_days:]
    x = [f"{d[0:4]}-{d[4:6]}-{d[6:8]}" for d in days]

    def series(prefix: str, key: str) -> list[float | None]:
        out: list[float | None] = []
        col = f"{prefix}_{key}"
        for d in days:
            out.append(_try_float(_ci_get(by_day[d], col)))
        return out

    keys = ["cod", "nh3n", "tn", "tp", "ss", "ph"]
    inlet = {k: series("inlet", k) for k in keys}
    outlet = {k: series("outlet", k) for k in keys}
    return {"x": x, "inlet": inlet, "outlet": outlet}


def _line_id(v: Any) -> int | None:
    if isinstance(v, int):
        return v if 1 <= v <= 4 else None
    if isinstance(v, float) and float(v).is_integer():
        iv = int(v)
        return iv if 1 <= iv <= 4 else None
    if isinstance(v, str):
        s = v.strip().lower()
        if s.isdigit():
            iv = int(s)
            return iv if 1 <= iv <= 4 else None
        m = re.search(r"([1-4])", s)
        if m:
            return int(m.group(1))
    return None


def _as_day_id(v: Any) -> str:
    s = str(v).strip() if v is not None else ""
    if len(s) == 8 and s.isdigit():
        return s
    return ""


def _fmt_day_zh(day_id: str) -> str:
    if len(day_id) == 8 and day_id.isdigit():
        y = int(day_id[0:4])
        m = int(day_id[4:6])
        d = int(day_id[6:8])
        return f"{y}年{m}月{d}日"
    return day_id


def _build_pool_lines_with_fallback(
    rows: list[dict[str, Any]],
    latest_wqid: Any,
    day_map: dict[str, str],
    latest_day_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    # Rows are sorted latest -> older. For each line, use latest batch value first;
    # if missing, backfill from nearest historical non-null value.
    by_line: dict[int, list[dict[str, Any]]] = {1: [], 2: [], 3: [], 4: []}
    for r in rows:
        line = _line_id(_ci_get(r, "key", "line", "pool_key"))
        if line is None:
            continue
        by_line[line].append(r)

    def latest_metric(
        line_rows: list[dict[str, Any]],
        metric_keys: tuple[str, ...],
        latest_id: Any,
    ) -> tuple[float | None, Any | None, bool]:
        latest_val = None
        latest_src = None
        latest_missing = False
        # Try same-batch value first.
        if latest_id is not None:
            for rr in line_rows:
                rid = _ci_get(rr, "water_quality_id", "id")
                if str(rid) != str(latest_id):
                    continue
                latest_val = _try_float(_ci_get(rr, *metric_keys))
                latest_src = rid
                if latest_val is None:
                    latest_missing = True
                break
        if latest_val is not None:
            return latest_val, latest_src, latest_missing
        # Backfill by nearest previous non-null value.
        for rr in line_rows:
            v = _try_float(_ci_get(rr, *metric_keys))
            if v is not None:
                return v, _ci_get(rr, "water_quality_id", "id"), latest_missing
        return None, None, latest_missing

    lines: list[dict[str, Any]] = []
    notes: list[str] = []
    for i in [1, 2, 3, 4]:
        line_rows = by_line[i]
        mlss, mlss_src, _ = latest_metric(line_rows, ("parm_mlss", "mlss", "MLSS", "tss", "parm_tss"), latest_wqid)
        mlvss, mlvss_src, mlvss_latest_missing = latest_metric(line_rows, ("parm_mlvss", "mlvss", "MLVSS", "vss", "parm_vss"), latest_wqid)
        lines.append({"key": i, "mlss": mlss, "mlvss": mlvss})

        if latest_wqid is not None:
            src_day = day_map.get(str(mlvss_src), "")
            if not src_day and mlvss_src is not None:
                src_day = _as_day_id(mlvss_src)
            if mlvss is not None and mlvss_src is not None and str(mlvss_src) != str(latest_wqid):
                if latest_day_id:
                    notes.append(
                        f"line{i} 今日（{_fmt_day_zh(latest_day_id)}）mlvss无有效值，"
                        f"已回溯至 {_fmt_day_zh(src_day or '历史最近日')} 的最近非空值 {fmt(mlvss)}。"
                    )
                else:
                    notes.append(
                        f"line{i} 当次 mlvss 无有效值，已回溯至最近非空日 {(_fmt_day_zh(src_day) if src_day else '历史最近日')} 值 {fmt(mlvss)}。"
                    )
            elif mlvss is None:
                if latest_day_id:
                    notes.append(f"line{i} 今日（{_fmt_day_zh(latest_day_id)}）mlvss无有效值，且历史记录中也未找到可回溯值。")
                else:
                    notes.append(f"line{i} mlvss 无有效值，且历史记录中也未找到可回溯值。")
            elif mlvss_latest_missing and latest_day_id:
                notes.append(f"line{i} 今日（{_fmt_day_zh(latest_day_id)}）mlvss未测或未回填，当前展示值已取最近可用值 {fmt(mlvss)}。")

    if all(x.get("mlss") is None and x.get("mlvss") is None for x in lines):
        notes.append("water_quality_pool has no valid parm_mlss/parm_mlvss values")
    return lines, notes


def _s3_client():
    if boto3 is None:
        raise RuntimeError("boto3 not installed")
    region = os.environ.get("REPORT_S3_REGION", "cn-north-1")
    endpoint = os.environ.get("REPORT_S3_ENDPOINT_URL", "").strip()
    if not endpoint and region.startswith("cn-"):
        endpoint = f"https://s3.{region}.amazonaws.com.cn"
    return boto3.client(
        "s3",
        aws_access_key_id=os.environ.get("REPORT_S3_ACCESS_KEY_ID", os.environ.get("AWS_ACCESS_KEY_ID", "")),
        aws_secret_access_key=os.environ.get("REPORT_S3_SECRET_ACCESS_KEY", os.environ.get("AWS_SECRET_ACCESS_KEY", "")),
        region_name=region,
        endpoint_url=endpoint or None,
    )


def _s3_get_json(client, bucket: str, key: str) -> dict[str, Any]:
    obj = client.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def _s3_get_jsonl_rows(client, bucket: str, key: str) -> list[dict[str, Any]]:
    obj = client.get_object(Bucket=bucket, Key=key)
    raw = obj["Body"].read()
    if key.endswith(".gz"):
        raw = gzip.decompress(raw)
    rows: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except Exception:
            continue
    return rows


def load_sensor_validate_from_s3(max_points: int = 1200) -> dict[str, Any]:
    bucket = os.environ.get("REPORT_S3_BUCKET", "llmdata")
    prefix = os.environ.get("REPORT_S3_PREFIX", "digitaltwin_exports").strip("/")
    latest_key = os.environ.get("REPORT_S3_LATEST_KEY", f"{prefix}/latest/manifest.json")
    try:
        client = _s3_client()
        manifest, used_manifest_key = _load_manifest_with_success_fallback(client, bucket, prefix, latest_key)
        datasets = {d.get("name"): d for d in manifest.get("datasets", []) if isinstance(d, dict)}
        ds = datasets.get("sensor_validate")
        if not ds or not ds.get("s3_key"):
            return {
                "ok": False,
                "message": "sensor_validate dataset not found in latest manifest",
                "used_manifest_key": used_manifest_key,
                "points": [],
            }
        rows = _s3_get_jsonl_rows(client, bucket, str(ds["s3_key"]))
    except Exception as exc:
        return {"ok": False, "message": str(exc), "points": []}

    points: list[dict[str, Any]] = []
    for r in rows:
        ts_raw = _ci_get(r, "longtime", "time", "timestamp", "ts")
        dt = parse_dt(ts_raw)
        if dt is None:
            continue
        points.append(
            {
                "t": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "clean_data": _try_float(_ci_get(r, "clean_data")),
                "drift_prob": _try_float(_ci_get(r, "clean_data_drift_prob", "drift_prob")),
                "drift_degree": _try_float(_ci_get(r, "clean_data_drift_degree", "drift_degree")),
            }
        )
    points = sorted(points, key=lambda x: x["t"])
    if len(points) > max_points:
        step = max(1, len(points) // max_points)
        points = points[::step]

    latest = points[-1] if points else {}
    clean_vals = [x["clean_data"] for x in points if isinstance(x.get("clean_data"), (int, float))]
    return {
        "ok": True,
        "used_manifest_key": used_manifest_key,
        "source_key": ds.get("s3_key"),
        "rows": len(rows),
        "points": points,
        "summary": {
            "latest_time": latest.get("t", ""),
            "latest_clean_data": latest.get("clean_data"),
            "latest_drift_prob": latest.get("drift_prob"),
            "latest_drift_degree": latest.get("drift_degree"),
            "clean_mean": (sum(clean_vals) / len(clean_vals)) if clean_vals else None,
        },
    }


def _pick_first(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return rows[0] if rows else None


def _load_manifest_with_success_fallback(client, bucket: str, prefix: str, latest_key: str) -> tuple[dict[str, Any], str]:
    latest = _s3_get_json(client, bucket, latest_key)
    latest_status = str(latest.get("status", "SUCCESS")).upper()
    latest_datasets = latest.get("datasets", [])
    if latest_status in {"SUCCESS", "PARTIAL"} and isinstance(latest_datasets, list) and len(latest_datasets) > 0:
        return latest, latest_key

    run_prefix = f"{prefix}/runs/"
    manifest_keys: list[str] = []
    token = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": run_prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if key.endswith("/manifest.json"):
                manifest_keys.append(key)
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")

    for key in sorted(manifest_keys, reverse=True):
        if key == latest_key:
            continue
        try:
            candidate = _s3_get_json(client, bucket, key)
        except Exception:
            continue
        status = str(candidate.get("status", "SUCCESS")).upper()
        datasets = candidate.get("datasets", [])
        if status in {"SUCCESS", "PARTIAL"} and isinstance(datasets, list) and len(datasets) > 0:
            return candidate, key
    return latest, latest_key


def _build_report_from_s3(report_date: str, lang: str) -> dict[str, Any]:
    report = _base_report(report_date)
    bucket = os.environ.get("REPORT_S3_BUCKET", "llmdata")
    prefix = os.environ.get("REPORT_S3_PREFIX", "digitaltwin_exports").strip("/")
    latest_key = os.environ.get("REPORT_S3_LATEST_KEY", f"{prefix}/latest/manifest.json")

    try:
        client = _s3_client()
        manifest, used_manifest_key = _load_manifest_with_success_fallback(client, bucket, prefix, latest_key)
        datasets = {d.get("name"): d for d in manifest.get("datasets", []) if isinstance(d, dict)}
        data_rows: dict[str, list[dict[str, Any]]] = {}
        for name in [
            "sim_recorder",
            "sim_predict",
            "carbon_opt_task",
            "carbon_opt_schedule",
            "carbon_opt_front",
            "water_quality",
            "water_quality_pool",
        ]:
            ds = datasets.get(name)
            if not ds or not ds.get("s3_key"):
                continue
            data_rows[name] = _s3_get_jsonl_rows(client, bucket, str(ds["s3_key"]))
    except Exception as exc:
        report["db_msg"] = f"s3 read failed: {exc}"
        report["sim_notes"] = ["S3读取失败，未获取 sim_recorder/sim_predict 数据。"]
        report["carbon_diagnosis"] = ["S3读取失败，无法读取 AI加碳策略三表，请检查 S3 配置与对象路径。"]
        return report

    report["db_ok"] = True
    report["db_msg"] = f"s3 manifest loaded: {used_manifest_key}"

    task_rows = data_rows.get("carbon_opt_task", [])
    front_rows = data_rows.get("carbon_opt_front", [])
    sched_rows = data_rows.get("carbon_opt_schedule", [])
    rec_rows = data_rows.get("sim_recorder", [])
    pred_rows = data_rows.get("sim_predict", [])
    wq_rows = data_rows.get("water_quality", [])
    wqp_rows = data_rows.get("water_quality_pool", [])

    report["carbon"]["task_count"] = len(task_rows) if task_rows else "N/A"
    task = _pick_first(task_rows)
    if task:
        status = task.get("status", "N/A")
        remark = task.get("remark", "")
        report["carbon"]["latest_task"] = f"{status} ({remark})".strip()
        target = _try_float(task.get("snox_limit"))
        report["carbon"]["snox_target_num"] = target
        report["carbon"]["snox_target"] = fmt(target)

    front = _pick_first(front_rows)
    if front:
        t = front.get("exec_time", front.get("execute_time", "N/A"))
        mode = front.get("mode", "N/A")
        carbon = _try_float(front.get("carbon_2_cal"))
        snox = _try_float(front.get("snox_sim"))
        report["carbon"]["front_current"] = f"time={t}, mode={mode}, carbon={fmt(carbon)}, snox={fmt(snox)}"
        report["carbon"]["front_snox_num"] = snox
        report["carbon"]["front_snox"] = fmt(snox)

    vals = [_try_float(r.get("carbon_2_cal")) for r in sched_rows]
    vals = [x for x in vals if x is not None]
    if vals:
        report["carbon"]["schedule_next_60m"] = (
            f"points={len(vals)}, avg={fmt(sum(vals)/len(vals))}, min={fmt(min(vals))}, max={fmt(max(vals))}"
        )
        # Use recent ordered schedule points to estimate how frequently carbon setpoint changes.
        sorted_rows = sorted(
            [r for r in sched_rows if parse_dt(r.get("execute_time")) is not None],
            key=lambda x: parse_dt(x.get("execute_time")) or datetime.min,
        )
        recent_vals = [_try_float(r.get("carbon_2_cal")) for r in sorted_rows[-60:]]
        recent_vals = [x for x in recent_vals if x is not None]
        if recent_vals:
            ev, rt, sp = compute_adjustment_stats([x for x in recent_vals if x is not None])
            report["carbon"]["adjust_events"] = ev
            report["carbon"]["adjust_ratio"] = fmt(rt, 2)
            report["carbon"]["adjust_span"] = fmt(sp)

    target = report["carbon"]["snox_target_num"]
    current = report["carbon"]["front_snox_num"]
    if target is not None and current is not None:
        report["carbon"]["snox_gap"] = fmt(current - target)

    report["carbon_chain"] = {
        "step_1_task": "carbon_opt_task records one optimization/manual task",
        "step_2_schedule": "carbon_opt_schedule expands strategy into 10-minute execution points",
        "step_3_front": "carbon_opt_front upserts final effective points (history + forecast)",
        "step_4_dispatch": "set_carbon_opc reads front effective values for control dispatch",
        "current_target_snox": report["carbon"]["snox_target"],
        "current_front_snox": report["carbon"]["front_snox"],
        "current_gap": report["carbon"]["snox_gap"],
    }

    for rows, key, table in [
        (rec_rows, "recorder_means", "sim_recorder"),
        (pred_rows, "predict_means", "sim_predict"),
    ]:
        if not rows:
            report["sim_notes"].append(f"{table} empty in latest S3 run")
            continue
        cols = list(rows[0].keys())
        targets = [c for c in cols if ("snox" in c.lower() and "cstr" in c.lower()) or ("eff" in c.lower())]
        if not targets:
            report["sim_notes"].append(f"{table} no CSTR*_SNOx/EFF* columns matched")
            continue
        for c in targets:
            series = [_try_float(r.get(c)) for r in rows]
            series = [x for x in series if x is not None]
            if not series:
                continue
            avg_v = sum(series) / len(series)
            max_v = max(series)
            report[key][c] = avg_v
            if avg_v > 0 and max_v / avg_v >= 1.8:
                report["warnings"].append(classify_peak(table, c, avg_v, max_v, lang))

    wq = _pick_first(wq_rows)
    if wq:
        inlet = {}
        outlet = {}
        for k, v in wq.items():
            if not isinstance(k, str):
                continue
            num = _try_float(v)
            if num is None:
                continue
            kl = k.lower()
            if kl.startswith("inlet_"):
                inlet[k] = num
            elif kl.startswith("outlet_"):
                outlet[k] = num
        report["lab"]["inlet"] = inlet
        report["lab"]["outlet"] = outlet
        report["lab"]["insights"] = _compute_lab_insights(wq_rows, wq, lang)
        report["lab"]["plotly"] = _build_lab_plotly(wq_rows, max_days=10)
        wqid = _ci_get(wq, "water_quality_id", "id")
        if wqid is not None:
            day_map: dict[str, str] = {}
            for wr in wq_rows:
                wid = _ci_get(wr, "water_quality_id", "id")
                did = _as_day_id(_ci_get(wr, "local_day_id", "day_id", "date_id"))
                if wid is not None and did:
                    day_map[str(wid)] = did
            latest_day_id = _as_day_id(_ci_get(wq, "local_day_id", "day_id", "date_id"))
            lines, notes = _build_pool_lines_with_fallback(wqp_rows, wqid, day_map, latest_day_id)
            alerts = []
            for item in lines:
                key = item.get("key")
                mlss = item.get("mlss")
                mlvss = item.get("mlvss")
                if mlss is not None and (mlss < 500 or mlss > 8000):
                    alerts.append(f"line{key} mlss abnormal: {fmt(mlss)}")
                if mlvss is not None and (mlvss < 300 or mlvss > 7000):
                    alerts.append(f"line{key} mlvss abnormal: {fmt(mlvss)}")
                if mlss is not None and mlvss is not None and mlss > 0:
                    ratio = mlvss / mlss
                    if ratio < 0.4 or ratio > 0.9:
                        alerts.append(f"line{key} mlvss/mlss ratio unusual: {fmt(ratio, 2)}")
            missing_mlvss = [str(x.get("key")) for x in lines if x.get("mlvss") is None]
            if missing_mlvss:
                alerts.append(
                    "mlvss 缺失线路: "
                    + ",".join(missing_mlvss)
                    + "。可能原因：实验室该批次未回填 parm_mlvss 或同步字段缺失；建议检查 S3 源文件与 water_quality_pool.parm_mlvss。"
                )
            report["lab"]["pool_lines"] = lines
            report["lab"]["alerts"] = alerts
            if notes:
                report["lab"]["note"] = "; ".join(notes)
    else:
        report["lab"]["note"] = "water_quality empty in latest S3 run"

    report["carbon_diagnosis"] = build_carbon_diagnosis(report["carbon"], lang)
    report["history_baseline"] = _build_history_baseline(report)
    return report


def build_diagnostics(lang: str = "zh") -> dict[str, Any]:
    now = datetime.now()
    host = os.environ.get("REPORT_HOST", "127.0.0.1")
    port = int(os.environ.get("REPORT_PORT", "8003"))
    payload: dict[str, Any] = {
        "generated_at": now.isoformat(timespec="seconds"),
        "service": {
            "running": True,
            "pid": os.getpid(),
            "host": host,
            "port": port,
            "uptime_sec": max(0, int((now - PROCESS_START_TS).total_seconds())),
        },
        "log_tail": {"path": str(BASE_DIR / "report_server.out"), "lines": []},
        "s3_manifest": {},
    }

    log_path = Path(payload["log_tail"]["path"])
    max_lines = int(os.environ.get("REPORT_DIAG_LOG_LINES", "80"))
    try:
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            payload["log_tail"]["lines"] = lines[-max_lines:]
        else:
            payload["log_tail"]["lines"] = ["report_server.out not found"]
    except Exception as exc:
        payload["log_tail"]["lines"] = [f"read log failed: {exc}"]

    bucket = os.environ.get("REPORT_S3_BUCKET", "llmdata")
    prefix = os.environ.get("REPORT_S3_PREFIX", "digitaltwin_exports").strip("/")
    latest_key = os.environ.get("REPORT_S3_LATEST_KEY", f"{prefix}/latest/manifest.json")
    try:
        client = _s3_client()
        manifest, used_key = _load_manifest_with_success_fallback(client, bucket, prefix, latest_key)
        datasets = manifest.get("datasets", [])
        ds_summary: list[dict[str, Any]] = []
        if isinstance(datasets, list):
            for d in datasets:
                if not isinstance(d, dict):
                    continue
                ds_summary.append(
                    {
                        "name": d.get("name"),
                        "rows": d.get("rows"),
                        "bytes": d.get("bytes"),
                        "s3_key": d.get("s3_key"),
                    }
                )
        payload["s3_manifest"] = {
            "ok": True,
            "bucket": bucket,
            "used_manifest_key": used_key,
            "status": manifest.get("status", "UNKNOWN"),
            "generated_at": manifest.get("generated_at", ""),
            "dataset_count": len(ds_summary),
            "datasets": ds_summary,
            "errors": manifest.get("errors", []),
        }
    except Exception as exc:
        payload["s3_manifest"] = {
            "ok": False,
            "bucket": bucket,
            "latest_key": latest_key,
            "error": str(exc),
        }
    return payload


def build_report(report_date: str, lang: str) -> dict[str, Any]:
    source = os.environ.get("REPORT_DATA_SOURCE", "s3").strip().lower()
    if source == "mysql":
        return build_report_mysql(report_date, lang)

    report = _build_report_from_s3(report_date, lang)
    if report.get("db_ok"):
        return report

    if os.environ.get("REPORT_S3_FALLBACK_DB", "1") == "1":
        fallback = build_report_mysql(report_date, lang)
        fallback["sim_notes"] = report.get("sim_notes", []) + fallback.get("sim_notes", [])
        fallback["db_msg"] = f"{report.get('db_msg', '')}; fallback=mysql"
        return fallback
    return report


def build_carbon_diagnosis(carbon: dict[str, Any], lang: str) -> list[str]:
    out: list[str] = []
    adjust_events = to_num(carbon.get("adjust_events"))
    adjust_ratio = to_num(carbon.get("adjust_ratio"))
    adjust_span = to_num(carbon.get("adjust_span"))
    latest = str(carbon.get("latest_task", "N/A"))
    front = str(carbon.get("front_current", "N/A"))
    sched = str(carbon.get("schedule_next_60m", "N/A"))
    gap = to_num(carbon.get("snox_gap"))

    if adjust_events is not None and adjust_ratio is not None:
        if adjust_ratio >= 0.6:
            out.append(
                "加碳设定在当前窗口内调整较频繁（高变动率），更可能是负荷波动或控制参数偏紧，而非任务触发频次问题。"
                if lang == "zh" else
                "Carbon setpoint changes are frequent in the current window (high change ratio), likely due to load swings or tight control tuning rather than task trigger frequency."
            )
        elif adjust_ratio >= 0.25:
            out.append(
                "加碳设定存在中等频率调整，建议结合进水扰动与回流状态进一步确认是否需要收敛控制参数。"
                if lang == "zh" else
                "Carbon setpoint shows medium adjustment frequency; verify influent perturbations and recycle status before tightening/relaxing control."
            )
        else:
            out.append(
                "加碳设定整体较平稳，策略未出现明显频繁调整。"
                if lang == "zh" else
                "Carbon setpoint is relatively stable with no obvious frequent adjustments."
            )
    if adjust_span is not None and adjust_span > 0.8:
        out.append(
            "窗口内碳投加幅度较大，需关注工艺段负荷切换及预测输入稳定性。"
            if lang == "zh" else
            "Carbon dosing span is large in the window; check process load switching and forecast-input stability."
        )

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


def llm_required() -> bool:
    return os.environ.get("REPORT_LLM_REQUIRED", "1") == "1"


def llm_retry_times() -> int:
    try:
        return max(0, int(os.environ.get("REPORT_LLM_RETRY", "1")))
    except Exception:
        return 1


def llm_timeout_seconds() -> int:
    try:
        return max(15, int(os.environ.get("REPORT_LLM_TIMEOUT_SEC", "90")))
    except Exception:
        return 90


def llm_max_tokens() -> int:
    try:
        return max(256, int(os.environ.get("REPORT_LLM_MAX_TOKENS", "1200")))
    except Exception:
        return 1200


def _missing_facts(report: dict[str, Any]) -> list[str]:
    facts: list[str] = []
    if not report.get("recorder_means"):
        facts.append("sim_recorder 关键指标均值未取到。")
    if not report.get("predict_means"):
        facts.append("sim_predict 关键指标均值未取到。")
    for n in report.get("sim_notes", []):
        s = str(n).lower()
        if any(x in s for x in ["empty", "not found", "no cstr", "未获取", "未匹配"]):
            facts.append(str(n))
    for n in report.get("lab", {}).get("alerts", []):
        if "缺失" in str(n):
            facts.append(str(n))
    dedup: list[str] = []
    seen = set()
    for x in facts:
        if x not in seen:
            dedup.append(x)
            seen.add(x)
    return dedup


def report_has_missing(report: dict[str, Any]) -> bool:
    return len(_missing_facts(report)) > 0


def validate_llm_output(text: str, lang: str, require_missing_diagnosis: bool) -> tuple[bool, str]:
    t = (text or "").strip()
    if len(t) < 20:
        return False, "LLM output too short"
    if lang == "zh":
        bad_openers = [
            "好的，作为",
            "好的, 作为",
            "好的，",
            "当然，",
            "当然,",
            "下面我将",
            "根据您提供",
        ]
        if any(t.startswith(x) for x in bad_openers):
            return False, "LLM output has non-professional conversational opener"
    if lang == "zh":
        required_sections = ["总评", "当前运行状态", "预测简述", "预警原因与工艺诊断", "操作建议", "AI加碳策略"]
        for sec in required_sections:
            if sec not in t:
                return False, f"LLM output missing section: {sec}"
    if lang == "zh":
        useful_tokens = ["结论", "建议", "风险"]
    else:
        useful_tokens = ["conclusion", "recommend", "risk"]
    low = t.lower()
    for token in useful_tokens:
        if token.lower() not in low:
            return False, f"LLM output missing usefulness token: {token}"
    if not require_missing_diagnosis:
        # Even without missing data, still require actionable diagnosis style.
        if lang == "zh":
            if "AI加碳策略" not in t:
                return False, "LLM output missing AI加碳策略 section"
            if not any(x in t for x in ["增碳", "降碳", "保持"]):
                return False, "LLM output missing explicit carbon action (增碳/降碳/保持)"
            if not any(x in t for x in ["现状", "当前状态"]):
                return False, "LLM output missing carbon current-state analysis"
            if not any(x in t for x in ["未来", "趋势", "后续"]):
                return False, "LLM output missing carbon future-trend analysis"
        else:
            if "ai carbon" not in low:
                return False, "LLM output missing AI carbon section"
        return True, ""
    if lang == "zh":
        need = ["原因", "排查", "结果"]
    else:
        need = ["cause", "check", "result"]
    for token in need:
        if token.lower() not in low:
            return False, f"LLM output missing token: {token}"
    # Enforce richer AI carbon strategy, not plain data restatement.
    if lang == "zh":
        carbon_need = ["AI加碳策略", "建议"]
        action_need = ["增碳", "降碳", "保持"]
        for token in carbon_need:
            if token not in t:
                return False, f"LLM output missing token: {token}"
        if not any(x in t for x in action_need):
            return False, "LLM output missing explicit carbon action (增碳/降碳/保持)"
        if not any(x in t for x in ["现状", "当前状态"]):
            return False, "LLM output missing carbon current-state analysis"
        if not any(x in t for x in ["未来", "趋势", "后续"]):
            return False, "LLM output missing carbon future-trend analysis"
    else:
        if "ai carbon" not in low or "recommend" not in low:
            return False, "LLM output missing AI carbon recommendation section"
    return True, ""


def _llm_request(base: str, key: str, body: dict[str, Any], timeout_sec: int) -> str:
    req = urlrequest.Request(
        f"{base}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=timeout_sec) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


def _repair_llm_output(
    base: str,
    key: str,
    model: str,
    raw_text: str,
    reason: str,
    lang: str,
    timeout_sec: int,
) -> str:
    if lang == "zh":
        instruction = (
            "下面是一个不合格的报告文本，请按要求重写为工程可执行版本。"
            "必须包含：总评结论、AI加碳策略建议（明确增碳/降碳/保持）、风险、缺失值原因、排查步骤、排查后结果。"
            "禁止仅复述数据。"
        )
    else:
        instruction = (
            "Rewrite the following report into an actionable engineering report."
            " It must include conclusion, AI carbon action (increase/decrease/hold), risk, missing-value cause, checks, and post-check result."
            " Do not merely restate data."
        )
    body = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": llm_max_tokens(),
        "messages": [
            {"role": "system", "content": "You are a strict report improver. Output only the rewritten report."},
            {"role": "user", "content": f"{instruction}\n\n[FAILED_REASON]\n{reason}\n\n[RAW_REPORT]\n{raw_text}"},
        ],
    }
    return _llm_request(base, key, body, timeout_sec)


def _expand_llm_output(base: str, key: str, model: str, raw_text: str, lang: str, timeout_sec: int) -> str:
    if lang == "zh":
        instruction = (
            "请将下面的报告扩写为完整工程版，保持事实不变。"
            "必须包含并显式分段：总评、当前运行状态、预测简述、预警原因与工艺诊断、操作建议、AI加碳策略。"
            "对缺失值必须写：原因、排查步骤、排查后结果。"
            "AI加碳策略必须给出明确动作：增碳/降碳/保持。"
        )
    else:
        instruction = (
            "Expand the report into a complete engineering report while keeping facts unchanged."
            " Include explicit sections: summary, current operation, forecast, warning/diagnosis, actions, AI carbon strategy."
            " For missing values include cause/check/result and explicit carbon action."
        )
    body = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": llm_max_tokens(),
        "messages": [
            {"role": "system", "content": "You are a strict technical report writer. Output only the expanded report."},
            {"role": "user", "content": f"{instruction}\n\n[RAW_REPORT]\n{raw_text}"},
        ],
    }
    return _llm_request(base, key, body, timeout_sec)


def _translate_text_local(text: str, target_lang: str = "en") -> str:
    src = (text or "").strip()
    if not src or target_lang != "en":
        return src
    line_map = {
        "总评": "Summary",
        "当前运行状态": "Current Running Status",
        "预测简述": "Prediction Brief",
        "预警原因与工艺诊断": "Warning Causes & Process Diagnosis",
        "操作建议": "Actions",
        "AI加碳策略专项分析": "AI Carbon Strategy Special Analysis",
        "AI加碳策略": "AI Carbon Strategy",
    }
    token_map = [
        ("【重点】", "[Key] "),
        ("风险", "risk"),
        ("建议", "recommendation"),
        ("现状", "current status"),
        ("未来", "future"),
        ("趋势", "trend"),
        ("增碳", "increase carbon"),
        ("降碳", "decrease carbon"),
        ("保持", "hold"),
        ("预警", "warning"),
        ("工艺诊断", "process diagnosis"),
        ("原因", "cause"),
        ("排查", "check"),
        ("结果", "result"),
        ("缺失", "missing"),
    ]
    out: list[str] = []
    for raw in src.splitlines():
        line = raw.rstrip()
        key = line.strip().replace("*", "")
        if key in line_map:
            out.append(line_map[key])
            continue
        t = line
        for a, b in token_map:
            t = t.replace(a, b)
        out.append(t)
    return "\n".join(out)


def get_analysis_for_lang(
    report: dict[str, Any],
    lang: str,
    provider: str,
    mode: str = "fast",
    force_regen: bool = False,
) -> str:
    # Always generate one base analysis and reuse for language switching.
    report_for_cache = json.loads(json.dumps(report, ensure_ascii=False))
    if isinstance(report_for_cache, dict):
        report_for_cache.pop("generated_at", None)
    cache_basis = json.dumps({"report": report_for_cache, "provider": provider, "mode": mode}, ensure_ascii=False, sort_keys=True)
    h = hashlib.sha1(cache_basis.encode("utf-8")).hexdigest()
    zh_key = f"{h}:zh"
    en_key = f"{h}:en"
    stale_key_zh = f"{provider}:zh:{mode}"
    stale_key_en = f"{provider}:en:{mode}"

    backup_zh = ANALYSIS_CACHE.get(zh_key)
    backup_en = ANALYSIS_CACHE.get(en_key)
    if force_regen:
        ANALYSIS_CACHE.pop(zh_key, None)
        ANALYSIS_CACHE.pop(en_key, None)
    if zh_key not in ANALYSIS_CACHE:
        try:
            ANALYSIS_CACHE[zh_key] = llm_analysis(report, "zh", provider, mode)
            LAST_GOOD_ANALYSIS[stale_key_zh] = ANALYSIS_CACHE[zh_key]
            try:
                write_s3_memory_summary(report, ANALYSIS_CACHE[zh_key], "zh", provider, mode)
            except Exception:
                pass
        except Exception as exc:
            if provider.lower() == "glm":
                try:
                    fb = llm_analysis(report, "zh", "deepseek", mode)
                    note = "【重点】GLM 当前返回不稳定，已自动回退 DeepSeek 结果。\n"
                    ANALYSIS_CACHE[zh_key] = _sanitize_analysis_text(note + fb, "zh")
                    LAST_GOOD_ANALYSIS[stale_key_zh] = ANALYSIS_CACHE[zh_key]
                    return ANALYSIS_CACHE[zh_key]
                except Exception:
                    pass
            if backup_zh:
                ANALYSIS_CACHE[zh_key] = backup_zh
                LAST_GOOD_ANALYSIS[stale_key_zh] = backup_zh
                return ANALYSIS_CACHE[zh_key]
            if stale_key_zh in LAST_GOOD_ANALYSIS:
                ANALYSIS_CACHE[zh_key] = LAST_GOOD_ANALYSIS[stale_key_zh]
            else:
                raise exc

    if lang == "zh":
        return ANALYSIS_CACHE[zh_key]

    if en_key not in ANALYSIS_CACHE:
        try:
            ANALYSIS_CACHE[en_key] = _translate_text_local(ANALYSIS_CACHE[zh_key], "en")
            LAST_GOOD_ANALYSIS[stale_key_en] = ANALYSIS_CACHE[en_key]
        except Exception:
            if backup_en:
                ANALYSIS_CACHE[en_key] = backup_en
                LAST_GOOD_ANALYSIS[stale_key_en] = backup_en
                return ANALYSIS_CACHE[en_key]
            if stale_key_en in LAST_GOOD_ANALYSIS:
                ANALYSIS_CACHE[en_key] = LAST_GOOD_ANALYSIS[stale_key_en]
            else:
                raise
    return ANALYSIS_CACHE[en_key]


def llm_analysis(report: dict[str, Any], lang: str, provider: str, mode: str = "fast") -> str:
    enabled = os.environ.get("REPORT_LLM_ENABLED", "0") == "1"
    if not enabled:
        msg = "LLM 分析未开启（REPORT_LLM_ENABLED=1）" if lang == "zh" else "LLM analysis disabled (REPORT_LLM_ENABLED=1)."
        if llm_required():
            raise RuntimeError(msg)
        return msg
    profile = llm_profile(provider)
    key = profile["key"]
    if not key:
        msg = "未配置 REPORT_LLM_API_KEY。" if lang == "zh" else "REPORT_LLM_API_KEY is not configured."
        if llm_required():
            raise RuntimeError(msg)
        return msg
    base = profile["base"]
    model = profile["model"]
    rules = load_text(lang, "REPORT_REQUIREMENTS")
    context = load_text(lang, "REPORT_CONTEXT")
    extra_context = load_extra_context()
    prompt = (
        "你是污水处理厂数字孪生运行专家。请严格按规范输出报告分析，必须包含“AI加碳策略专项分析”小节。该小节必须显式包含三段：1) 现状判断 2) 未来1-3小时趋势判断 3) 可执行建议（含动作幅度、验证指标、风险）。此外，必须给出“历史对比”内容，至少覆盖：前1日或近3日趋势、inlet/outlet COD变化或去除率、并引用 [HISTORY_BASELINE] 或 [RAG_MEMORY] 中的事实。重点解释三表给出的策略建议和当前状态，不要展开讲三表生成流程。请结合 NOX 目标差距给出可执行建议。对 N/A/NaN 不允许只复述缺失，必须推理最可能原因、给验证证据、给替代判断路径和分级排查动作。若存在缺失值，必须单独输出：1) 原因假设 2) 排查步骤 3) 排查后结果（基于现有证据的最可能结论与风险）。若 [MISSING_FACTS] 为空，禁止声称“运行数据缺失/大面积缺失”。对紧急或高风险结论，请在句首添加“【重点】”。禁止出现“好的，作为……”这类口语开场；首行直接进入结论。"
        if lang == "zh"
        else "You are a wastewater digital twin operations expert. Include a dedicated 'AI Carbon Strategy' section with three explicit parts: current status, 1-3 hour outlook, and executable actions. Add an explicit historical comparison (yesterday or recent 3-day trend), including COD in/out trend or removal-rate evidence from [HISTORY_BASELINE]/[RAG_MEMORY]. Do not claim missing data unless it is listed in [MISSING_FACTS]."
    )
    base_body = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": llm_max_tokens(),
        "messages": [
            {"role": "system", "content": "Be concise, practical, professional, and diagnostic. Start directly with report sections (no conversational opener). When values are missing (N/A/NaN), infer causes and propose verifiable checks instead of merely reporting missingness."},
        ],
    }
    full_user = (
        prompt
        + "\n\n[REPORT_RULES]\n"
        + rules
        + "\n\n[PROCESS_CONTEXT]\n"
        + context
        + "\n\n[EXTRA_CONTEXT]\n"
        + extra_context
        + "\n\n[RAG_MEMORY]\n"
        + load_s3_memory_context(report, lang, provider, mode)
        + "\n\n[MISSING_FACTS]\n"
        + ("\n".join(_missing_facts(report)) if _missing_facts(report) else "none")
        + "\n\n[HISTORY_BASELINE]\n"
        + ("\n".join(report.get("history_baseline", [])) if isinstance(report.get("history_baseline", []), list) and report.get("history_baseline") else "none")
        + "\n\n[DATA_JSON]\n"
        + json.dumps(report, ensure_ascii=False)
    )
    compact_user = (
        prompt
        + "\n\n[REPORT_RULES]\n"
        + rules[:6000]
        + "\n\n[PROCESS_CONTEXT]\n"
        + context[:6000]
        + "\n\n[RAG_MEMORY]\n"
        + load_s3_memory_context(report, lang, provider, mode)[:2000]
        + "\n\n[MISSING_FACTS]\n"
        + ("\n".join(_missing_facts(report)) if _missing_facts(report) else "none")
        + "\n\n[HISTORY_BASELINE]\n"
        + ("\n".join(report.get("history_baseline", [])) if isinstance(report.get("history_baseline", []), list) and report.get("history_baseline") else "none")
        + "\n\n[DATA_JSON]\n"
        + json.dumps(report, ensure_ascii=False)
    )
    required_missing_diag = report_has_missing(report)
    fast_mode = (mode or "fast").lower() == "fast"
    attempts = 1 if fast_mode else (1 + llm_retry_times())
    last_err = ""
    best_effort_text = ""
    timeout_sec = min(llm_timeout_seconds(), 45) if fast_mode else llm_timeout_seconds()
    max_wall_sec = int(os.environ.get("REPORT_LLM_MAX_WALL_SEC", "120" if fast_mode else "240"))
    start_ts = datetime.now()
    for _ in range(attempts):
        if int((datetime.now() - start_ts).total_seconds()) > max_wall_sec:
            last_err = f"llm wall-time exceeded {max_wall_sec}s"
            break
        try:
            body = dict(base_body)
            body["max_tokens"] = min(llm_max_tokens(), 900) if fast_mode else llm_max_tokens()
            body["messages"] = list(base_body["messages"]) + [{"role": "user", "content": compact_user if fast_mode else full_user}]
            text = _llm_request(base, key, body, timeout_sec)
            if len(text.strip()) >= 40:
                best_effort_text = text
            # GLM may return concise first-pass output; force one expansion round when too short.
            if profile["provider"] == "glm" and len(text.strip()) < 450:
                try:
                    text = _expand_llm_output(base, key, model, text, lang, timeout_sec)
                    if len(text.strip()) >= 40:
                        best_effort_text = text
                except Exception:
                    pass
            ok, reason = validate_llm_output(text, lang, required_missing_diag)
            if ok:
                return _sanitize_analysis_text(text, lang)
            if fast_mode and len(text.strip()) >= 80:
                return _sanitize_analysis_text(text, lang)
            try:
                repaired = _repair_llm_output(base, key, model, text, reason, lang, timeout_sec)
                ok2, reason2 = validate_llm_output(repaired, lang, required_missing_diag)
                if ok2:
                    return _sanitize_analysis_text(repaired, lang)
                if fast_mode and len(repaired.strip()) >= 80:
                    return _sanitize_analysis_text(repaired, lang)
                last_err = reason2
            except Exception as rexc:
                last_err = f"{reason}; repair_failed={rexc}"
        except Exception:
            try:
                # Timeout/large-context fallback: retry once with compact context payload.
                body = dict(base_body)
                body["max_tokens"] = min(llm_max_tokens(), 900) if fast_mode else llm_max_tokens()
                body["messages"] = list(base_body["messages"]) + [{"role": "user", "content": compact_user}]
                text = _llm_request(base, key, body, timeout_sec)
                if len(text.strip()) >= 40:
                    best_effort_text = text
                if profile["provider"] == "glm" and len(text.strip()) < 450:
                    try:
                        text = _expand_llm_output(base, key, model, text, lang, timeout_sec)
                        if len(text.strip()) >= 40:
                            best_effort_text = text
                    except Exception:
                        pass
                ok, reason = validate_llm_output(text, lang, required_missing_diag)
                if ok:
                    return _sanitize_analysis_text(text, lang)
                if fast_mode and len(text.strip()) >= 80:
                    return _sanitize_analysis_text(text, lang)
                try:
                    repaired = _repair_llm_output(base, key, model, text, reason, lang, timeout_sec)
                    ok2, reason2 = validate_llm_output(repaired, lang, required_missing_diag)
                    if ok2:
                        return _sanitize_analysis_text(repaired, lang)
                    if fast_mode and len(repaired.strip()) >= 80:
                        return _sanitize_analysis_text(repaired, lang)
                    last_err = reason2
                except Exception as rexc:
                    last_err = f"{reason}; repair_failed={rexc}"
            except Exception as exc2:
                last_err = str(exc2)
    if profile["provider"] == "glm" and best_effort_text.strip():
        fallback_prefix = "（GLM返回精简版，建议点击“更新AI分析”重试）\n" if lang == "zh" else "(GLM returned a concise version; click 'Update AI Analysis' to retry.)\n"
        return _sanitize_analysis_text(fallback_prefix + best_effort_text, lang)
    msg = f"LLM 调用失败: {last_err}" if lang == "zh" else f"LLM call failed: {last_err}"
    if llm_required():
        raise RuntimeError(msg)
    return msg


def load_text(lang: str, stem: str) -> str:
    if lang == "en":
        p = BASE_DIR / f"{stem}.en.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
    p = BASE_DIR / f"{stem}.zh.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""


def read_docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            with zf.open("word/document.xml") as f:
                xml = f.read().decode("utf-8", errors="ignore")
        text = re.sub(r"<[^>]+>", " ", xml)
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception:
        return ""


def read_docx_bytes(data: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            with zf.open("word/document.xml") as f:
                xml = f.read().decode("utf-8", errors="ignore")
        text = re.sub(r"<[^>]+>", " ", xml)
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception:
        return ""


def read_any_text(path: Path, max_chars: int = 12000) -> str:
    if not path.exists():
        return ""
    if path.suffix.lower() == ".docx":
        return read_docx_text(path)[:max_chars]
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except Exception:
        return ""


def load_extra_context() -> str:
    configured = os.environ.get("REPORT_EXTRA_CONTEXT_PATHS", "").strip()
    if configured:
        paths = [Path(x.strip()) for x in configured.split(";") if x.strip()]
    else:
        paths = [
            BASE_DIR / "README.zh.md",
            BASE_DIR.parent.parent / "README.zh.md",
            DT_ROOT / "Data_structure_enriched.docx",
            DT_ROOT / "Data_structure.docx",
            DT_ROOT / "docs" / "references" / "data_structure.md",
        ]
    blocks: list[str] = []
    for p in paths:
        txt = read_any_text(p)
        if txt:
            blocks.append(f"[{p.name}]\n{txt}")
    s3_blocks = load_s3_docs_context()
    if s3_blocks:
        blocks.append(s3_blocks)
    return "\n\n".join(blocks)


def load_s3_docs_context() -> str:
    enabled = os.environ.get("REPORT_S3_DOCS_ENABLED", "1") == "1"
    if not enabled:
        return ""
    bucket = os.environ.get("REPORT_S3_BUCKET", "").strip()
    if not bucket:
        return ""
    prefix = os.environ.get("REPORT_S3_DOCS_PREFIX", "docs/").strip("/")
    max_files = int(os.environ.get("REPORT_S3_DOCS_MAX_FILES", "12"))
    max_chars = int(os.environ.get("REPORT_S3_DOCS_MAX_CHARS", "30000"))
    allow_suffix = (".md", ".txt", ".docx")

    try:
        client = _s3_client()
        token = None
        keys: list[str] = []
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": f"{prefix}/"}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            for obj in page.get("Contents", []):
                k = str(obj.get("Key", ""))
                if k.lower().endswith(allow_suffix):
                    keys.append(k)
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
    except Exception:
        return ""

    if not keys:
        return ""
    keys = sorted(keys)[:max_files]

    blocks: list[str] = []
    used = 0
    for key in keys:
        if used >= max_chars:
            break
        try:
            obj = client.get_object(Bucket=bucket, Key=key)
            raw = obj["Body"].read()
            if key.lower().endswith(".docx"):
                txt = read_docx_bytes(raw)
            else:
                txt = raw.decode("utf-8", errors="ignore")
            txt = re.sub(r"\s+", " ", txt).strip()
            if not txt:
                continue
            remain = max_chars - used
            txt = txt[:remain]
            used += len(txt)
            blocks.append(f"[S3:{key}]\n{txt}")
        except Exception:
            continue
    return "\n\n".join(blocks)


def _memory_enabled() -> bool:
    return os.environ.get("REPORT_MEMORY_ENABLED", "1") == "1"


def _memory_bucket_and_prefix() -> tuple[str, str]:
    bucket = os.environ.get("REPORT_MEMORY_BUCKET", os.environ.get("REPORT_S3_BUCKET", "llmdata")).strip() or "llmdata"
    prefix = os.environ.get("REPORT_MEMORY_PREFIX", "memory").strip("/").strip() or "memory"
    return bucket, prefix


def _utc_now_str() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def _parse_utc_compact(ts: str) -> datetime | None:
    s = (ts or "").strip()
    if not s:
        return None
    for f in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, f)
        except Exception:
            continue
    return None


def _days_ago(ts: str) -> int:
    dt = _parse_utc_compact(ts)
    if not dt:
        return 0
    return max(0, int((datetime.utcnow() - dt).total_seconds() // 86400))


def _memory_retention_days() -> tuple[int, int]:
    normal = int(os.environ.get("REPORT_MEMORY_RETENTION_DAYS", "10"))
    high = int(os.environ.get("REPORT_MEMORY_HIGH_RETENTION_DAYS", "20"))
    if high < normal:
        high = normal
    return max(1, normal), max(1, high)


def _collect_report_keywords(report: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for w in report.get("warnings", []):
        if not isinstance(w, dict):
            continue
        for k in ("table", "column", "reason", "diagnosis"):
            v = str(w.get(k, "")).strip().lower()
            if v:
                tokens.update(re.findall(r"[a-z0-9_]+", v))
    carbon = report.get("carbon", {})
    if isinstance(carbon, dict):
        for k in ("snox_gap", "snox_target", "front_snox", "latest_task"):
            v = str(carbon.get(k, "")).strip().lower()
            if v:
                tokens.update(re.findall(r"[a-z0-9_]+", v))
    lab = report.get("lab", {})
    if isinstance(lab, dict):
        for x in lab.get("alerts", []):
            if not isinstance(x, str):
                continue
            tokens.update(re.findall(r"[a-z0-9_]+", x.lower()))
    return {x for x in tokens if len(x) >= 3}


def _load_memory_index_or_recent_runs(client, bucket: str, prefix: str) -> list[dict[str, Any]]:
    index_key = os.environ.get("REPORT_MEMORY_INDEX_KEY", f"{prefix}/hot/index/latest.json")
    try:
        obj = _s3_get_json(client, bucket, index_key)
        items = obj.get("items", [])
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
    except Exception:
        pass

    # Fallback: scan recent run entries.
    max_files = int(os.environ.get("REPORT_MEMORY_FALLBACK_FILES", "40"))
    run_prefix = f"{prefix}/runs/"
    keys: list[str] = []
    token = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": run_prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            k = str(obj.get("Key", ""))
            if k.endswith(".json"):
                keys.append(k)
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    rows: list[dict[str, Any]] = []
    for k in sorted(keys, reverse=True)[:max_files]:
        try:
            rows.append(_s3_get_json(client, bucket, k))
        except Exception:
            continue
    return rows


def load_s3_memory_context(report: dict[str, Any], lang: str, provider: str, mode: str) -> str:
    if not _memory_enabled():
        return ""
    bucket, prefix = _memory_bucket_and_prefix()
    try:
        client = _s3_client()
        items = _load_memory_index_or_recent_runs(client, bucket, prefix)
    except Exception:
        return ""
    if not items:
        return ""
    keys = _collect_report_keywords(report)
    max_items = int(os.environ.get("REPORT_MEMORY_TOPK", "6"))
    max_chars = int(os.environ.get("REPORT_MEMORY_MAX_CHARS", "5000"))

    def score(it: dict[str, Any]) -> int:
        text = " ".join(
            str(it.get(k, "")) for k in ("title", "summary", "content", "tags", "keywords", "diagnosis", "actions")
        ).lower()
        if not text:
            return 0
        hit = 0
        for kw in keys:
            if kw in text:
                hit += 1
        if str(it.get("lang", "")).lower() in {lang.lower(), "", "multi"}:
            hit += 1
        if str(it.get("provider", "")).lower() in {provider.lower(), ""}:
            hit += 1
        if str(it.get("mode", "")).lower() in {mode.lower(), ""}:
            hit += 1
        return hit

    ranked = sorted(items, key=score, reverse=True)
    selected = [x for x in ranked if score(x) > 0][:max_items]
    if not selected:
        selected = ranked[: min(3, len(ranked))]

    blocks: list[str] = []
    used = 0
    for i, it in enumerate(selected, start=1):
        text = (
            f"[MEMORY_{i}] title={it.get('title','')}\n"
            f"summary={it.get('summary','')}\n"
            f"actions={it.get('actions','')}\n"
            f"diagnosis={it.get('diagnosis','')}\n"
            f"source={it.get('source','')}\n"
            f"time={it.get('created_at','')}"
        )
        remain = max_chars - used
        if remain <= 0:
            break
        text = text[:remain]
        used += len(text)
        blocks.append(text)
    return "\n\n".join(blocks)


def write_s3_memory_summary(report: dict[str, Any], analysis_text: str, lang: str, provider: str, mode: str) -> None:
    if not _memory_enabled():
        return
    bucket, prefix = _memory_bucket_and_prefix()
    client = _s3_client()
    now = _utc_now_str()
    digest = hashlib.sha1((analysis_text[:500] + str(report.get("date", ""))).encode("utf-8")).hexdigest()[:10]
    run_key = f"{prefix}/runs/{now}-{digest}.json"
    confidence = "high" if ("【重点】" in analysis_text or len(report.get("warnings", [])) >= 2) else "normal"
    rec = {
        "created_at": now,
        "report_date": report.get("date"),
        "lang": lang,
        "provider": provider,
        "mode": mode,
        "confidence": confidence,
        "title": "digitaltwin_report_summary",
        "summary": analysis_text[:1200],
        "diagnosis": " ; ".join(str(x) for x in report.get("carbon_diagnosis", [])[:3]),
        "actions": " ; ".join(str(x) for x in report.get("sim_notes", [])[:3]),
        "tags": [w.get("column") for w in report.get("warnings", []) if isinstance(w, dict)][:8],
        "source": "digitaltwin_report",
    }
    client.put_object(
        Bucket=bucket,
        Key=run_key,
        Body=json.dumps(rec, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )

    index_key = os.environ.get("REPORT_MEMORY_INDEX_KEY", f"{prefix}/hot/index/latest.json")
    index: dict[str, Any] = {"items": []}
    try:
        index = _s3_get_json(client, bucket, index_key)
        if not isinstance(index, dict):
            index = {"items": []}
    except Exception:
        pass
    items = index.get("items", [])
    if not isinstance(items, list):
        items = []
    items.insert(0, rec)
    keep = int(os.environ.get("REPORT_MEMORY_INDEX_KEEP", "300"))
    index["items"] = [x for x in items if isinstance(x, dict)][:keep]
    index["updated_at"] = now
    client.put_object(
        Bucket=bucket,
        Key=index_key,
        Body=json.dumps(index, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    _run_memory_daily_maintenance(client, bucket, prefix)


def _extract_group_tag(item: dict[str, Any]) -> str:
    tags = item.get("tags", [])
    if isinstance(tags, list):
        for t in tags:
            s = str(t or "").strip().lower()
            if s:
                return s
    text = " ".join(str(item.get(k, "")) for k in ("title", "summary", "diagnosis")).lower()
    for k in ("eff_xtss", "eff_nh3", "eff_tn", "cstr3_10_snox", "cstr3_4_snox", "snox"):
        if k in text:
            return k
    return "general"


def _run_memory_daily_maintenance(client, bucket: str, prefix: str) -> None:
    marker_key = f"{prefix}/maintenance/last_run.json"
    today = datetime.utcnow().strftime("%Y%m%d")
    try:
        marker = _s3_get_json(client, bucket, marker_key)
        if str(marker.get("date", "")) == today:
            return
    except Exception:
        pass

    normal_days, high_days = _memory_retention_days()
    run_prefix = f"{prefix}/runs/"
    token = None
    run_keys: list[str] = []
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": run_prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            k = str(obj.get("Key", ""))
            if k.endswith(".json"):
                run_keys.append(k)
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")

    kept_items: list[dict[str, Any]] = []
    moved = 0
    for k in sorted(run_keys, reverse=True):
        try:
            item = _s3_get_json(client, bucket, k)
        except Exception:
            continue
        created = str(item.get("created_at", ""))
        conf = str(item.get("confidence", "normal")).lower()
        age = _days_ago(created)
        max_days = high_days if conf == "high" else normal_days
        if age > max_days:
            archive_key = f"{prefix}/archive/{k.split('/')[-1]}"
            try:
                client.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": k}, Key=archive_key)
                client.delete_object(Bucket=bucket, Key=k)
                moved += 1
            except Exception:
                pass
            continue
        kept_items.append(item)

    # Summarize similar memories into hot layer.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for it in kept_items:
        g = _extract_group_tag(it)
        grouped.setdefault(g, []).append(it)
    hot_items: list[dict[str, Any]] = []
    for g, arr in grouped.items():
        arr_sorted = sorted(arr, key=lambda x: str(x.get("created_at", "")), reverse=True)
        top = arr_sorted[:5]
        merged_summary = " | ".join(str(x.get("summary", ""))[:180] for x in top if str(x.get("summary", "")).strip())
        hot_items.append(
            {
                "created_at": _utc_now_str(),
                "group": g,
                "title": f"hot_summary_{g}",
                "summary": merged_summary[:1200],
                "confidence": "high" if any(str(x.get("confidence", "")) == "high" for x in top) else "normal",
                "source_count": len(arr),
                "source": "memory_compaction",
                "tags": [g],
            }
        )

    keep = int(os.environ.get("REPORT_MEMORY_INDEX_KEEP", "300"))
    hot_items = sorted(hot_items, key=lambda x: str(x.get("created_at", "")), reverse=True)[:keep]
    hot_index_key = os.environ.get("REPORT_MEMORY_INDEX_KEY", f"{prefix}/hot/index/latest.json")
    hot_index = {"updated_at": _utc_now_str(), "items": hot_items}
    client.put_object(
        Bucket=bucket,
        Key=hot_index_key,
        Body=json.dumps(hot_index, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    # Keep a daily snapshot for audit.
    daily_hot_key = f"{prefix}/hot/daily/{today}.json"
    client.put_object(
        Bucket=bucket,
        Key=daily_hot_key,
        Body=json.dumps(hot_index, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    client.put_object(
        Bucket=bucket,
        Key=marker_key,
        Body=json.dumps({"date": today, "moved_to_archive": moved, "kept": len(kept_items), "hot": len(hot_items)}).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )


def list_s3_memory_state(limit: int = 200) -> dict[str, Any]:
    bucket, prefix = _memory_bucket_and_prefix()
    client = _s3_client()
    index_key = os.environ.get("REPORT_MEMORY_INDEX_KEY", f"{prefix}/index/latest.json")
    state: dict[str, Any] = {
        "bucket": bucket,
        "prefix": prefix,
        "index_key": index_key,
        "index_updated_at": "",
        "index_items": 0,
        "runs": [],
    }
    try:
        index = _s3_get_json(client, bucket, index_key)
        if isinstance(index, dict):
            items = index.get("items", [])
            state["index_updated_at"] = str(index.get("updated_at", ""))
            state["index_items"] = len(items) if isinstance(items, list) else 0
    except Exception:
        pass

    run_prefix = f"{prefix}/runs/"
    token = None
    rows: list[dict[str, Any]] = []
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": run_prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            rows.append(
                {
                    "key": str(obj.get("Key", "")),
                    "size": int(obj.get("Size", 0)),
                    "last_modified": str(obj.get("LastModified", "")),
                }
            )
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    rows = sorted(rows, key=lambda x: x.get("key", ""), reverse=True)[: max(1, int(limit))]
    state["runs"] = rows
    state["run_count"] = len(rows)
    return state


def delete_s3_memory_key(key: str) -> dict[str, Any]:
    bucket, prefix = _memory_bucket_and_prefix()
    k = (key or "").strip()
    if not k or not k.startswith(f"{prefix}/"):
        raise ValueError("invalid key")
    client = _s3_client()
    client.delete_object(Bucket=bucket, Key=k)
    return {"ok": True, "deleted_key": k, "bucket": bucket}


def get_s3_memory_key(key: str) -> dict[str, Any]:
    bucket, prefix = _memory_bucket_and_prefix()
    k = (key or "").strip()
    if not k or not k.startswith(f"{prefix}/"):
        raise ValueError("invalid key")
    client = _s3_client()
    return _s3_get_json(client, bucket, k)


def _extract_carbon_analysis_section(analysis_text: str) -> str:
    text = (analysis_text or "").strip()
    if not text:
        return ""
    # Prefer markdown heading section for carbon strategy.
    patterns = [
        r"(?:^|\n)\s*#{1,6}\s*\*{0,2}\s*6[\.\)]?\s*AI加碳策略专项分析.*?(?=\n\s*#{1,6}\s*|\Z)",
        r"(?:^|\n)\s*#{1,6}\s*\*{0,2}\s*AI加碳策略专项分析.*?(?=\n\s*#{1,6}\s*|\Z)",
        r"(?:^|\n)\s*#{1,6}\s*\*{0,2}\s*AI加碳策略.*?(?=\n\s*#{1,6}\s*|\Z)",
        r"(?:^|\n)\s*#{1,6}\s*\*{0,2}\s*AI Carbon Strategy.*?(?=\n\s*#{1,6}\s*|\Z)",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.I | re.S)
        if m:
            return m.group(0).strip()
    # Fallback for plain-text outputs without markdown headings.
    lines = [ln.rstrip() for ln in text.splitlines()]
    start = -1
    for i, ln in enumerate(lines):
        s = ln.strip()
        if re.search(r"(AI加碳策略专项分析|AI加碳策略|AI Carbon Strategy)", s, flags=re.I):
            start = i
            break
    if start >= 0:
        stop_markers = (
            r"^(当前运行状态|预测简述|预警原因与工艺诊断|操作建议|结论|智慧工艺诊断|Lab Running Overview|Warning Causes|Actionable Suggestions)\b"
        )
        out = []
        for j in range(start, len(lines)):
            s = lines[j].strip()
            if j > start and re.match(stop_markers, s, flags=re.I):
                break
            out.append(lines[j])
        section = "\n".join(x for x in out if x.strip()).strip()
        if section:
            return section
    # Last-resort: return the first substantial chunk so panel is never empty.
    short = "\n".join([x for x in lines[:14] if x.strip()]).strip()
    return short


def _sanitize_analysis_text(text: str, lang: str) -> str:
    t = (text or "").strip()
    if not t:
        return t
    if lang == "zh":
        # Remove conversational lead-in before the first real section heading.
        first_sec = re.search(r"(总评|当前运行状态|预测简述|预警原因与工艺诊断|操作建议|AI加碳策略)", t)
        if first_sec and first_sec.start() > 0:
            t = t[first_sec.start():].lstrip()
        bad_prefix = re.compile(r"^(好的[，,]?\s*|当然[，,]?\s*|下面我将[，,]?\s*|根据您提供[^。]*[。:：]\s*)+", re.I)
        t = bad_prefix.sub("", t).lstrip()
    return t


def _analysis_to_html(text: str, lang: str) -> str:
    t = _sanitize_analysis_text(text or "", lang)
    if not t:
        return ""
    def _inline_md(s: str) -> str:
        x = html.escape(s)
        x = re.sub(r"`([^`]+)`", r"<code>\1</code>", x)
        x = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", x)
        x = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", x)
        x = x.replace("【重点】", '<span class="ai-tag">重点</span> ')
        return x

    out: list[str] = ['<div class="ai-md">']
    in_ul = False
    in_ol = False

    def _close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    heading_words = ("当前运行状态", "预测简述", "预警原因与工艺诊断", "操作建议", "AI加碳策略", "风险", "结论")
    for raw in t.splitlines():
        line = raw.rstrip()
        if not line.strip():
            _close_lists()
            continue
        s = line.strip()
        norm = s.replace("*", "").strip()
        line_no_hash = re.sub(r"^#+\s*", "", norm).strip()

        if line_no_hash.startswith("总评"):
            m = re.match(r"^总评\s*[:：]?\s*(.*)$", line_no_hash)
            if m and m.group(1).strip():
                _close_lists()
                tail = _inline_md(m.group(1).strip())
                cls = "ai-line ai-urgent" if "ai-tag" in tail else "ai-line"
                out.append(f'<p class="{cls}">{tail}</p>')
            continue

        m_h = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m_h:
            _close_lists()
            level = min(max(len(m_h.group(1)), 2), 4)
            body = _inline_md(m_h.group(2).strip())
            out.append(f'<h{level} class="ai-heading">{body}</h{level}>')
            continue

        is_named_heading = line_no_hash in heading_words or bool(
            re.match(r"^(当前运行状态|预测简述|预警原因与工艺诊断|操作建议|AI加碳策略|风险|结论)\s*[:：]?$", line_no_hash)
        )
        if is_named_heading:
            _close_lists()
            out.append(f'<h4 class="ai-heading">{_inline_md(line_no_hash)}</h4>')
            continue

        m_ol = re.match(r"^\s*(\d+)\.\s+(.*)$", line)
        if m_ol:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append('<ol class="ai-list ai-ol">')
                in_ol = True
            body = _inline_md(m_ol.group(2).strip())
            li_cls = ' class="ai-urgent"' if "ai-tag" in body else ""
            out.append(f"<li{li_cls}>{body}</li>")
            continue

        m_ul = re.match(r"^\s*[-*•]\s+(.*)$", line)
        if m_ul:
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append('<ul class="ai-list ai-ul">')
                in_ul = True
            body = _inline_md(m_ul.group(1).strip())
            li_cls = ' class="ai-urgent"' if "ai-tag" in body else ""
            out.append(f"<li{li_cls}>{body}</li>")
            continue

        _close_lists()
        body = _inline_md(s)
        p_cls = "ai-line ai-urgent" if "ai-tag" in body else "ai-line"
        out.append(f'<p class="{p_cls}">{body}</p>')

    _close_lists()
    out.append("</div>")
    return "".join(out)


def render(report: dict[str, Any], lang: str, analysis_text: str, mode: str = "fast") -> str:
    zh = lang != "en"
    title = "数字孪生生产报告" if zh else "Digital Twin Production Report"
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
    analysis_text = _sanitize_analysis_text(analysis_text, lang)
    carbon_analysis = _extract_carbon_analysis_section(analysis_text)
    carbon_analysis_html = _analysis_to_html(carbon_analysis or "", lang)
    analysis = _analysis_to_html(analysis_text, lang)
    model_line = f"{profile['model']} @ {profile['base']}"
    zh_url = f"/report?date={report['date']}&lang=zh&provider={provider}&mode={mode}"
    en_url = f"/report?date={report['date']}&lang=en&provider={provider}&mode={mode}"
    ds_url = f"/report?date={report['date']}&lang={lang}&provider=deepseek&mode={mode}"
    glm_url = f"/report?date={report['date']}&lang={lang}&provider=glm&mode={mode}"
    refresh_url = f"/report?date={report['date']}&lang={lang}&provider={provider}&mode={mode}&refresh=1"
    sensor_url = "/sensor?embed=1"
    lab = report.get("lab", {})
    inlet = lab.get("inlet", {})
    outlet = lab.get("outlet", {})
    pool_lines = lab.get("pool_lines", [])
    lab_alerts = lab.get("alerts", [])
    lab_insights = lab.get("insights", [])
    lab_note = lab.get("note", "")
    lab_plot = lab.get("plotly", {}) if isinstance(lab.get("plotly", {}), dict) else {}
    inlet_text = "; ".join(f"{k}={fmt(v)}" for k, v in sorted(inlet.items())[:12]) or "N/A"
    outlet_text = "; ".join(f"{k}={fmt(v)}" for k, v in sorted(outlet.items())[:12]) or "N/A"
    pool_text = "; ".join(f"line{p.get('key')}: mlss={fmt(p.get('mlss'))}, mlvss={fmt(p.get('mlvss'))}" for p in pool_lines[:8]) or "N/A"
    lab_insight_text = "".join(f"<li>{html.escape(str(x))}</li>" for x in lab_insights)
    hist_base = report.get("history_baseline", []) if isinstance(report.get("history_baseline", []), list) else []
    hist_base_html = "".join(f"<li>{html.escape(str(x))}</li>" for x in hist_base)
    lab_note_title = "补充说明" if zh else "Notes"
    lab_note_text = (
        "MLSS abnormal 当值 < 500 或 > 8000；MLVSS abnormal 当值 < 300 或 > 7000；MLVSS/MLSS ratio unusual 当比值 < 0.40 或 > 0.90。"
        if zh
        else "MLSS abnormal if value < 500 or > 8000; MLVSS abnormal if value < 300 or > 7000; MLVSS/MLSS ratio unusual if ratio < 0.40 or > 0.90."
    )
    lab_alert_text = ""
    for x in lab_alerts:
        msg = str(x)
        needs_tip = ("abnormal" in msg.lower()) or ("unusual" in msg.lower())
        if needs_tip:
            lab_alert_text += (
                f"<li>{html.escape(msg)}"
                f"<span class=\"info-wrap\" style=\"margin-left:6px\">"
                f"<button type=\"button\" class=\"info-btn mini-info\">i</button>"
                f"<div class=\"info-pop\"><b>{lab_note_title}</b><br/>{html.escape(lab_note_text)}</div>"
                f"</span></li>"
            )
        else:
            lab_alert_text += f"<li>{html.escape(msg)}</li>"
    if not lab_alert_text:
        lab_alert_text = "<li>无明显异常</li>" if zh else "<li>No obvious abnormality.</li>"
    if lab_note:
        lab_alert_text += f"<li>{html.escape(lab_note)}</li>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>{title}</title>
<style>
body{{font-family:"IBM Plex Sans","PingFang SC","Microsoft YaHei",sans-serif;margin:0;color:#1f2937;background:
radial-gradient(circle at 15% 20%, rgba(55,117,182,.18), transparent 35%),
radial-gradient(circle at 80% 10%, rgba(42,154,212,.12), transparent 32%),
linear-gradient(rgba(9,17,27,.82), rgba(9,17,27,.82)),
url('/static/digitaltwin.png') center/cover fixed no-repeat;}}
.page{{max-width:min(1680px,96vw);margin:0 auto;padding:18px}}
.topbar{{position:relative;overflow:hidden;display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;background:linear-gradient(135deg, rgba(8,24,44,.86), rgba(15,40,68,.78));backdrop-filter:blur(8px);border:1px solid rgba(126,179,232,.5);border-radius:14px;padding:14px 16px;margin-bottom:12px;color:#e6edf5;box-shadow:0 0 0 1px rgba(68,129,192,.22) inset, 0 12px 30px rgba(0,0,0,.22), 0 0 24px rgba(76,144,210,.18)}}
.topbar::before{{content:"";position:absolute;inset:0;background:url('/static/plantwin_watermark.svg') center/92% no-repeat;opacity:.2;pointer-events:none}}
.topbar > *{{position:relative;z-index:1}}
.title{{font-size:22px;font-weight:700;letter-spacing:.2px}}
.meta{{font-size:12px;color:#b7c8da}}
.actions{{display:flex;gap:8px;flex-wrap:wrap}}
.lang{{display:inline-block;padding:5px 12px;border:1px solid #77a8df;border-radius:999px;text-decoration:none;color:#d9ecff;background:rgba(24,58,91,.45);box-shadow:0 0 12px rgba(80,147,214,.2)}}
.tabs{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}}
.tab-btn{{padding:8px 14px;border:1px solid #9dc4e8;border-radius:10px;background:linear-gradient(180deg, rgba(244,250,255,.95), rgba(228,241,255,.95));color:#0f4c81;cursor:pointer;font-weight:600}}
.tab-btn.active{{background:linear-gradient(180deg,#d9efff,#bfe0ff);border-color:#58a0db;box-shadow:0 0 0 1px rgba(68,129,192,.25) inset, 0 6px 14px rgba(68,129,192,.22)}}
.panel{{display:none}}
.panel.active{{display:block}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:10px}}
.overview-grid{{display:grid;grid-template-columns:1.2fr 1fr;gap:10px}}
.diag-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}
.card{{background:linear-gradient(180deg, rgba(248,251,255,.96), rgba(241,248,255,.94));border:1px solid #b8d0e8;border-radius:12px;padding:14px;box-shadow:0 10px 28px rgba(0,0,0,.16), 0 0 0 1px rgba(142,188,230,.2) inset;font-size:14px;line-height:1.62}}
h2{{margin:0 0 8px 0;color:#0f4c81;font-size:19px}}
.muted{{color:#496381;font-size:13px}} ul{{margin:0 0 0 18px;padding:0}}
.ai-summary-box{{background:
linear-gradient(180deg, rgba(228,241,255,.74), rgba(216,235,255,.62));
border:1px solid #9cc6ee;border-radius:10px;padding:10px 12px;position:relative}}
.ai-summary-box::before{{content:"";position:absolute;inset:0;background:repeating-linear-gradient(0deg, transparent, transparent 21px, rgba(66,120,173,.06) 22px);pointer-events:none;border-radius:10px}}
.plot-box{{height:290px}}
.ai-md{{position:relative;z-index:1}}
.ai-line{{line-height:1.68;font-size:14px;margin:0 0 8px 0;color:#1f2937}}
.ai-heading{{font-size:18px;font-weight:700;color:#0f4c81;margin:12px 0 8px 0;padding-top:4px;border-top:1px solid rgba(112,157,201,.35)}}
.ai-list{{margin:0 0 10px 22px;padding:0}}
.ai-list li{{margin:0 0 8px 0;line-height:1.7}}
.ai-md strong{{font-weight:700}}
.ai-md em{{font-style:italic}}
.ai-md code{{background:#e8f1fb;border:1px solid #c7dbef;padding:1px 4px;border-radius:5px;font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;font-size:12px}}
.ai-urgent{{color:#ff9f43;font-weight:700}}
.ai-tag{{display:inline-block;font-size:12px;padding:1px 6px;border-radius:999px;background:#ff9f43;color:#0e233b;margin-right:6px}}
.footer{{margin-top:14px;text-align:center;color:#c5d6ea;font-size:12px}}
.memory-link{{display:inline-block;padding:6px 12px;border:1px solid #7fb0e0;border-radius:999px;text-decoration:none;color:#d8ecff;background:rgba(23,57,90,.45);margin-bottom:8px}}
.sensor-frame{{width:100%;height:760px;border:1px solid #2b4f7b;border-radius:12px;background:#0d1827}}
.switch-mask{{position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:rgba(4,10,18,.48);z-index:9999}}
.switch-box{{background:#0f2137;border:1px solid #3c6796;border-radius:12px;padding:14px 18px;color:#d8ecff;box-shadow:0 10px 30px rgba(0,0,0,.25)}}
.info-wrap{{position:relative;display:inline-block;margin-left:8px}}
.info-btn{{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;border:1px solid #7aa8d6;color:#0f4c81;background:#e8f2ff;cursor:pointer;font-size:12px;font-weight:700;line-height:1}}
.info-pop{{display:none;position:absolute;left:0;top:24px;z-index:50;min-width:340px;max-width:520px;background:#f7fbff;border:1px solid #b8d0e8;border-radius:10px;padding:10px 12px;box-shadow:0 8px 24px rgba(0,0,0,.18);color:#1f2937;font-size:13px;line-height:1.5}}
.info-wrap.open .info-pop{{display:block}}
@media (max-width: 980px){{ .diag-grid{{grid-template-columns:1fr}} .overview-grid{{grid-template-columns:1fr}} }}
</style></head><body>
<div class="page">
<div class="topbar">
  <div>
    <div class="title">{title}</div>
    <div class="meta">date={report['date']} | generated={report['generated_at']}</div>
    <div class="meta">db={report['db_ok']} ({html.escape(report['db_msg'])})</div>
    <div class="meta">provider={provider} | model={html.escape(model_line)}</div>
  </div>
  <div class="actions">
    <a class="lang" href="{zh_url}">中文</a><a class="lang" href="{en_url}">English</a>
    <a class="lang provider-switch" data-provider="deepseek" href="{ds_url}">DeepSeek</a><a class="lang provider-switch" data-provider="glm" href="{glm_url}">GLM</a>
    <a id="forceRefreshBtn" class="lang" href="{refresh_url}">{'更新AI分析' if zh else 'Update AI Analysis'}</a>
  </div>
</div>
<div class="tabs">
  <button class="tab-btn active" data-tab="tab-ai-overview">{'AI智慧分析' if zh else 'AI Insight'}</button>
  <button class="tab-btn" data-tab="tab-carbon">{'AI加碳策略' if zh else 'AI Carbon Strategy'}</button>
  <button class="tab-btn" data-tab="tab-diagnosis">{'智慧工艺诊断' if zh else 'Smart Process Diagnosis'}</button>
  <button class="tab-btn" data-tab="tab-sensor">{'Sensor Validate' if zh else 'Sensor Validate'}</button>
</div>
<div id="tab-ai-overview" class="panel active">
<div class="overview-grid">
<div class="card"><h2>{'运行概况（实验室）' if zh else 'Lab Running Overview'}</h2>
<div>inlet_*: {html.escape(inlet_text)}</div>
<div>outlet_*: {html.escape(outlet_text)}</div>
<div>pool(mlss/mlvss): {html.escape(pool_text)}</div>
<div class="muted" style="margin-top:6px">{'工艺见解' if zh else 'Process Insights'}</div>
<ul>{lab_insight_text or ('<li>暂无</li>' if zh else '<li>N/A</li>')}</ul>
<ul>{lab_alert_text}</ul>
<div class="muted" style="margin-top:8px">{'实验室趋势图（进水）' if zh else 'Lab Trend (Inlet)'}</div>
<div id="labInletPlot" class="plot-box"></div>
<div class="muted" style="margin-top:8px">{'实验室趋势图（出水）' if zh else 'Lab Trend (Outlet)'}</div>
<div id="labOutletPlot" class="plot-box"></div>
<div class="muted" style="margin-top:8px">{'历史对比基线' if zh else 'Historical Baseline'}</div>
<ul>{hist_base_html or ('<li>暂无历史对比基线</li>' if zh else '<li>No baseline</li>')}</ul>
</div>
<div class="card"><h2>{'AI智慧分析综述' if zh else 'AI Insight Summary'}</h2><div class="ai-summary-box">{analysis}</div></div>
</div>
</div>

<div id="tab-carbon" class="panel">
<div class="grid">
<div class="card"><h2>{'AI加碳策略' if zh else 'AI Carbon Strategy'}</h2>
<div>latest_task: {html.escape(str(report['carbon']['latest_task']))}</div>
<div>snox_target: {html.escape(str(report['carbon']['snox_target']))}</div>
<div>snox_current(front): {html.escape(str(report['carbon']['front_snox']))}</div>
<div>snox_gap(current-target): {html.escape(str(report['carbon']['snox_gap']))}</div>
<div>front_current: {html.escape(str(report['carbon']['front_current']))}</div>
<div>schedule_next_60m: {html.escape(str(report['carbon']['schedule_next_60m']))}</div>
<div>adjust_events: {html.escape(str(report['carbon'].get('adjust_events', 'N/A')))} | adjust_ratio: {html.escape(str(report['carbon'].get('adjust_ratio', 'N/A')))} | adjust_span: {html.escape(str(report['carbon'].get('adjust_span', 'N/A')))}</div>
<ul>{carbon_diag}</ul>
<div class="muted" style="margin-top:10px"><b>{'AI加碳策略专项分析（LLM）' if zh else 'AI Carbon Strategy Analysis (LLM)'}</b></div>
<div style="white-space:normal;line-height:1.6">{carbon_analysis_html if carbon_analysis_html else ('未提取到AI加碳专项分析，请检查LLM输出分段。' if zh else 'AI carbon strategy section not found in LLM output.')}</div>
</div>
</div>
</div>

<div id="tab-diagnosis" class="panel">
<div class="diag-grid">
<div class="card"><h2>{'运行态 sim_recorder 均值' if zh else 'sim_recorder means'}</h2><div>{html.escape(rec)}</div></div>
<div class="card"><h2>{'预测态 sim_predict 均值' if zh else 'sim_predict means'}</h2><div>{html.escape(pre)}</div></div>
<div class="card"><h2>{'数据处理说明' if zh else 'Data Processing Notes'}</h2><ul>{sim_note_html or ('<li>无</li>' if zh else '<li>None</li>')}</ul></div>
<div class="card"><h2>{'预警原因与工艺诊断' if zh else 'Warning Causes & Process Diagnosis'}</h2><ul>{warning_html}</ul></div>
</div>
</div>

<div id="tab-sensor" class="panel">
<div class="grid">
<div class="card"><h2>Sensor Validate</h2>
<div class="muted">{'Dash版传感器校验视图（含时序+漂移gauge）。' if zh else 'Dash-based sensor validation view (timeseries + drift gauge).'}</div>
<iframe class="sensor-frame" src="{sensor_url}" title="sensor-validate-dash"></iframe>
</div>
</div>
</div>
</div>
<div class="footer">
  <a class="memory-link" href="/memory">Memory Log</a>
  <div>Powered by Transend Technology</div>
</div>
<div id="switchMask" class="switch-mask"><div class="switch-box" id="switchMsg">正在切换模型并重新生成分析...</div></div>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<script>
const tabButtons = document.querySelectorAll('.tab-btn');
const panels = document.querySelectorAll('.panel');
tabButtons.forEach((btn) => {{
  btn.addEventListener('click', () => {{
    tabButtons.forEach((b) => b.classList.remove('active'));
    panels.forEach((p) => p.classList.remove('active'));
    btn.classList.add('active');
    const id = btn.getAttribute('data-tab');
    const panel = document.getElementById(id);
    if (panel) panel.classList.add('active');
  }});
}});

const currentProvider = {json.dumps(str(provider).lower())};
const zhMode = {json.dumps(bool(zh))};
const mask = document.getElementById('switchMask');
const msg = document.getElementById('switchMsg');
document.querySelectorAll('.provider-switch').forEach((a) => {{
  a.addEventListener('click', (ev) => {{
    const target = String(a.getAttribute('data-provider') || '').toLowerCase();
    if (!target || target === currentProvider) return;
    const text = zhMode
      ? `将从 ${{currentProvider.toUpperCase()}} 切换到 ${{target.toUpperCase()}}，将重新调用 LLM 并需要等待，是否继续？`
      : `Switch from ${{currentProvider.toUpperCase()}} to ${{target.toUpperCase()}}? This will regenerate analysis and may take longer.`;
    if (!window.confirm(text)) {{
      ev.preventDefault();
      return;
    }}
    if (mask && msg) {{
      msg.textContent = zhMode ? `正在切换到 ${{target.toUpperCase()}} 并生成报告...` : `Switching to ${{target.toUpperCase()}} and regenerating report...`;
      mask.style.display = 'flex';
    }}
  }});
}});
const forceBtn = document.getElementById('forceRefreshBtn');
if (forceBtn) {{
  forceBtn.addEventListener('click', (ev) => {{
    const ok = window.confirm(zhMode ? '将更新AI分析（忽略缓存并重新调用 LLM），是否继续？' : 'Update AI analysis now? This ignores cache and calls LLM again.');
    if (!ok) {{
      ev.preventDefault();
      return;
    }}
    if (mask && msg) {{
      msg.textContent = zhMode ? '正在更新AI分析并生成报告...' : 'Updating AI analysis and regenerating report...';
      mask.style.display = 'flex';
    }}
  }});
}}

const labData = {json.dumps(lab_plot, ensure_ascii=False)};
function makeLabPlot(divId, side){{
  if (!window.Plotly) return;
  const x = Array.isArray(labData.x) ? labData.x : [];
  const obj = (side === 'inlet' ? labData.inlet : labData.outlet) || {{}};
  if (!x.length || !obj || Object.keys(obj).length === 0) {{
    const el = document.getElementById(divId);
    if (el) el.innerHTML = '<div class="muted">No data</div>';
    return;
  }}
  const colors = {{
    cod:'#60a5fa', nh3n:'#f59e0b', tn:'#10b981', tp:'#ef4444', ss:'#a78bfa', ph:'#14b8a6'
  }};
  const traces = Object.keys(obj).map((k) => ({{
    x, y: obj[k], mode:'lines+markers', name: (side + '_' + k).toUpperCase(),
    line: {{width:2,color:colors[k]||'#64748b'}}, marker: {{size:5}}
  }}));
  Plotly.newPlot(divId, traces, {{
    margin: {{l:45,r:20,t:10,b:40}},
    paper_bgcolor:'rgba(0,0,0,0)',
    plot_bgcolor:'rgba(255,255,255,0.92)',
    xaxis: {{showgrid:false}},
    yaxis: {{gridcolor:'#dbe7f4'}},
    legend: {{orientation:'h', y:-0.25}}
  }}, {{responsive:true, displaylogo:false}});
}}
makeLabPlot('labInletPlot', 'inlet');
makeLabPlot('labOutletPlot', 'outlet');

document.querySelectorAll('.mini-info').forEach((btn) => {{
  btn.addEventListener('click', (e) => {{
    e.stopPropagation();
    const wrap = btn.closest('.info-wrap');
    if (!wrap) return;
    document.querySelectorAll('.info-wrap.open').forEach((w) => {{
      if (w !== wrap) w.classList.remove('open');
    }});
    wrap.classList.toggle('open');
  }});
}});
document.addEventListener('click', () => {{
  document.querySelectorAll('.info-wrap.open').forEach((w) => w.classList.remove('open'));
}});
</script>
</body></html>"""


def render_loading_page(report_date: str, lang: str, provider: str, mode: str = "fast") -> str:
    m = load_ui_messages(lang)
    title = str(m.get("loading_title", "报告生成中" if lang != "en" else "Generating Report"))
    steps = m.get("loading_steps") if isinstance(m.get("loading_steps"), list) else []
    steps = [str(x) for x in steps if str(x).strip()]
    if not steps:
        steps = ["正在读取数据...", "正在连接LLM...", "正在生成报告..."] if lang != "en" else ["Reading data...", "Connecting LLM...", "Generating report..."]
    hint_slow = str(m.get("hint_slow", ""))
    timeout_title = str(m.get("timeout_title", ""))
    timeout_items = m.get("timeout_items") if isinstance(m.get("timeout_items"), list) else []
    timeout_items = [str(x) for x in timeout_items if str(x).strip()]
    error_prefix = str(m.get("error_prefix", "报告生成失败：" if lang != "en" else "Report generation failed: "))
    diag_label = str(m.get("diag_label", "运维排查建议" if lang != "en" else "Ops Checklist"))
    diag_button = str(m.get("diag_button", "手动诊断" if lang != "en" else "Run Diagnostics"))
    diag_loading = str(m.get("diag_loading", "正在获取诊断信息..." if lang != "en" else "Loading diagnostics..."))
    diag_error_prefix = str(m.get("diag_error_prefix", "诊断失败：" if lang != "en" else "Diagnostics failed: "))
    api_url = f"/api/report?date={report_date}&lang={lang}&provider={provider}&mode={mode}"
    diag_url = f"/api/diagnostics?lang={lang}"
    timeout_items_html = "".join(f"<li>{html.escape(x)}</li>" for x in timeout_items)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>{title}</title>
<style>
body{{font-family:"Segoe UI","PingFang SC","Microsoft YaHei",Arial,sans-serif;color:#1d2939;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:
linear-gradient(rgba(10,18,28,.75), rgba(10,18,28,.75)),
url('/static/digitaltwin.png') center/cover no-repeat fixed;}}
.card{{background:rgba(248,251,255,.95);border:1px solid #b8d0e8;border-radius:14px;padding:24px;min-width:420px;max-width:720px;box-shadow:0 10px 30px rgba(0,0,0,.22)}}
.spinner{{width:24px;height:24px;border:3px solid #d0d5dd;border-top-color:#1453c2;border-radius:50%;animation:spin 1s linear infinite;display:inline-block;vertical-align:middle;margin-right:10px}}
.muted{{color:#475467;font-size:14px;margin-top:8px}}
.warn{{margin-top:12px;padding:10px;border:1px solid #f7b955;border-radius:8px;background:#fff8e6;display:none}}
.warn ul{{margin:8px 0 0 18px;padding:0}}
.btn{{margin-top:12px;padding:8px 12px;border:1px solid #1453c2;background:#fff;color:#1453c2;border-radius:8px;cursor:pointer}}
.diag{{margin-top:12px;padding:10px;border:1px solid #d0d5dd;border-radius:8px;background:#fcfcfd;display:none;white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;max-height:300px;overflow:auto}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
</style></head>
<body>
<div class="card">
  <div><span class="spinner"></span><b>{title}</b></div>
  <div id="status" class="muted">{html.escape(steps[0])}</div>
  <div class="muted">{html.escape(hint_slow)}</div>
  <div class="muted">date={html.escape(report_date)} | provider={html.escape(provider)}</div>
  <button id="diagBtn" class="btn">{html.escape(diag_button)}</button>
  <div id="diagBox" class="diag"></div>
  <div id="warn" class="warn">
    <div><b>{html.escape(timeout_title)}</b></div>
    <div class="muted">{html.escape(diag_label)}</div>
    <ul>{timeout_items_html}</ul>
  </div>
</div>
<script>
const msgs = {json.dumps(steps, ensure_ascii=False)};
let idx = 0;
setInterval(() => {{
  idx = (idx + 1) % msgs.length;
  document.getElementById('status').textContent = msgs[idx];
}}, 1600);
setTimeout(() => {{
  const warn = document.getElementById('warn');
  if (warn) warn.style.display = 'block';
}}, 5 * 60 * 1000);

document.getElementById('diagBtn').addEventListener('click', async () => {{
  const box = document.getElementById('diagBox');
  box.style.display = 'block';
  box.textContent = {json.dumps(diag_loading, ensure_ascii=False)};
  try {{
    const res = await fetch({json.dumps(diag_url)}, {{credentials: 'same-origin'}});
    const data = await res.json();
    if (!res.ok) throw new Error(data && (data.message || data.error) ? (data.message || data.error) : 'request failed');
    box.textContent = JSON.stringify(data, null, 2);
  }} catch (e) {{
    box.textContent = {json.dumps(diag_error_prefix, ensure_ascii=False)} + e;
  }}
}});

fetch({json.dumps(api_url)}, {{credentials: 'same-origin'}})
  .then(async (res) => {{
    const data = await res.json();
    if (!res.ok) {{
      throw new Error(data && (data.message || data.error) ? (data.message || data.error) : 'request failed');
    }}
    if (!data.html) {{
      throw new Error('missing rendered html');
    }}
    document.open();
    document.write(data.html);
    document.close();
  }})
  .catch((err) => {{
    document.getElementById('status').textContent = {json.dumps(error_prefix, ensure_ascii=False)} + err;
    const warn = document.getElementById('warn');
    if (warn) warn.style.display = 'block';
  }});
</script>
</body></html>"""
