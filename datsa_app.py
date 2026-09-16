"""
DATSA Prototype (Python)
------------------------
Target profile generation, CSV auto-column-detection, path simulation
on a satellite map, and Excel export.

The map is a persistent Leaflet map (client-side JS in assets/datsa_map.js)
that is only ever updated in place — this is what keeps playback smooth
instead of flickering/rezooming on every animation frame.

Run:
    pip install dash pandas openpyxl
    python datsa_app.py
Then open the printed http://127.0.0.1:8050 link in your browser.

IMPORTANT: keep the "assets" folder (containing datsa_map.js) in the same
directory as this script — Dash auto-loads everything inside ./assets.
"""

import base64
import io
import math
import re
from datetime import datetime

import pandas as pd
from dash import Dash, dcc, html, Input, Output, State, dash_table, no_update, clientside_callback

# ----------------------------------------------------------------------
# Column auto-detection
# ----------------------------------------------------------------------

NAME_HINTS = {
    "lat": re.compile(r"^(lat|latitude|y)$", re.I),
    "lon": re.compile(r"^(lon|lng|long|longitude|x)$", re.I),
    "alt": re.compile(r"^(alt|altitude|elev|elevation|height|h)$", re.I),
    "speed": re.compile(r"^(spd|speed|vel|velocity|tas|ias|gs|groundspeed)$", re.I),
}
LOOSE_HINTS = {k: re.compile(k, re.I) for k in NAME_HINTS}


def detect_columns(df: pd.DataFrame) -> dict:
    """Guess which columns are lat/lon/alt/speed using header names + value ranges."""
    scores = {"lat": {}, "lon": {}, "alt": {}, "speed": {}}
    numeric_cols = {}

    for col in df.columns:
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if series.empty:
            continue
        numeric_cols[col] = series
        vmin, vmax = series.min(), series.max()
        clean = col.strip()

        for field, pat in NAME_HINTS.items():
            if pat.match(clean):
                scores[field][col] = scores[field].get(col, 0) + 100
            elif LOOSE_HINTS[field].search(clean):
                scores[field][col] = scores[field].get(col, 0) + 40

        if -90 <= vmin and vmax <= 90:
            scores["lat"][col] = scores["lat"].get(col, 0) + 15
        if (vmin >= -180 and vmax <= 180) and (vmax > 90 or vmin < -90):
            scores["lon"][col] = scores["lon"].get(col, 0) + 20
        if -180 <= vmin and vmax <= 180:
            scores["lon"][col] = scores["lon"].get(col, 0) + 8
        if -500 <= vmin and vmax <= 50000:
            scores["alt"][col] = scores["alt"].get(col, 0) + 5
        if 0 <= vmin and vmax <= 1000:
            scores["speed"][col] = scores["speed"].get(col, 0) + 4

    used = []

    def pick(field):
        candidates = {c: s for c, s in scores[field].items() if c not in used}
        if not candidates:
            return None
        best = max(candidates, key=candidates.get)
        return best if candidates[best] > 0 else None

    result = {}
    for field in ["lat", "lon", "alt", "speed"]:
        col = pick(field)
        if col:
            used.append(col)
        result[field] = col

    # Hard fallback for lat/lon if scoring was inconclusive
    if not result["lat"] or not result["lon"]:
        remaining = [c for c in numeric_cols if c not in used]
        if not result["lat"]:
            for c in remaining:
                s = numeric_cols[c]
                if s.min() >= -90 and s.max() <= 90:
                    result["lat"] = c
                    used.append(c)
                    break
        if not result["lon"]:
            for c in remaining:
                if c == result["lat"]:
                    continue
                s = numeric_cols[c]
                if s.min() >= -180 and s.max() <= 180:
                    result["lon"] = c
                    used.append(c)
                    break

    return result


def build_waypoints_from_df(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    out = pd.DataFrame()
    out["lat"] = pd.to_numeric(df[mapping["lat"]], errors="coerce")
    out["lon"] = pd.to_numeric(df[mapping["lon"]], errors="coerce")
    out["alt"] = pd.to_numeric(df[mapping["alt"]], errors="coerce") if mapping.get("alt") else 0.0
    out["speed"] = pd.to_numeric(df[mapping["speed"]], errors="coerce") if mapping.get("speed") else 50.0
    out = out.dropna(subset=["lat", "lon"]).reset_index(drop=True)
    out["alt"] = out["alt"].fillna(0)
    out["speed"] = out["speed"].fillna(50)
    out.insert(0, "id", [f"WP{i + 1}" for i in range(len(out))])
    return out


# ----------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def smooth_path(waypoints: pd.DataFrame, points_per_segment: int = 20) -> pd.DataFrame:
    """
    Fit a Catmull-Rom spline through the waypoints so the path curves smoothly
    through turns instead of forming sharp corners. Passes exactly through
    every original waypoint. Falls back to the raw points if there are fewer
    than 3 (a spline needs at least 3 points to curve).
    """
    pts = waypoints[["lat", "lon", "alt", "speed"]].values.tolist()
    if len(pts) < 3:
        return waypoints[["lat", "lon", "alt", "speed"]].reset_index(drop=True)

    # duplicate endpoints so the curve has control points before the first
    # and after the last waypoint (standard Catmull-Rom boundary handling)
    extended = [pts[0]] + pts + [pts[-1]]
    result = []
    for i in range(1, len(extended) - 2):
        p0, p1, p2, p3 = extended[i - 1], extended[i], extended[i + 1], extended[i + 2]
        for j in range(points_per_segment):
            t = j / points_per_segment
            t2, t3 = t * t, t * t * t
            point = []
            for k in range(4):  # lat, lon, alt, speed each smoothed independently
                a0, a1, a2, a3 = p0[k], p1[k], p2[k], p3[k]
                val = 0.5 * (
                    (2 * a1)
                    + (-a0 + a2) * t
                    + (2 * a0 - 5 * a1 + 4 * a2 - a3) * t2
                    + (-a0 + 3 * a1 - 3 * a2 + a3) * t3
                )
                point.append(val)
            result.append(point)
    result.append(pts[-1])
    return pd.DataFrame(result, columns=["lat", "lon", "alt", "speed"])


def simulate_track(waypoints: pd.DataFrame, sample_interval_s: float = 1.0, points_per_segment: int = 20):
    """
    Interpolate motion along a smoothed (curved) version of the waypoint path.

    Returns (track_df, waypoint_markers): waypoint_markers is a list, one entry
    per ORIGINAL waypoint (in order), giving the cumulative distance along the
    flown path at which that waypoint is reached — the basis for Range-to-Go /
    Time-to-Go against a selected Point of Interest.
    """
    dense = smooth_path(waypoints, points_per_segment=points_per_segment)
    n_wp = len(waypoints)
    # smooth_path places original waypoint k at dense index k*points_per_segment
    # for all but the last, and at the final dense index for the last waypoint.
    marker_dense_idx = {k * points_per_segment: k for k in range(max(n_wp - 1, 0))}
    if n_wp > 0:
        marker_dense_idx[len(dense) - 1] = n_wp - 1

    rows = []
    t = 0.0
    dist_total = 0.0
    marker_dist_km = {}
    if 0 in marker_dense_idx:
        marker_dist_km[marker_dense_idx[0]] = 0.0

    for i in range(len(dense) - 1):
        a, b = dense.iloc[i], dense.iloc[i + 1]
        leg_dist = haversine_m(a.lat, a.lon, b.lat, b.lon)
        spd = max((a.speed + b.speed) / 2, 0.1)
        leg_time = leg_dist / spd if spd > 0 else 0
        brg = bearing_deg(a.lat, a.lon, b.lat, b.lon)
        steps = max(1, round(leg_time / sample_interval_s))
        for s in range(steps):
            frac = s / steps
            lat = a.lat + (b.lat - a.lat) * frac
            lon = a.lon + (b.lon - a.lon) * frac
            alt = a.alt + (b.alt - a.alt) * frac
            rows.append({
                "time_s": round(t, 1),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "altitude_m": round(alt, 1),
                "speed_ms": round(spd, 1),
                "heading_deg": round(brg, 1),
                "distance_km": round((dist_total + leg_dist * frac) / 1000, 3),
            })
            t += sample_interval_s
        dist_total += leg_dist
        if (i + 1) in marker_dense_idx:
            marker_dist_km[marker_dense_idx[i + 1]] = round(dist_total / 1000, 3)

    last = dense.iloc[-1]
    rows.append({
        "time_s": round(t, 1), "lat": last.lat, "lon": last.lon,
        "altitude_m": last.alt, "speed_ms": last.speed, "heading_deg": rows[-1]["heading_deg"] if rows else 0,
        "distance_km": round(dist_total / 1000, 3),
    })

    waypoint_markers = [
        {
            "id": waypoints.iloc[k]["id"] if "id" in waypoints.columns else f"WP{k + 1}",
            "distance_km": marker_dist_km.get(k, round(dist_total / 1000, 3)),
        }
        for k in range(n_wp)
    ]
    return pd.DataFrame(rows), waypoint_markers


# ----------------------------------------------------------------------
# App layout
# ----------------------------------------------------------------------

COLORS = dict(bg="#0d1b1e", panel="#0f2b30", panel2="#123640", line="#1e4952",
              accent="#3ddc97", accent2="#ffb454", text="#d8ecec", dim="#82a5a8")

app = Dash(
    __name__,
    external_scripts=["https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"],
    external_stylesheets=["https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css"],
)
app.title = "DATSA Prototype (Python)"
server = app.server  # exposes the underlying Flask app for gunicorn/WSGI hosts


def btn_style(primary=False):
    return {
        "width": "100%", "padding": "10px 12px", "borderRadius": "7px",
        "border": f"1px solid {COLORS['accent'] if primary else COLORS['line']}",
        "background": COLORS["accent"] if primary else COLORS["panel2"],
        "color": "#04241a" if primary else COLORS["text"],
        "fontWeight": "700", "cursor": "pointer", "marginTop": "6px",
    }


def readout_box(label, value, box_id=None):
    value_props = {"style": {"fontFamily": "monospace", "fontSize": "15px", "fontWeight": "700"}}
    if box_id:
        value_props["id"] = box_id
    return html.Div(style={
        "background": COLORS["panel2"], "borderRadius": "8px", "padding": "9px 11px",
        "border": f"1px solid {COLORS['line']}",
    }, children=[
        html.Div(label, style={"fontSize": "9.5px", "textTransform": "uppercase", "color": COLORS["dim"]}),
        html.Div(value, **value_props),
    ])


app.layout = html.Div(style={
    "background": COLORS["bg"], "color": COLORS["text"], "fontFamily": "Inter, sans-serif",
    "height": "100vh", "display": "flex", "flexDirection": "column",
}, children=[

    html.Div(style={
        "display": "flex", "justifyContent": "space-between", "alignItems": "center",
        "padding": "14px 22px", "background": COLORS["panel"], "borderBottom": f"1px solid {COLORS['line']}",
    }, children=[
        html.Div([
            html.H1("DATSA", style={"margin": 0, "fontSize": "18px", "display": "inline-block", "marginRight": "10px"}),
            html.Span("target path & simulation — python prototype", style={"fontSize": "12px", "color": COLORS["dim"]}),
        ]),
        html.Div(id="status-pill", style={
            "fontSize": "11px", "padding": "4px 12px", "borderRadius": "20px",
            "background": "rgba(61,220,151,.15)", "color": COLORS["accent"], "fontWeight": "700",
        }, children="Idle"),
    ]),

    html.Div(style={"flex": 1, "display": "flex", "minHeight": 0}, children=[

        html.Div(style={"flex": 1, "position": "relative"}, children=[
            html.Div(id="map-div", style={"width": "100%", "height": "100%"}),
            html.Div(id="clientside-dummy", style={"display": "none"}),
        ]),

        html.Div(style={
            "width": "360px", "background": COLORS["panel"], "borderLeft": f"1px solid {COLORS['line']}",
            "overflowY": "auto", "padding": "0",
        }, children=[

            html.Div(style={"padding": "16px 18px", "borderBottom": f"1px solid {COLORS['line']}"}, children=[
                html.H2("Import CSV", style={"fontSize": "11px", "textTransform": "uppercase",
                                              "color": COLORS["accent"], "letterSpacing": "0.08em"}),
                dcc.Upload(
                    id="csv-upload",
                    children=html.Div(["Drop a .csv here or click to upload"]),
                    style={
                        "border": f"1.5px dashed {COLORS['line']}", "borderRadius": "8px", "padding": "16px",
                        "textAlign": "center", "fontSize": "12.5px", "color": COLORS["dim"], "cursor": "pointer",
                    },
                    multiple=False,
                ),
                html.Div(id="csv-mapping-result", style={"fontSize": "11.5px", "marginTop": "10px", "fontFamily": "monospace"}),
            ]),

            html.Div(style={"padding": "16px 18px", "borderBottom": f"1px solid {COLORS['line']}"}, children=[
                html.H2("Waypoints", style={"fontSize": "11px", "textTransform": "uppercase",
                                             "color": COLORS["accent"], "letterSpacing": "0.08em"}),
                dash_table.DataTable(
                    id="wp-table",
                    columns=[
                        {"name": "Point ID", "id": "id", "type": "text"},
                        {"name": "Lat", "id": "lat", "type": "numeric"},
                        {"name": "Lon", "id": "lon", "type": "numeric"},
                        {"name": "Alt (m)", "id": "alt", "type": "numeric", "editable": True},
                        {"name": "Speed (m/s)", "id": "speed", "type": "numeric", "editable": True},
                    ],
                    data=[],
                    style_table={"maxHeight": "180px", "overflowY": "auto"},
                    style_cell={"backgroundColor": COLORS["panel2"], "color": COLORS["text"],
                                "fontSize": "11px", "fontFamily": "monospace", "border": "none"},
                    style_header={"backgroundColor": COLORS["panel2"], "color": COLORS["dim"],
                                  "fontSize": "10px", "textTransform": "uppercase", "border": "none"},
                    editable=True,
                    row_deletable=True,
                ),
            ]),

            html.Div(style={"padding": "16px 18px", "borderBottom": f"1px solid {COLORS['line']}"}, children=[
                html.H2("Simulation", style={"fontSize": "11px", "textTransform": "uppercase",
                                              "color": COLORS["accent"], "letterSpacing": "0.08em"}),
                html.Button("▶ Generate Simulation", id="btn-simulate", n_clicks=0, style=btn_style(primary=True)),
                html.Div(style={"display": "flex", "gap": "8px", "marginTop": "6px"}, children=[
                    html.Button("▶ Play", id="btn-play-toggle", n_clicks=0,
                                style={**btn_style(), "marginTop": 0}),
                    dcc.Dropdown(
                        id="play-speed",
                        options=[{"label": f"{x}×", "value": x} for x in [1, 2, 5, 10, 25]],
                        value=5, clearable=False, style={"width": "90px", "fontSize": "12px"},
                    ),
                ]),
                html.Div(style={"marginTop": "10px"}, children=[
                    html.Label("Scrub playback", style={"fontSize": "11px", "color": COLORS["dim"]}),
                    dcc.Slider(id="play-slider", min=0, max=0, step=1, value=0,
                               tooltip={"placement": "bottom"}, updatemode="drag"),
                ]),
                dcc.Interval(id="play-interval", interval=200, n_intervals=0, disabled=True),
                dcc.Store(id="store-playing", data=False),
            ]),

            html.Div(style={"padding": "16px 18px", "borderBottom": f"1px solid {COLORS['line']}"}, children=[
                html.H2("Live Readout", style={"fontSize": "11px", "textTransform": "uppercase",
                                                "color": COLORS["accent"], "letterSpacing": "0.08em"}),
                html.Div(id="readout-panel", style={
                    "display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "8px", "fontSize": "13px",
                }),
            ]),

            html.Div(style={"padding": "16px 18px", "borderBottom": f"1px solid {COLORS['line']}"}, children=[
                html.H2("Range / Time to Go", style={"fontSize": "11px", "textTransform": "uppercase",
                                                       "color": COLORS["accent"], "letterSpacing": "0.08em"}),
                html.Label("Point of Interest", style={"fontSize": "11px", "color": COLORS["dim"]}),
                dcc.Dropdown(
                    id="poi-select", options=[], value=None, clearable=True,
                    placeholder="Select a waypoint…", style={"fontSize": "12px", "marginTop": "4px"},
                ),
                html.Label("Speed override (m/s)", style={"fontSize": "11px", "color": COLORS["dim"],
                                                            "marginTop": "8px", "display": "block"}),
                dcc.Input(
                    id="speed-override", type="number", placeholder="use current speed",
                    style={"width": "100%", "fontSize": "12px", "padding": "6px", "marginTop": "4px",
                           "background": COLORS["panel2"], "color": COLORS["text"],
                           "border": f"1px solid {COLORS['line']}", "borderRadius": "6px"},
                ),
                html.Div(style={
                    "display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "8px", "marginTop": "10px",
                }, children=[
                    readout_box("Range to Go", "—", box_id="range-to-go-box"),
                    readout_box("Time to Go", "—", box_id="time-to-go-box"),
                ]),
            ]),

            html.Div(style={"padding": "16px 18px"}, children=[
                html.H2("Export", style={"fontSize": "11px", "textTransform": "uppercase",
                                          "color": COLORS["accent"], "letterSpacing": "0.08em"}),
                html.Button("⬇ Save results to Excel", id="btn-export", n_clicks=0, style=btn_style(primary=True)),
                dcc.Download(id="download-xlsx"),
                html.Div("Run a simulation first — track points and waypoints are written to a downloadable .xlsx.",
                         style={"fontSize": "11px", "color": COLORS["dim"], "marginTop": "8px"}),
            ]),
        ]),
    ]),

    dcc.Store(id="store-waypoints"),
    dcc.Store(id="store-track"),
    dcc.Store(id="store-wp-markers"),
])


# ----------------------------------------------------------------------
# Callbacks
# ----------------------------------------------------------------------

@app.callback(
    Output("store-waypoints", "data"),
    Output("csv-mapping-result", "children"),
    Output("wp-table", "data"),
    Output("store-track", "data", allow_duplicate=True),
    Output("play-slider", "max", allow_duplicate=True),
    Output("play-slider", "value", allow_duplicate=True),
    Output("store-playing", "data", allow_duplicate=True),
    Output("store-wp-markers", "data", allow_duplicate=True),
    Input("csv-upload", "contents"),
    State("csv-upload", "filename"),
    prevent_initial_call=True,
)
def handle_csv_upload(contents, filename):
    content_type, content_string = contents.split(",")
    decoded = base64.b64decode(content_string)
    df = pd.read_csv(io.StringIO(decoded.decode("utf-8-sig")))
    df.columns = [c.strip() for c in df.columns]

    mapping = detect_columns(df)
    if not mapping["lat"] or not mapping["lon"]:
        msg = html.Div("Could not detect lat/lon columns.", style={"color": COLORS["accent2"]})
        return no_update, msg, no_update, no_update, no_update, no_update, no_update, no_update

    waypoints = build_waypoints_from_df(df, mapping)

    mapping_display = html.Div([
        html.Div(f"{field}: {mapping[field] or '— not found'}", style={"color": COLORS["dim"]})
        for field in ["lat", "lon", "alt", "speed"]
    ])

    # reset any previous simulation — new waypoints invalidate the old track/markers
    return (
        waypoints.to_dict("records"), mapping_display, waypoints.round(4).to_dict("records"),
        None, 0, 0, False, None,
    )


@app.callback(
    Output("store-track", "data"),
    Output("play-slider", "max"),
    Output("play-slider", "value"),
    Output("store-playing", "data", allow_duplicate=True),
    Output("store-wp-markers", "data"),
    Input("btn-simulate", "n_clicks"),
    State("wp-table", "data"),
    prevent_initial_call=True,
)
def run_simulation(sim_clicks, table_data):
    waypoints = pd.DataFrame(table_data) if table_data else None
    if waypoints is None or len(waypoints) < 2:
        return no_update, no_update, no_update, no_update, no_update
    track, markers = simulate_track(waypoints)
    # auto-start playback so the target actually moves right away
    return track.to_dict("records"), max(0, len(track) - 1), 0, True, markers


# Drives the persistent client-side Leaflet map (assets/datsa_map.js).
# Only the marker position / line data changes on each tick — the map
# itself, its zoom, and its pan are never rebuilt, so there is no
# flicker or zoom reset during playback.
clientside_callback(
    "window.dash_clientside.datsa.updateMap",
    Output("clientside-dummy", "children"),
    Input("wp-table", "data"),
    Input("store-track", "data"),
    Input("play-slider", "value"),
)


@app.callback(
    Output("store-playing", "data"),
    Output("btn-play-toggle", "children"),
    Input("btn-play-toggle", "n_clicks"),
    State("store-playing", "data"),
    prevent_initial_call=True,
)
def toggle_play(n_clicks, playing):
    new_state = not playing
    return new_state, ("⏸ Pause" if new_state else "▶ Play")


@app.callback(
    Output("play-interval", "disabled"),
    Input("store-playing", "data"),
)
def toggle_interval(playing):
    return not bool(playing)


# Advances playback entirely client-side (see assets/datsa_map.js) so each
# animation frame doesn't wait on a server round-trip — this is what makes
# playback feel smooth even on a slow/distant free-tier host.
clientside_callback(
    "window.dash_clientside.datsa.advancePlayback",
    Output("play-slider", "value", allow_duplicate=True),
    Output("store-playing", "data", allow_duplicate=True),
    Input("play-interval", "n_intervals"),
    State("play-slider", "value"),
    State("play-slider", "max"),
    State("play-speed", "value"),
    State("store-playing", "data"),
    prevent_initial_call=True,
)


@app.callback(
    Output("readout-panel", "children"),
    Input("play-slider", "value"),
    State("store-track", "data"),
)
def update_readout(slider_val, track_data):
    if not track_data:
        return [readout_box(l, "—") for l in ["Lat/Lon", "Altitude", "Speed", "Heading", "Distance", "Time"]]
    track = pd.DataFrame(track_data)
    idx = min(slider_val, len(track) - 1)
    pt = track.iloc[idx]
    return [
        readout_box("Lat/Lon", f"{pt.lat:.4f}, {pt.lon:.4f}"),
        readout_box("Altitude", f"{pt.altitude_m:.0f} m"),
        readout_box("Speed", f"{pt.speed_ms:.1f} m/s"),
        readout_box("Heading", f"{pt.heading_deg:.0f}°"),
        readout_box("Distance", f"{pt.distance_km:.2f} km"),
        readout_box("Time", f"{pt.time_s:.0f} s"),
    ]


@app.callback(
    Output("poi-select", "options"),
    Output("poi-select", "value"),
    Input("wp-table", "data"),
    State("poi-select", "value"),
)
def populate_poi_options(table_data, current_value):
    if not table_data:
        return [], None
    ids = [row.get("id") for row in table_data if row.get("id")]
    options = [{"label": wp_id, "value": wp_id} for wp_id in ids]
    # keep the current selection if it's still a valid waypoint, otherwise clear it
    value = current_value if current_value in ids else None
    return options, value


@app.callback(
    Output("range-to-go-box", "children"),
    Output("time-to-go-box", "children"),
    Input("play-slider", "value"),
    Input("poi-select", "value"),
    Input("speed-override", "value"),
    State("store-track", "data"),
    State("store-wp-markers", "data"),
)
def update_range_time_to_go(slider_val, poi_id, speed_override, track_data, markers):
    if not track_data or not markers or not poi_id:
        return "—", "—"

    marker = next((m for m in markers if m["id"] == poi_id), None)
    if marker is None:
        return "—", "—"

    track = pd.DataFrame(track_data)
    idx = min(slider_val or 0, len(track) - 1)
    current_pt = track.iloc[idx]

    range_km = marker["distance_km"] - current_pt.distance_km
    if range_km <= 0:
        return "Passed", "—"

    # DATSA spec: default to current speed, allow manual override for Time-to-Go
    speed_mps = speed_override if speed_override and speed_override > 0 else current_pt.speed_ms
    if not speed_mps or speed_mps <= 0:
        return f"{range_km:.2f} km", "—"

    time_s = (range_km * 1000) / speed_mps
    mins, secs = divmod(int(round(time_s)), 60)
    return f"{range_km:.2f} km", f"{mins:02d}:{secs:02d}"


@app.callback(
    Output("download-xlsx", "data"),
    Input("btn-export", "n_clicks"),
    State("store-track", "data"),
    State("wp-table", "data"),
    prevent_initial_call=True,
)
def export_excel(n_clicks, track_data, wp_data):
    if not track_data:
        return no_update
    track = pd.DataFrame(track_data)
    waypoints = pd.DataFrame(wp_data)

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        track.to_excel(writer, sheet_name="Simulation Track", index=False)
        waypoints.to_excel(writer, sheet_name="Waypoints", index=False)
    buffer.seek(0)

    fname = f"DATSA_simulation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return dcc.send_bytes(buffer.getvalue(), fname)


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8050)), debug=False)