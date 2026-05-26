## 🚀 Quick Start: Install on Raspberry Pi
# internet-pi-project
To track uptime of the device along with the speed and latency of the internet

To install Internet Pi on a fresh Raspberry Pi, just run the following commands in your terminal:
Make the script executable and run it:

```bash
# Clone the repository
sudo apt-get update && sudo apt-get install -y git
# You can use any directory you like, e.g. $HOME/internet-pi
cd $HOME

git clone https://github.com/therealwizywig/network_stats

chmod +x ~/network_stats/setup_monitor.sh  ~/network_stats/monitor.py

sudo ~/network_stats/setup_monitor.sh
```
Now enter the DeviceID assigned from Global

Note:
This script requests your custom Device ID, generates your configuration environment file, and installs all core dependencies:

