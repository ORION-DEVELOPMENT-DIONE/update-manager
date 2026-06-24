"""
Orion Energy Dashboard — Backend
Flask app serving REST API + static frontend on port 3001.
Reads energy data from local InfluxDB, system info from /proc + systemd.
Auth via device-bound token stored in /etc/orion/dashboard.token.
"""

import json
import logging
import os
import secrets
import socket
import subprocess
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, abort

# ── Optional InfluxDB import ─────────────────────────────────────────────────
try:
    from influxdb_client import InfluxDBClient
    INFLUX_OK = True
except ImportError:
    INFLUX_OK = False

# ── Config ────────────────────────────────────────────────────────────────────
INFLUX_URL        = os.environ.get("INFLUX_URL", "http://localhost:8086")
INFLUX_ORG        = os.environ.get("INFLUX_ORG", "orion")
INFLUX_BUCKET     = os.environ.get("INFLUX_BUCKET", "energy_metrics")
INFLUX_TOKEN_FILE = os.environ.get("INFLUX_TOKEN_FILE", "/home/orangepi/.influxdb_token")
DASHBOARD_TOKEN   = os.environ.get("DASHBOARD_TOKEN_FILE", "/etc/orion/dashboard.token")
STATIC_DIR        = os.environ.get("STATIC_DIR", "/home/orangepi/orion-dashboard/frontend")
PORT              = int(os.environ.get("DASHBOARD_PORT", "3001"))
TARIFF_FILE       = "/etc/orion/tariff.json"
DEFAULT_TARIFF    = 0.15  # €/kWh

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dashboard")


# ── Token management ──────────────────────────────────────────────────────────
def _ensure_token():
    """Generate dashboard access token on first run."""
    p = Path(DASHBOARD_TOKEN)
    if p.exists() and p.read_text().strip():
        return p.read_text().strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    p.write_text(token)
    os.chmod(str(p), 0o600)
    log.info(f"Generated new dashboard token → {DASHBOARD_TOKEN}")
    return token


_AUTH_TOKEN = _ensure_token()


def _load_influx_token():
    try:
        return Path(INFLUX_TOKEN_FILE).read_text().strip()
    except Exception as e:
        log.error(f"Cannot read InfluxDB token: {e}")
        return ""


def _get_tariff():
    try:
        return json.loads(Path(TARIFF_FILE).read_text()).get("rate", DEFAULT_TARIFF)
    except Exception:
        return DEFAULT_TARIFF


# ── InfluxDB client ───────────────────────────────────────────────────────────
_influx_client = None
_query_api = None

def _init_influx():
    global _influx_client, _query_api
    if not INFLUX_OK:
        log.warning("influxdb-client not installed")
        return
    token = _load_influx_token()
    if not token:
        return
    try:
        _influx_client = InfluxDBClient(url=INFLUX_URL, token=token, org=INFLUX_ORG, timeout=5000)
        _query_api = _influx_client.query_api()
        log.info("InfluxDB connected")
    except Exception as e:
        log.error(f"InfluxDB init failed: {e}")


def _flux_query(query):
    """Run Flux query, return list of dicts."""
    if not _query_api:
        return []
    try:
        tables = _query_api.query(query, org=INFLUX_ORG)
        rows = []
        for table in tables:
            for rec in table.records:
                rows.append(rec.values)
        return rows
    except Exception as e:
        log.error(f"Flux query error: {e}")
        return []


# ── System helpers ────────────────────────────────────────────────────────────
def _uptime_s():
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        return 0

def _cpu_temp():
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000.0
    except Exception:
        return 0

def _mem_free_mb():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0

def _disk_free_mb(path="/"):
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) // (1024 * 1024)
    except Exception:
        return 0

def _service_status(name):
    try:
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() == "active"
    except Exception:
        return False

def _wifi_rssi():
    """Get WiFi signal strength via nmcli (percentage → approx dBm)."""
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SIGNAL", "dev", "wifi"],
            capture_output=True, text=True, timeout=5
        )
        for line in r.stdout.splitlines():
            if line.startswith("yes:"):
                pct = int(line.split(":")[1])
                # Convert percentage to approximate dBm
                return int((pct / 2) - 100)
    except Exception:
        pass
    return -99

def _wifi_ssid():
    """Get connected WiFi SSID via nmcli."""
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
            capture_output=True, text=True, timeout=5
        )
        for line in r.stdout.splitlines():
            if line.startswith("yes:"):
                return line.split(":", 1)[1]
    except Exception:
        pass
    return "Unknown"

def _git_hash():
    try:
        r = subprocess.run(["git", "-C", "/home/orangepi/screen-manager", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=None)


# ── Auth middleware ────────────────────────────────────────────────────────────
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = (request.args.get("t")
                 or request.cookies.get("orion_token")
                 or request.headers.get("Authorization", "").replace("Bearer ", ""))
        if not token or token != _AUTH_TOKEN:
            abort(401)
        return f(*args, **kwargs)
    return decorated


# ── Static file serving ───────────────────────────────────────────────────────
@app.route("/")
def index():
    token = request.args.get("t")
    if token and token == _AUTH_TOKEN:
        # Set cookie so subsequent requests don't need ?t= in URL
        resp = send_from_directory(STATIC_DIR, "index.html")
        resp.set_cookie("orion_token", token, max_age=86400 * 30, httponly=True, samesite="Strict")
        return resp
    if request.cookies.get("orion_token") == _AUTH_TOKEN:
        return send_from_directory(STATIC_DIR, "index.html")
    abort(401)


@app.route("/<path:path>")
def static_files(path):
    """Serve frontend static files (JS, CSS, images). No auth on assets."""
    # Only protect HTML pages, not assets
    if path.endswith(".html"):
        token = request.args.get("t") or request.cookies.get("orion_token")
        if token != _AUTH_TOKEN:
            abort(401)
    return send_from_directory(STATIC_DIR, path)


# ── API: Live metrics ─────────────────────────────────────────────────────────
@app.route("/api/live")
@require_auth
def api_live():
    """Latest energy reading from InfluxDB. Falls back to older data if meter offline."""
    offline = False
    rows = _flux_query(f'''
        from(bucket:"{INFLUX_BUCKET}")
        |> range(start:-1h)
        |> filter(fn:(r) => r._measurement == "energy")
        |> last()
        |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
    ''')

    if not rows:
        # No recent data — try last 365 days for last known reading
        offline = True
        rows = _flux_query(f'''
            from(bucket:"{INFLUX_BUCKET}")
            |> range(start:-365d)
            |> filter(fn:(r) => r._measurement == "energy")
            |> group()
            |> sort(columns:["_time"])
            |> last()
            |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
        ''')

    if not rows:
        return jsonify({"error": "no_data", "message": "No solar data found", "offline": True}), 200

    r = rows[0]
    tariff = _get_tariff()

    # Per-phase data
    phases = []
    labels = ["r", "y", "b"]
    names = ["Phase R", "Phase Y", "Phase B"]
    colors = ["#00DCC8", "#8250FF", "#00A3FF"]
    for i, l in enumerate(labels):
        phases.append({
            "label": labels[i].upper(),
            "name": names[i],
            "current": r.get(f"current_{l}", 0),
            "power": r.get(f"power_{l}", 0),
            "color": colors[i],
            "connected": r.get(f"current_{l}", 0) > 0 or r.get(f"power_{l}", 0) > 0,
        })

    total_power = r.get("totalPower", 0)
    energy_total = r.get("energyTotal", 0)

    # Compute last seen label
    last_seen_label = None
    ts = r.get("_time")
    if hasattr(ts, "timestamp"):
        age = int(time.time() - ts.timestamp())
        if age < 60:
            last_seen_label = f"{age}s ago"
        elif age < 3600:
            last_seen_label = f"{age // 60}m ago"
        elif age < 86400:
            last_seen_label = f"{age // 3600}h {(age % 3600) // 60}m ago"
        else:
            last_seen_label = f"{age // 86400}d {(age % 86400) // 3600}h ago"

    return jsonify({
        "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "voltage": r.get("voltage", 0),
        "totalPower": total_power,
        "energyTotal": energy_total,
        "battery": r.get("battery", 0),
        "phases": phases,
        "phaseCount": r.get("phaseCount", 0),
        "tariff": tariff,
        "earningsEstimate": round(energy_total * tariff, 2),
        "offline": offline,
        "lastSeen": last_seen_label,
    })


# ── API: History ──────────────────────────────────────────────────────────────
@app.route("/api/history")
@require_auth
def api_history():
    """Power consumption time series."""
    rng = request.args.get("range", "24h")
    window = {
        "1h": "1m", "3h": "2m", "6h": "5m", "12h": "5m",
        "24h": "5m", "7d": "30m", "30d": "2h", "90d": "6h", "180d": "12h",
    }.get(rng, "5m")
    start = {
        "1h": "-1h", "3h": "-3h", "6h": "-6h", "12h": "-12h",
        "24h": "-24h", "7d": "-7d", "30d": "-30d", "90d": "-90d", "180d": "-180d",
    }.get(rng, "-24h")

    rows = _flux_query(f'''
        from(bucket:"{INFLUX_BUCKET}")
        |> range(start:{start})
        |> filter(fn:(r) => r._measurement == "energy" and r._field == "totalPower")
        |> aggregateWindow(every:{window}, fn:mean, createEmpty:false)
        |> yield(name:"power")
    ''')

    points = []
    for r in rows:
        t = r.get("_time")
        ts = t.timestamp() * 1000 if hasattr(t, "timestamp") else 0
        points.append({"t": int(ts), "v": round(r.get("_value", 0), 1)})

    # Stats
    vals = [p["v"] for p in points] if points else [0]
    return jsonify({
        "range": rng,
        "points": points,
        "stats": {
            "avg": round(sum(vals) / len(vals), 1),
            "peak": round(max(vals), 1),
            "min": round(min(vals), 1),
        }
    })


# ── API: Daily totals ────────────────────────────────────────────────────────
@app.route("/api/daily")
@require_auth
def api_daily():
    """Daily kWh totals for bar chart."""
    days = min(int(request.args.get("days", "14")), 180)
    tariff = _get_tariff()

    rows = _flux_query(f'''
        from(bucket:"{INFLUX_BUCKET}")
        |> range(start:-{days}d)
        |> filter(fn:(r) => r._measurement == "energy" and r._field == "totalPower")
        |> aggregateWindow(every:1d, fn:mean, createEmpty:false)
        |> map(fn:(r) => ({{r with _value: r._value * 24.0 / 1000.0}}))
    ''')

    daily = []
    total = 0
    for r in rows:
        t = r.get("_time")
        kwh = round(r.get("_value", 0), 2)
        total += kwh
        daily.append({
            "date": t.strftime("%Y-%m-%d") if hasattr(t, "strftime") else str(t),
            "day": t.strftime("%a") if hasattr(t, "strftime") else "",
            "kwh": kwh,
        })

    return jsonify({
        "days": daily,
        "totalKwh": round(total, 2),
        "totalEarnings": round(total * tariff, 2),
        "avgDaily": round(total / max(len(daily), 1), 2),
        "tariff": tariff,
    })


# ── API: Phases ───────────────────────────────────────────────────────────────
@app.route("/api/phases")
@require_auth
def api_phases():
    """Per-phase data with 6h mini-history, connection-aware."""
    labels = ["r", "y", "b"]
    names = ["Phase R", "Phase Y", "Phase B"]
    colors = ["#00DCC8", "#8250FF", "#00A3FF"]

    # Get phaseCount and connectedPhases from latest reading
    meta_rows = _flux_query(f'''
        from(bucket:"{INFLUX_BUCKET}")
        |> range(start:-1h)
        |> filter(fn:(r) => r._measurement == "energy" and r._field == "phaseCount")
        |> last()
    ''')
    phase_count = 0
    if meta_rows:
        phase_count = int(meta_rows[0].get("_value", 0))

    result = []
    for i, l in enumerate(labels):
        # Latest values
        latest = _flux_query(f'''
            from(bucket:"{INFLUX_BUCKET}")
            |> range(start:-1h)
            |> filter(fn:(r) => r._measurement == "energy" and (r._field == "power_{l}" or r._field == "current_{l}"))
            |> last()
        ''')

        power = 0
        current = 0
        for r in latest:
            f = r.get("_field", "")
            if f == f"power_{l}":
                power = round(r.get("_value", 0), 1)
            elif f == f"current_{l}":
                current = round(r.get("_value", 0), 2)

        connected = power > 0 or current > 0

        # Only fetch history for connected phases
        history = []
        if connected:
            rows = _flux_query(f'''
                from(bucket:"{INFLUX_BUCKET}")
                |> range(start:-6h)
                |> filter(fn:(r) => r._measurement == "energy" and r._field == "power_{l}")
                |> aggregateWindow(every:5m, fn:mean, createEmpty:false)
            ''')
            for r in rows:
                t = r.get("_time")
                ts = t.timestamp() * 1000 if hasattr(t, "timestamp") else 0
                history.append({"t": int(ts), "v": round(r.get("_value", 0), 1)})

        result.append({
            "label": l.upper(),
            "name": names[i],
            "color": colors[i],
            "power": power,
            "current": current,
            "connected": connected,
            "history": history,
        })

    connected_phases = [p for p in result if p["connected"]]
    total = sum(p["power"] for p in connected_phases)
    for p in result:
        p["percent"] = round((p["power"] / total * 100) if total > 0 and p["connected"] else 0, 1)

    return jsonify({
        "phases": result,
        "totalPower": total,
        "phaseCount": phase_count,
        "connectedCount": len(connected_phases),
    })


# ── API: Device ───────────────────────────────────────────────────────────────
@app.route("/api/device")
@require_auth
def api_device():
    """System status and service health."""
    uptime = _uptime_s()
    days = int(uptime // 86400)
    hours = int((uptime % 86400) // 3600)

    services = [
        {"name": "Orion Meter",  "unit": None, "ok": True},
        {"name": "Orion Gateway",       "unit": "screen.service"},
        {"name": "Orion Screen",  "unit": "screen.service"},
    ]

    for svc in services:
        if svc.get("unit"):
            svc["ok"] = _service_status(svc["unit"])
        del svc["unit"]

    # ESP32 last seen via MQTT timestamp in InfluxDB
    esp_rows = _flux_query(f'''
        from(bucket:"{INFLUX_BUCKET}")
        |> range(start:-365d)
        |> filter(fn:(r) => r._measurement == "energy" and r._field == "voltage")
        |> group()
        |> sort(columns:["_time"])
        |> last()
        |> keep(columns:["_time"])
    ''')
    esp_last_seen = None
    if esp_rows:
        t = esp_rows[0].get("_time")
        if hasattr(t, "timestamp"):
            esp_last_seen = int(time.time() - t.timestamp())
    services[0]["ok"] = esp_last_seen is not None and esp_last_seen < 1800

    # Format ESP32 last seen
    esp_label = None
    if esp_last_seen is not None:
        if esp_last_seen < 60:
            esp_label = f"{esp_last_seen}s ago"
        elif esp_last_seen < 3600:
            esp_label = f"{esp_last_seen // 60}m ago"
        elif esp_last_seen < 86400:
            esp_label = f"{esp_last_seen // 3600}h {(esp_last_seen % 3600) // 60}m ago"
        else:
            esp_days = esp_last_seen // 86400
            esp_hours = (esp_last_seen % 86400) // 3600
            esp_label = f"{esp_days}d {esp_hours}h ago"

    # Load average
    load_1m = 0
    try:
        load_1m = float(Path("/proc/loadavg").read_text().split()[0])
    except Exception:
        pass

    # Update status — check git for pending updates
    update_status = "up-to-date"
    try:
        subprocess.run(["git", "-C", "/home/orangepi/screen-manager", "fetch", "origin"],
                       capture_output=True, timeout=10)
        local = subprocess.run(["git", "-C", "/home/orangepi/screen-manager", "rev-parse", "HEAD"],
                               capture_output=True, text=True, timeout=3).stdout.strip()
        remote = subprocess.run(["git", "-C", "/home/orangepi/screen-manager", "rev-parse", "@{u}"],
                                capture_output=True, text=True, timeout=3).stdout.strip()
        if local and remote and local != remote:
            update_status = "update-available"
    except Exception:
        pass

    return jsonify({
        "hostname": socket.gethostname(),
        "deviceId": "F8B3B77FEAF4",
        "firmware": "1.2.0",
        "uptime": f"{days}d {hours}h",
        "uptimeSeconds": int(uptime),
        "cpuTemp": round(_cpu_temp(), 1),
        "memFreeMb": _mem_free_mb(),
        "diskFreeMb": _disk_free_mb(),
        "wifiRssi": _wifi_rssi(),
        "wifiSsid": _wifi_ssid(),
        "wifiStrength": "good" if _wifi_rssi() > -60 else ("fair" if _wifi_rssi() > -75 else "poor"),
        "esp32LastSeen": esp_last_seen,
        "esp32LastSeenLabel": esp_label,
        "load1m": round(load_1m, 2),
        "updateStatus": update_status,
        "services": services,
    })


# ── API: Token info (for QR code generation) ─────────────────────────────────
# ── API: Analytics — hourly pattern ────────────────────────────────────────────
@app.route("/api/analytics/hourly")
@require_auth
def api_analytics_hourly():
    """Average power by hour of day (last 30 days)."""
    rows = _flux_query(f'''
        from(bucket:"{INFLUX_BUCKET}")
        |> range(start:-30d)
        |> filter(fn:(r) => r._measurement == "energy" and r._field == "totalPower")
        |> aggregateWindow(every:1h, fn:mean, createEmpty:false)
        |> map(fn:(r) => ({{r with hour: date.hour(t: r._time)}}))
    ''')

    hourly = {}
    counts = {}
    for r in rows:
        h = r.get("hour", 0)
        v = r.get("_value", 0)
        hourly[h] = hourly.get(h, 0) + v
        counts[h] = counts.get(h, 0) + 1

    result = []
    peak_hour = 0
    peak_val = 0
    for h in range(24):
        avg = round(hourly.get(h, 0) / max(counts.get(h, 0), 1), 1)
        result.append({"hour": h, "label": f"{h:02d}:00", "avgPower": avg})
        if avg > peak_val:
            peak_val = avg
            peak_hour = h

    total_avg = sum(r["avgPower"] for r in result) / 24
    return jsonify({
        "hours": result,
        "peakHour": f"{peak_hour:02d}:00",
        "peakPower": peak_val,
        "avgPower": round(total_avg, 1),
    })


# ── API: Analytics — peaks ────────────────────────────────────────────────────
@app.route("/api/analytics/peaks")
@require_auth
def api_analytics_peaks():
    """Top 5 highest power readings in last 30 days."""
    rows = _flux_query(f'''
        from(bucket:"{INFLUX_BUCKET}")
        |> range(start:-30d)
        |> filter(fn:(r) => r._measurement == "energy" and r._field == "totalPower")
        |> aggregateWindow(every:15m, fn:max, createEmpty:false)
        |> group()
        |> sort(columns:["_value"], desc:true)
        |> limit(n:5)
    ''')

    peaks = []
    for r in rows:
        t = r.get("_time")
        peaks.append({
            "timestamp": t.isoformat() if hasattr(t, "isoformat") else str(t),
            "label": t.strftime("%b %d, %H:%M") if hasattr(t, "strftime") else "",
            "power": round(r.get("_value", 0), 1),
        })

    return jsonify({"peaks": peaks})


# ── API: Analytics — projection ───────────────────────────────────────────────
@app.route("/api/analytics/projection")
@require_auth
def api_analytics_projection():
    """Monthly cost projection based on last 7 days."""
    tariff = _get_tariff()

    rows = _flux_query(f'''
        from(bucket:"{INFLUX_BUCKET}")
        |> range(start:-7d)
        |> filter(fn:(r) => r._measurement == "energy" and r._field == "totalPower")
        |> aggregateWindow(every:1d, fn:mean, createEmpty:false)
        |> map(fn:(r) => ({{r with _value: r._value * 24.0 / 1000.0}}))
    ''')

    daily_kwh = [r.get("_value", 0) for r in rows]
    if not daily_kwh:
        return jsonify({"avgDailyKwh": 0, "projectedMonthlyKwh": 0,
                        "projectedMonthlyCost": 0, "tariff": tariff, "daysOfData": 0})

    avg_daily = sum(daily_kwh) / len(daily_kwh)
    projected_monthly = avg_daily * 30

    return jsonify({
        "avgDailyKwh": round(avg_daily, 2),
        "projectedMonthlyKwh": round(projected_monthly, 1),
        "projectedMonthlyCost": round(projected_monthly * tariff, 2),
        "tariff": tariff,
        "daysOfData": len(daily_kwh),
    })


# ── API: Analytics — compare ─────────────────────────────────────────────────
@app.route("/api/analytics/compare")
@require_auth
def api_analytics_compare():
    """This week vs last week energy comparison."""
    tariff = _get_tariff()

    this_week = _flux_query(f'''
        from(bucket:"{INFLUX_BUCKET}")
        |> range(start:-7d)
        |> filter(fn:(r) => r._measurement == "energy" and r._field == "totalPower")
        |> aggregateWindow(every:1d, fn:mean, createEmpty:false)
        |> map(fn:(r) => ({{r with _value: r._value * 24.0 / 1000.0}}))
    ''')

    last_week = _flux_query(f'''
        from(bucket:"{INFLUX_BUCKET}")
        |> range(start:-14d, stop:-7d)
        |> filter(fn:(r) => r._measurement == "energy" and r._field == "totalPower")
        |> aggregateWindow(every:1d, fn:mean, createEmpty:false)
        |> map(fn:(r) => ({{r with _value: r._value * 24.0 / 1000.0}}))
    ''')

    tw_total = sum(r.get("_value", 0) for r in this_week)
    lw_total = sum(r.get("_value", 0) for r in last_week)
    change_pct = ((tw_total - lw_total) / lw_total * 100) if lw_total > 0 else 0

    tw_days = []
    for r in this_week:
        t = r.get("_time")
        tw_days.append({
            "day": t.strftime("%a") if hasattr(t, "strftime") else "",
            "kwh": round(r.get("_value", 0), 2)
        })
    lw_days = []
    for r in last_week:
        t = r.get("_time")
        lw_days.append({
            "day": t.strftime("%a") if hasattr(t, "strftime") else "",
            "kwh": round(r.get("_value", 0), 2)
        })

    return jsonify({
        "thisWeek": {"totalKwh": round(tw_total, 2), "totalEarnings": round(tw_total * tariff, 2), "days": tw_days},
        "lastWeek": {"totalKwh": round(lw_total, 2), "totalEarnings": round(lw_total * tariff, 2), "days": lw_days},
        "changePct": round(change_pct, 1),
    })


@app.route("/api/token")
def api_token():
    """Returns access URL. Only accessible from localhost (for screen-manager)."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    return jsonify({
        "token": _AUTH_TOKEN,
        "url": f"http://{socket.gethostname()}.local:{PORT}/?t={_AUTH_TOKEN}",
    })


# ── Startup ───────────────────────────────────────────────────────────────────
_init_influx()

if __name__ == "__main__":
    log.info(f"Dashboard starting on port {PORT}")
    log.info(f"Access: http://localhost:{PORT}/?t={_AUTH_TOKEN}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)