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
            data.get("netsuite_url", ""),
            data.get("speedtest_servers", [{"label": "auto"}]),
        )
    except Exception as e:
        print(f"Warning: could not load {TARGETS_FILE}: {e}", file=sys.stderr)
        return {}, "", [{"label": "auto"}]

TARGET_SERVERS, NETSUITE_URL, SPEEDTEST_SERVERS = load_targets()

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

def probe_netsuite():
    try:
        response = requests.get(NETSUITE_URL, timeout=5)
        if response.status_code >= 400:
            return "error"
        if response.history:
            return "redirected"
        return "reached"
    except requests.exceptions.SSLError:
        return "ssl_error"
    except requests.exceptions.ConnectionError as e:
        if "NameResolutionError" in str(e) or "Name or service not known" in str(e):
            return "dns_failed"
        return "failed"
    except requests.exceptions.Timeout:
        return "timeout"
    except requests.exceptions.RequestException:
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
    
    payload = {
        "device_id": device_id,
        "ping_timestamp": timestamp,
        "pi_uptime": uptime_sec,
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

    if show_progress: update_progress(60, "Probing NetSuite Status...")
    payload["netsuite_status"] = probe_netsuite()

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