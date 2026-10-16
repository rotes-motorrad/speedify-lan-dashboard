#!/usr/bin/env bash
set -euo pipefail
usage() {
    cat <<'HELP'
Install the local Speedify dashboard and start it at boot.
Usage: sudo bash install.sh --bind PI_LAN_IP [--port 8088] [--user PI_USERNAME]
Uses the existing Python 3 and Speedify installations; no pip packages are needed.
HELP
}
bind_ip=""
port="8088"
run_user="${SUDO_USER:-}"
while (($#)); do
    case "$1" in
        --bind|--port|--user)
            (($# >= 2)) || { usage; exit 2; }
            case "$1" in --bind) bind_ip="$2";; --port) port="$2";; --user) run_user="$2";; esac
            shift 2;;
        -h|--help) usage; exit 0;;
        *) usage; exit 2;;
    esac
done
[[ "${EUID}" == 0 ]] || { printf '%s\n' 'Run the installer with sudo.'; exit 1; }
[[ -n "$bind_ip" ]] || { usage; exit 2; }
[[ -n "$run_user" && "$run_user" != root && "$run_user" =~ ^[a-zA-Z_][a-zA-Z0-9_.-]*\$?$ ]] || {
    printf '%s\n' 'Specify your normal, non-root Pi login with --user USERNAME.'; exit 2;
}
id "$run_user" >/dev/null
command -v python3 >/dev/null
command -v systemctl >/dev/null
[[ -x /usr/share/speedify/speedify_cli ]] || { printf '%s\n' 'Speedify CLI was not found at /usr/share/speedify/speedify_cli.'; exit 1; }
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$source_dir/dashboard.py" && -f "$source_dir/index.html" ]] || { printf '%s\n' 'Extract the whole dashboard folder first.'; exit 1; }
# Validate before writing files, including that the chosen LAN address belongs to this Pi.
python3 - "$bind_ip" "$port" <<'PY'
import ipaddress, socket, sys
if sys.version_info < (3,9):
    raise SystemExit('Python 3.9 or newer is required.')
ip = ipaddress.IPv4Address(sys.argv[1])
nets = ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16']
if not any(ip in ipaddress.ip_network(n) for n in nets):
    raise SystemExit('Use the Pi LAN address, not a public, VPN, loopback, or wildcard address.')
port = int(sys.argv[2])
if not 1024 <= port <= 65535:
    raise SystemExit('Port must be between 1024 and 65535.')
# Check this IP belongs to the Pi; use an ephemeral port so an existing service can be updated.
with socket.socket() as sock:
    sock.bind((str(ip), 0))
PY
install -d -m 0755 /opt/speedify-dashboard
install -m 0644 "$source_dir/dashboard.py" "$source_dir/index.html" /opt/speedify-dashboard/
for extra in README.md test_dashboard.py; do
    if [[ -f "$source_dir/$extra" ]]; then
        install -m 0644 "$source_dir/$extra" /opt/speedify-dashboard/
    fi
done
cat > /etc/systemd/system/speedify-dashboard.service <<SERVICE
[Unit]
Description=Speedify read-only LAN dashboard
Wants=network-online.target
After=network-online.target speedify.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=$run_user
WorkingDirectory=/opt/speedify-dashboard
ExecStart=/usr/bin/python3 /opt/speedify-dashboard/dashboard.py --bind $bind_ip --port $port
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=always
RestartSec=4
TimeoutStopSec=10
KillMode=control-group
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
NoNewPrivileges=true
CapabilityBoundingSet=CAP_NET_RAW
AmbientCapabilities=CAP_NET_RAW

[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
systemctl enable speedify-dashboard.service
systemctl restart speedify-dashboard.service
if ! python3 - "$bind_ip" "$port" "$source_dir/dashboard.py" <<'PY'
import ast, http.client, json, pathlib, sys, time
tree = ast.parse(pathlib.Path(sys.argv[3]).read_text())
expected_version = next(ast.literal_eval(node.value) for node in tree.body
                        if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == 'VERSION'
                                for target in node.targets))
for attempt in range(20):
    connection = http.client.HTTPConnection(sys.argv[1], int(sys.argv[2]), timeout=1)
    try:
        connection.request('GET', '/api/live')
        response = connection.getresponse()
        body = json.loads(response.read())
        if response.status == 200 and body.get('version') == expected_version:
            break
    except (OSError, ValueError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    time.sleep(0.5)
else:
    raise SystemExit(1)
PY
then
    printf '%s\n' 'Dashboard startup did not complete. Check the log below (a port conflict is one possible cause).'
    journalctl -u speedify-dashboard -n 15 --no-pager
    exit 1
fi
if ! command -v ping >/dev/null; then
    printf '%s\n' 'For internet ping readings, also run: sudo apt install iputils-ping'
fi
printf '\nDashboard installed: http://%s:%s\n' "$bind_ip" "$port"
printf '%s\n' 'Open that address from your desktop on the home network.' 'Check startup with: sudo systemctl status speedify-dashboard --no-pager'
