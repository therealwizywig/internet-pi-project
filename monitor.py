#!/usr/bin/env python3
import os
import sys
import csv
import json
import subprocess
import re
from datetime import datetime, timezone
import requests

# --- CONFIGURATION PATHS ---
CSV_FILE = os.path.expanduser("~/network_stats/speedtest_results.csv")
REPO_DIR = os.path.expanduser("~/network_stats/repo")
ENV_FILE = os.path.expanduser("~/network_stats/device_env.conf")

INGEST_URL = "https://telemetry-ingest-32461014139.us-central1.run.app/telemetry"
INGEST_HEADERS = {
    "Content-Type": "application/json",
    "X-Ingest-Key": "UDE_rex!qhp*eby6kry"
}
ZEROTIER_NETWORK_ID = "633e31d8a24687c7"

# Updatable target map
TARGET_SERVERS = {
    "google": "google.com",
    "github": "github.com",
    "apple": "apple.com",
    "microsoft": "download.windowsupdate.com"
}
NETSUITE_URL = "https://signon.okta.com/app/netsuite/exk1jbjbbur8wkLJT0h8/sso/saml/metadata"
STATIC_SPEEDTEST_SERVER = None

# --- DYNAMIC CONFIGURATION LOADER ---
def load_device_id():
    """Reads the custom device ID string out of the environment config file."""
    default_fallback = "test-device-default"
    if not os.path.exists(ENV_FILE):
        return default_fallback
    try:
        with open(ENV_FILE, 'r') as f:
            for line in f:
                if line.strip().startswith("DEVICE_ID="):
                    # Extract string cleanly inside quotes
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
        return 1 if response.status_code == 200 else 0
    except requests.exceptions.RequestException:
        return 0

def run_speedtest():
    cmd = ["speedtest-cli", "--simple"]
    if STATIC_SPEEDTEST_SERVER:
        cmd.extend(["--server", str(STATIC_SPEEDTEST_SERVER)])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            return 0.0, 0.0
        down = re.search(r"Download:\s+([\d\.]+)", res.stdout)
        up = re.search(r"Upload:\s+([\d\.]+)", res.stdout)
        return float(down.group(1)) if down else 0.0, float(up.group(1)) if up else 0.0
    except Exception:
        return 0.0, 0.0

def get_zerotier_telemetry():
    zt_data = {"zt_node_id": "unknown", "zt_status": "OFFLINE", "zt_assigned_ip": "none"}
    try:
        info_res = subprocess.run(["sudo", "zerotier-cli", "info"], capture_output=True, text=True, timeout=5)
        if info_res.returncode == 0:
            match = re.search(r"info (\w+)", info_res.stdout)
            if match: zt_data["zt_node_id"] = match.group(1)

        net_res = subprocess.run(["sudo", "zerotier-cli", "listnetworks"], capture_output=True, text=True, timeout=5)
        if net_res.returncode == 0:
            for line in net_res.stdout.splitlines():
                if ZEROTIER_NETWORK_ID in line:
                    parts = line.split()
                    if len(parts) >= 4: zt_data["zt_status"] = parts[3]
                    if len(parts) >= 7 and parts[6] != "-":
                        zt_data["zt_assigned_ip"] = parts[6].split('/')[0]
                    break
    except Exception:
        pass
    return zt_data

# --- MAIN EXECUTION ---
def main():
    update_from_git()
    
    # Dynamically read current Device ID setting
    device_id = load_device_id()
    
    timestamp = get_utc_timestamp()
    uptime_sec = get_uptime_seconds()
    netsuite_status = probe_netsuite()
    zt_metrics = get_zerotier_telemetry()
    
    payload = {
        "device_id": device_id,
        "ping_timestamp": timestamp,
        "pi_uptime": uptime_sec,
        "netsuite_status": netsuite_status,
        "zerotier_node_id": zt_metrics["zt_node_id"],
        "zerotier_status": zt_metrics["zt_status"],
        "zerotier_ip": zt_metrics["zt_assigned_ip"]
    }
    
    is_offline = True
    for key, host in TARGET_SERVERS.items():
        latency, loss = parse_ping(host)
        payload[f"ping_latency_{key}"] = latency
        payload[f"ping_loss_{key}"] = loss
        if loss < 100.0:
            is_offline = False

    download, upload = (0.0, 0.0) if is_offline else run_speedtest()
    payload["download_mbps"] = download
    payload["upload_mbps"] = upload
    payload["wan_status"] = "Online" if not is_offline else "Offline"

    # --- WEAR-LEVELING IN-RAM STORAGE RETRY LOGIC ---
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
            print(f"Failed writing to local safety buffer: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
