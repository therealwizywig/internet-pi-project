#!/usr/bin/env python3
import os
import sys
import csv
import json
import subprocess
import re
import time
from datetime import datetime, timezone
import requests

# --- CONFIGURATION PATHS ---
CSV_FILE = os.path.expanduser("~/network_stats/speedtest_results.csv")
REPO_DIR = os.path.expanduser("~/network_stats/repo")
ENV_FILE = os.path.expanduser("~/network_stats/device_env.conf")
TARGETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets.json")

INGEST_URL = "https://telemetry-ingest-32461014139.us-central1.run.app/telemetry"
INGEST_HEADERS = {
    "Content-Type": "application/json",
    "X-Ingest-Key": "UDE_rex!qhp*eby6kry"
}

def load_targets():
    try:
        with open(TARGETS_FILE, 'r') as f:
            data = json.load(f)
        return (
            data.get("target_servers", {}),
            data.get("probe_urls", {}),
            data.get("speedtest_servers", [{"label": "auto"}]),
        )
    except Exception as e:
        print(f"Warning: could not load {TARGETS_FILE}: {e}", file=sys.stderr)
        return {}, {}, [{"label": "auto"}]

TARGET_SERVERS, PROBE_URLS, SPEEDTEST_SERVERS = load_targets()

# --- PROGRESS BAR UTILITY ---
def update_progress(percent, status_text=""):
    """Renders a clean, live terminal progress bar."""
    bar_length = 30
    filled_length = int(round(bar_length * percent / 100.0))
    bar = '█' * filled_length + '-' * (bar_length - filled_length)
    
    status_text = status_text.ljust(35)
    sys.stdout.write(f"\r[{bar}] {percent}% | {status_text}")
    sys.stdout.flush()

# --- DYNAMIC CONFIGURATION LOADER ---
def load_device_id():
    default_fallback = "test-device-default"
    if not os.path.exists(ENV_FILE):
        return default_fallback
    try:
        with open(ENV_FILE, 'r') as f:
            for line in f:
                if line.strip().startswith("DEVICE_ID="):
                    match = re.search(r'DEVICE_ID=["\']?([^"\']+)["\']?', line)
                    if match:
                        return match.group(1)
    except Exception:
        pass
    return default_fallback

# --- CORE UTILITIES ---
def get_utc_timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def get_uptime_seconds():
    try:
        with open('/proc/uptime', 'r') as f:
            return int(float(f.readline().split()[0]))
    except Exception:
        return 0

def update_from_git():
    if os.path.exists(os.path.join(REPO_DIR, ".git")):
        try:
            subprocess.run(["git", "-C", REPO_DIR, "pull"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

def get_connection_info():
    try:
        res = subprocess.run(["ip", "route", "get", "8.8.8.8"], capture_output=True, text=True)
        match = re.search(r"dev\s+(\S+)", res.stdout)
        if not match:
            return "unknown", "unknown"
        iface = match.group(1)

        if iface.startswith("wlan") or iface.startswith("wl"):
            conn_type = "wifi"
            iw = subprocess.run(["iw", "dev", iface, "link"], capture_output=True, text=True)
            out = iw.stdout
            if "EHT" in out:
                speed = "WiFi 7"
            elif "HE" in out:
                freq_match = re.search(r"freq:\s+(\d+)", out)
                speed = "WiFi 6E" if freq_match and int(freq_match.group(1)) >= 5925 else "WiFi 6"
            elif "VHT" in out:
                speed = "WiFi 5"
            elif "HT" in out:
                speed = "WiFi 4"
            else:
                speed = "WiFi"
        elif iface.startswith("eth") or iface.startswith("en"):
            conn_type = "ethernet"
            with open(f"/sys/class/net/{iface}/speed") as f:
                mbps = int(f.read().strip())
            speed_map = {10: "10 Mbps", 100: "100 Mbps", 1000: "Gigabit",
                         2500: "2.5 Gigabit", 5000: "5 Gigabit", 10000: "10 Gigabit",
                         25000: "25 Gigabit", 40000: "40 Gigabit", 100000: "100 Gigabit"}
            speed = speed_map.get(mbps, f"{mbps} Mbps")
        else:
            conn_type = iface
            speed = "unknown"

        return conn_type, speed
    except Exception:
        return "unknown", "unknown"

def get_external_ip_info():
    try:
        data = requests.get("https://ipinfo.io/json", timeout=5).json()
        return data.get("ip", ""), data.get("city", "")
    except Exception:
        return "", ""

def parse_ping(host):
    try:
        res = subprocess.run(["ping", "-c", "5", "-W", "5", host], capture_output=True, text=True)
        if res.returncode != 0:
            return 0.0, 100.0
        loss_match = re.search(r"(\d+)% packet loss", res.stdout)
        loss = float(loss_match.group(1)) if loss_match else 100.0
        rtt_match = re.search(r"rtt min/avg/max/mdev = [\d\.]+/([\d\.]+)/", res.stdout)
        avg_latency = float(rtt_match.group(1)) if rtt_match else 0.0
        return avg_latency, loss
    except Exception:
        return 0.0, 100.0

def probe_netsuite(url):
    try:
        res = subprocess.run(
            ["python3", "-c", f"import requests; print(requests.get('{url}').status_code)"],
            capture_output=True, text=True, timeout=10
        )
        if res.returncode != 0:
            stderr = res.stderr
            if "SSLError" in stderr:
                return "ssl_error"
            if "NameResolutionError" in stderr or "Name or service not known" in stderr:
                return "dns_failed"
            if "Timeout" in stderr or "timed out" in stderr:
                return "timeout"
            return "failed"
        status_code = int(res.stdout.strip())
        if status_code >= 400:
            return "error"
        return "reached"
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception:
        return "failed"

def run_speedtest(server_id=None):
    cmd = ["speedtest-cli", "--json"]
    if server_id:
        cmd.extend(["--server", str(server_id)])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            return 0.0, 0.0, ""
        data = json.loads(res.stdout)
        download = round(data.get("download", 0) / 1_000_000, 2)
        upload = round(data.get("upload", 0) / 1_000_000, 2)
        server = data.get("server", {})
        server_name = f"{server.get('sponsor', '')} ({server.get('name', '')})".strip(" ()")
        return download, upload, server_name
    except Exception:
        return 0.0, 0.0, ""

# --- MAIN EXECUTION ---
def main():
    show_progress = sys.stdout.isatty()

    if show_progress: update_progress(5, "Checking for Git updates...")
    update_from_git()
    
    if show_progress: update_progress(15, "Loading configuration...")
    device_id = load_device_id()
    timestamp = get_utc_timestamp()
    uptime_sec = get_uptime_seconds()

    if show_progress: update_progress(20, "Getting external IP...")
    external_ip, external_city = get_external_ip_info()
    connection_type, connection_speed = get_connection_info()

    payload = {
        "device_id": device_id,
        "ping_timestamp": timestamp,
        "pi_uptime": uptime_sec,
        "external_ip": external_ip,
        "external_city": external_city,
        "connection_type": connection_type,
        "connection_speed": connection_speed,
    }
    
    is_offline = True
    total_servers = len(TARGET_SERVERS)
    
    for idx, (key, host) in enumerate(TARGET_SERVERS.items(), 1):
        if show_progress: 
            current_pct = int(15 + (40 * (idx / total_servers)))
            update_progress(current_pct, f"Pinging {host}...")
            
        latency, loss = parse_ping(host)
        payload[f"ping_latency_{key}"] = latency
        payload[f"ping_loss_{key}"] = loss
        if loss < 100.0:
            is_offline = False

    total_probes = len(PROBE_URLS)
    for idx, (key, url) in enumerate(PROBE_URLS.items(), 1):
        if show_progress:
            current_pct = int(55 + (10 * (idx / total_probes)))
            update_progress(current_pct, f"Probing {key}...")
        payload[f"probe_{key}"] = probe_netsuite(url)

    if is_offline:
        for st in SPEEDTEST_SERVERS:
            label = st["label"]
            payload[f"download_mbps_{label}"] = 0.0
            payload[f"upload_mbps_{label}"] = 0.0
            payload[f"speedtest_server_{label}"] = ""
        if show_progress: update_progress(90, "Network offline. Skipping Speedtest.")
    else:
        total_st = len(SPEEDTEST_SERVERS)
        for idx, st in enumerate(SPEEDTEST_SERVERS, 1):
            label = st["label"]
            server_id = st.get("id")
            pct = int(70 + (20 * (idx / total_st)))
            status = f"Speedtest ({label})..."
            if show_progress: update_progress(pct, status)
            download, upload, server_name = run_speedtest(server_id)
            payload[f"download_mbps_{label}"] = download
            payload[f"upload_mbps_{label}"] = upload
            payload[f"speedtest_server_{label}"] = server_name

    payload["wan_status"] = "Online" if not is_offline else "Offline"

    if show_progress: update_progress(95, "Uploading telemetry...")
    
    upload_success = False
    try:
        response = requests.post(INGEST_URL, json=payload, headers=INGEST_HEADERS, timeout=10)
        if response.status_code in [200, 201, 202]:
            upload_success = True
    except Exception:
        pass

    if not upload_success:
        file_exists = os.path.isfile(CSV_FILE)
        try:
            with open(CSV_FILE, mode='a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=payload.keys())
                if not file_exists:
                    writer.writeheader()
                writer.writerow(payload)
        except Exception as e:
            print(f"\nFailed writing to local safety buffer: {e}", file=sys.stderr)

    if show_progress: 
        update_progress(100, "Done!")
        print() 

if __name__ == "__main__":
    main()