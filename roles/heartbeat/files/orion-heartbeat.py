#!/usr/bin/env python3
"""Orion fleet heartbeat — pushes line protocol to central InfluxDB."""
import os, time, socket, subprocess, urllib.request, urllib.error, logging
from pathlib import Path

# ── Config from env (set by systemd unit) ─────────────────────────────────────
INFLUX_URL    = os.environ.get("INFLUX_URL",    "http://localhost:8086")
INFLUX_ORG    = os.environ.get("INFLUX_ORG",    "orion")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "fleet")
INFLUX_TOKEN_FILE = os.environ.get("INFLUX_TOKEN_FILE", "/home/orangepi/.influxdb_token")
INTERVAL_S    = int(os.environ.get("INTERVAL_S", "60"))

def _load_token():
    try:
        return Path(INFLUX_TOKEN_FILE).read_text().strip()
    except Exception as e:
        logging.error(f"Cannot read token: {e}")
        return ""

INFLUX_TOKEN = _load_token()

LOCAL_INFLUX = "http://localhost:8086"
UPDATE_LOG   = "/var/log/orion-update.log"
FAILURE_FILE = "/var/lib/orion/auto-update-failures"
APP_DIR      = "/home/orangepi/screen-manager"

log = logging.getLogger("heartbeat")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ── Collectors ────────────────────────────────────────────────────────────────
def _read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except Exception:
        return default

def cpu_temp_c():
    try:
        return int(_read("/sys/class/thermal/thermal_zone0/temp", "0")) / 1000.0
    except Exception:
        return 0.0

def uptime_s():
    try:
        return float(_read("/proc/uptime").split()[0])
    except Exception:
        return 0.0

def mem_free_mb():
    try:
        for line in _read("/proc/meminfo").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0

def disk_free_mb(path="/"):
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) // (1024 * 1024)
    except Exception:
        return 0

def load_1m():
    try:
        return float(_read("/proc/loadavg").split()[0])
    except Exception:
        return 0.0

def service_active(name):
    r = subprocess.run(["systemctl", "is-active", name],
                       capture_output=True, text=True, timeout=3)
    return 1 if r.stdout.strip() == "active" else 0

def tailscale_ip():
    try:
        r = subprocess.run(["tailscale", "ip", "-4"],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip().splitlines()[0] if r.returncode == 0 else ""
    except Exception:
        return ""

def git_hash():
    try:
        r = subprocess.run(["git", "-C", APP_DIR, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"

def last_update_info():
    """Parse update log for most recent SUCCESS/FAILED only."""
    try:
        lines = Path(UPDATE_LOG).read_text().splitlines()
        for line in reversed(lines):
            if "SUCCESS" in line:
                return "ok"
            if "FAILED" in line or "CRITICAL" in line:
                return "fail"
    except Exception:
        pass
    return "none"

def update_failures_24h():
    try:
        if not Path(FAILURE_FILE).exists():
            return 0
        now = int(time.time())
        cutoff = now - 86400
        count = 0
        for line in Path(FAILURE_FILE).read_text().splitlines():
            try:
                ts = int(line.split()[0])
                if ts >= cutoff:
                    count += 1
            except (ValueError, IndexError):
                continue
        return count
    except Exception:
        return 0

def esp32_last_seen_s():
    """Query local InfluxDB: seconds since last energy metric."""
    try:
        q = ('from(bucket:"energy_metrics") |> range(start:-1h) '
             '|> filter(fn:(r) => r._measurement == "energy" '
             'and r._field == "voltage") '
             '|> last()')
        token = _read("/home/orangepi/.influxdb_token")
        if not token:
            return -1
        req = urllib.request.Request(
            f"{LOCAL_INFLUX}/api/v2/query?org=orion",
            data=q.encode(),
            headers={"Authorization": f"Token {token}",
                     "Content-Type": "application/vnd.flux"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            body = r.read().decode()
            from datetime import datetime
            for line in body.splitlines():
                if line.startswith(",") and "T" in line:
                    parts = line.split(",")
                    # _time is column index 5 in default Flux CSV format
                    if len(parts) > 5 and "T" in parts[5]:
                        ts = datetime.fromisoformat(parts[5].replace("Z", "+00:00"))
                        return int(time.time() - ts.timestamp())
    except Exception as e:
        log.debug(f"ESP32 lookup failed: {e}")
    return -1


# ── Line protocol formatter ───────────────────────────────────────────────────
def build_line():
    host = socket.gethostname()
    upd_status = last_update_info()

    fields_num = {
        "uptime_s":            uptime_s(),
        "cpu_temp_c":          cpu_temp_c(),
        "disk_free_mb":        disk_free_mb(),
        "mem_free_mb":         mem_free_mb(),
        "load_1m":             load_1m(),
        "screen_active":       service_active("screen.service"),
        "tailscale_active":    service_active("tailscaled"),
        "update_failures_24h": update_failures_24h(),
        "esp32_last_seen_s":   esp32_last_seen_s(),
    }
    fields_str = {
        "git_hash":    git_hash(),
        "last_update": upd_status,
    }
    tags = {
        "host":         host,
        "tailscale_ip": tailscale_ip(),
    }

    tag_str = ",".join(f"{k}={v}" for k, v in tags.items() if v)
    num_str = ",".join(f"{k}={v}" for k, v in fields_num.items())
    str_str = ",".join(f'{k}="{v}"' for k, v in fields_str.items())
    field_str = num_str + ("," + str_str if str_str else "")
    return f"orion_health,{tag_str} {field_str}"


# ── Push ──────────────────────────────────────────────────────────────────────
def push():
    if not INFLUX_URL or not INFLUX_TOKEN:
        log.warning("INFLUX_URL/TOKEN not set — logging only")
        log.info(build_line())
        return
    line = build_line()
    url = f"{INFLUX_URL}/api/v2/write?org={INFLUX_ORG}&bucket={INFLUX_BUCKET}&precision=s"
    req = urllib.request.Request(
        url, data=line.encode(),
        headers={"Authorization": f"Token {INFLUX_TOKEN}",
                 "Content-Type": "text/plain"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status >= 300:
                log.error(f"Push failed: {r.status}")
    except urllib.error.URLError as e:
        log.error(f"Push network error: {e}")
    except Exception as e:
        log.error(f"Push error: {e}")


# ── Main loop ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"Heartbeat starting — interval={INTERVAL_S}s, target={INFLUX_URL or 'log-only'}")
    while True:
        try:
            push()
        except Exception as e:
            log.error(f"Loop error: {e}")
        time.sleep(INTERVAL_S)