import os
import threading
from typing import Any
import statistics

from werkzeug.serving import make_server

from report_logic import load_sensor_validate_from_s3

try:
    from dash import Dash, Input, Output, dcc, html
    import plotly.graph_objects as go
except Exception:  # pragma: no cover
    Dash = None
    Input = None
    Output = None
    dcc = None
    html = None
    go = None


class SensorDashService:
    def __init__(self, host: str = "0.0.0.0", port: int = 8050, points: int = 1200):
        self.host = host
        self.port = port
        self.points = points
        self.app = None
        self._server = None
        self._thread = None
        if Dash is not None:
            self.app = self._build_app()

    @property
    def available(self) -> bool:
        return self.app is not None

    def _build_app(self):
        app = Dash(__name__)
        app.layout = html.Div(
            style={"fontFamily": "Segoe UI,Arial,sans-serif", "background": "#0b1220", "color": "#dbeafe", "minHeight": "100vh", "padding": "14px"},
            children=[
                html.H2("Sensor Validate (Dash, S3)"),
                html.Div(id="sensor-meta", style={"color": "#8fb0d8", "marginBottom": "8px"}),
                dcc.Graph(id="sensor-timeseries", config={"displayModeBar": False}),
                html.Div(id="drift-text", style={"border": "1px solid #1f365b", "borderRadius": "10px", "padding": "10px", "background": "#111a2d", "marginBottom": "10px"}),
                dcc.Graph(id="drift-gauge", config={"displayModeBar": False}),
                dcc.Interval(id="refresh", interval=60 * 1000, n_intervals=0),
            ],
        )

        @app.callback(
            Output("sensor-meta", "children"),
            Output("sensor-timeseries", "figure"),
            Output("drift-text", "children"),
            Output("drift-gauge", "figure"),
            Input("refresh", "n_intervals"),
        )
        def _refresh(_):
            payload = load_sensor_validate_from_s3(max_points=self.points)
            pts = payload.get("points", []) if isinstance(payload, dict) else []
            summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
            ts = [p.get("t", "") for p in pts]
            clean = [p.get("clean_data") for p in pts]
            prob = [p.get("drift_prob") for p in pts]
            degree = [p.get("drift_degree") for p in pts]
            clean_num = [float(x) for x in clean if isinstance(x, (int, float))]
            mean_v = statistics.mean(clean_num) if clean_num else None
            std_v = statistics.pstdev(clean_num) if len(clean_num) > 1 else 0.0
            ucl = (mean_v + 3 * std_v) if mean_v is not None else None
            lcl = (mean_v - 3 * std_v) if mean_v is not None else None
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=ts, y=clean, mode="lines", name="clean_data", line={"color": "#60a5fa"}))
            # sensor_check_dash.py style: show drift by coloring clean_data segments.
            if ts and clean and prob:
                drift_light_y = []
                drift_mid_y = []
                drift_high_y = []
                for i, v in enumerate(clean):
                    p = prob[i] if i < len(prob) else None
                    if not isinstance(v, (int, float)) or not isinstance(p, (int, float)):
                        drift_light_y.append(None)
                        drift_mid_y.append(None)
                        drift_high_y.append(None)
                        continue
                    drift_light_y.append(v if (0.2 <= p <= 0.3) else None)
                    drift_mid_y.append(v if (0.3 < p < 0.5) else None)
                    drift_high_y.append(v if (p >= 0.5) else None)
                if any(x is not None for x in drift_light_y):
                    fig.add_trace(
                        go.Scatter(
                            x=ts,
                            y=drift_light_y,
                            mode="lines",
                            name="Drift 0.2-0.3",
                            line={"color": "#fde68a", "width": 3},
                            connectgaps=False,
                        )
                    )
                if any(x is not None for x in drift_mid_y):
                    fig.add_trace(
                        go.Scatter(
                            x=ts,
                            y=drift_mid_y,
                            mode="lines",
                            name="Drift 0.3-0.5",
                            line={"color": "#f59e0b", "width": 3},
                            connectgaps=False,
                        )
                    )
                if any(x is not None for x in drift_high_y):
                    fig.add_trace(
                        go.Scatter(
                            x=ts,
                            y=drift_high_y,
                            mode="lines",
                            name="Drift >= 0.5",
                            line={"color": "#c2410c", "width": 3},
                            connectgaps=False,
                        )
                    )
            if ucl is not None and lcl is not None and ts:
                fig.add_trace(
                    go.Scatter(
                        x=ts,
                        y=[ucl] * len(ts),
                        mode="lines",
                        name="UCL",
                        line={"color": "#9ca3af", "dash": "dash"},
                    )
                )
                fig.add_trace(
                    go.Scatter(
                        x=ts,
                        y=[lcl] * len(ts),
                        mode="lines",
                        name="LCL",
                        line={"color": "#9ca3af", "dash": "dash"},
                    )
                )
                ox = []
                oy = []
                for i, v in enumerate(clean):
                    if isinstance(v, (int, float)) and (v > ucl or v < lcl):
                        ox.append(ts[i])
                        oy.append(v)
                if ox:
                    fig.add_trace(
                        go.Scatter(
                            x=ox,
                            y=oy,
                            mode="markers",
                            name="Outlier",
                            marker={"symbol": "x", "size": 8, "color": "#ef4444"},
                        )
                    )
            fig.update_layout(
                paper_bgcolor="#111a2d",
                plot_bgcolor="#0f1a30",
                font={"color": "#dbeafe"},
                legend={"orientation": "h"},
                margin={"l": 40, "r": 40, "t": 20, "b": 35},
                xaxis={"showgrid": False},
                yaxis={"title": "clean_data", "gridcolor": "#223b5e", "showgrid": True},
            )
            lp = summary.get("latest_drift_prob")
            lg = summary.get("latest_drift_degree")
            if isinstance(lp, (int, float)):
                pct = float(lp) * 100.0
                direction = "上升" if (float(lg) if isinstance(lg, (int, float)) else 0.0) > 0 else "下降"
                if float(lp) < 0.2:
                    status = f"飘移概率: {pct:.1f}% - 无明显飘移（{direction}）"
                elif float(lp) <= 0.3:
                    status = f"飘移概率: {pct:.1f}% - 轻微误差（{direction}）"
                elif float(lp) < 0.5:
                    status = f"飘移概率: {pct:.1f}% - 有漂移迹象（{direction}）"
                else:
                    status = f"飘移概率: {pct:.1f}% - 飘移显著（{direction}）"
            else:
                status = "飘移概率: N/A"
            text = (
                f"{status} | latest_time={summary.get('latest_time', 'N/A')} | "
                f"clean_mean={summary.get('clean_mean', 'N/A')} | "
                f"latest_drift_prob={lp} | latest_drift_degree={lg} | "
                f"ucl={round(ucl, 4) if ucl is not None else 'N/A'} | lcl={round(lcl, 4) if lcl is not None else 'N/A'}"
            )
            gauge_val = float(lp) * 100.0 if isinstance(lp, (int, float)) else 0.0
            gauge = go.Figure(
                go.Indicator(
                    mode="gauge+number",
                    value=gauge_val,
                    number={"suffix": "%"},
                    title={"text": "Drift Probability (%)"},
                    gauge={
                        "axis": {"range": [0, 100]},
                        "bar": {"color": "#f59e0b"},
                        "steps": [
                            {"range": [0, 50], "color": "#fde68a"},
                            {"range": [50, 100], "color": "#fca5a5"},
                        ],
                    },
                )
            )
            gauge.update_layout(paper_bgcolor="#111a2d", font={"color": "#dbeafe"}, height=220, margin={"l": 20, "r": 20, "t": 10, "b": 10})
            meta = f"rows={payload.get('rows', 0)} | source={payload.get('source_key', 'N/A')} | manifest={payload.get('used_manifest_key', 'N/A')}"
            return meta, fig, text, gauge

        return app

    def start(self) -> None:
        if not self.available:
            return
        if self._server is not None:
            return
        self._server = make_server(self.host, self.port, self.app.server)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()


def start_sensor_dash_if_enabled() -> dict[str, Any]:
    enabled = os.environ.get("REPORT_SENSOR_DASH_ENABLED", "1") == "1"
    if not enabled:
        return {"enabled": False, "available": False, "host": "", "port": 0}
    host = os.environ.get("REPORT_SENSOR_DASH_HOST", "0.0.0.0")
    port = int(os.environ.get("REPORT_SENSOR_DASH_PORT", "8060"))
    points = int(os.environ.get("REPORT_SENSOR_DASH_POINTS", "1200"))
    svc = SensorDashService(host=host, port=port, points=points)
    svc.start()
    return {"enabled": True, "available": svc.available, "host": host, "port": port}
