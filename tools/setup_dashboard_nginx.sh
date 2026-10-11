#!/usr/bin/env bash
# Configure Nginx on the Ubuntu controller; run with sudo after installing nginx,
# apache2-utils, gettext-base and openssl. No network addresses or secrets are
# persisted in the checkout. The dashboard must already listen on 127.0.0.1:8088.
set -euo pipefail
umask 077

public_interface=ens3
public_ip=
dashboard_user=${SUDO_USER:-ubuntu}
while (($#)); do
    case "$1" in
        --interface) public_interface=${2:?Missing interface}; shift 2 ;;
        --public-ip) public_ip=${2:?Missing IPv4 address}; shift 2 ;;
        --dashboard-user) dashboard_user=${2:?Missing user}; shift 2 ;;
        *) echo "Usage: sudo bash $0 [--interface ens3] [--public-ip IPv4] [--dashboard-user ubuntu]" >&2; exit 2 ;;
    esac
done
[[ $EUID == 0 ]] || { echo "Run this script with sudo." >&2; exit 1; }
for program in python3 ip ss nginx htpasswd envsubst openssl curl getent install mktemp; do
    command -v "$program" >/dev/null || { echo "Missing prerequisite: $program" >&2; exit 1; }
done
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
template=$script_dir/../gns3/controller/ubuntu-24.04/nginx/ninja-dashboard.conf.template
[[ -f $template ]] || { echo "Nginx template is missing." >&2; exit 1; }

# Reject config injection and addresses that are not actually assigned to the NIC.
public_ip=$(python3 - "$public_interface" "$public_ip" <<'PY'
import ipaddress, json, subprocess, sys
interface, selected = sys.argv[1:]
data = json.loads(subprocess.check_output(['ip', '-4', '-j', 'address', 'show', 'dev', interface]))
addresses = [item['local'] for link in data for item in link.get('addr_info', [])
             if item.get('family') == 'inet' and ipaddress.ip_address(item['local']).is_global]
if selected:
    value = ipaddress.IPv4Address(selected)
    if str(value) not in addresses:
        raise SystemExit('Requested public IPv4 is not assigned to the chosen interface')
else:
    if len(addresses) != 1:
        raise SystemExit('Expected exactly one public IPv4; specify --public-ip')
    value = ipaddress.IPv4Address(addresses[0])
print(value)
PY
)
user_record=$(getent passwd "$dashboard_user")
IFS=: read -r _ _ dashboard_uid dashboard_gid _ dashboard_home _ <<< "$user_record"
[[ $dashboard_uid != 0 && $dashboard_home == /* ]] || { echo "Choose a non-root dashboard user." >&2; exit 1; }
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8088/healthz | python3 -c '
import json, sys
data = json.load(sys.stdin)
if data.get("status") != "ok" or data.get("read_only") is not True:
    raise SystemExit("Expected the read-only dashboard on loopback port 8088")
'
ss -H -ltn 'sport = :8088' | python3 -c '
import sys
listeners = [line.split()[3] for line in sys.stdin if line.strip()]
if not listeners or any(address not in {"127.0.0.1:8088", "[::1]:8088"} for address in listeners):
    raise SystemExit("Port 8088 must listen only on loopback before enabling the proxy")
'

cert_dir=/etc/nginx/ninja-dashboard
install -d -o root -g www-data -m 0750 "$cert_dir"
install -d -o root -g root -m 0755 /var/lib/ninja-dashboard/acme
if [[ ! -e $cert_dir/fullchain.pem && ! -e $cert_dir/privkey.pem ]]; then
    openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 30 \
        -subj /CN=Ninja-PCE-lab -addext "subjectAltName=IP:$public_ip" \
        -addext 'basicConstraints=critical,CA:FALSE' \
        -addext 'keyUsage=critical,digitalSignature,keyEncipherment' \
        -addext 'extendedKeyUsage=serverAuth' \
        -keyout "$cert_dir/privkey.pem" -out "$cert_dir/fullchain.pem" 2>/dev/null
fi
openssl x509 -in "$cert_dir/fullchain.pem" -noout -checkend 0 >/dev/null
openssl x509 -in "$cert_dir/fullchain.pem" -noout -checkip "$public_ip" >/dev/null
chmod 0600 "$cert_dir/privkey.pem"
chmod 0644 "$cert_dir/fullchain.pem"

password_dir=$dashboard_home/.config/ninja-dashboard
if [[ ! -e $cert_dir/htpasswd ]]; then
    install -d -o "$dashboard_uid" -g "$dashboard_gid" -m 0700 "$password_dir"
    password_file=$password_dir/initial-password
    if [[ -e $password_file ]]; then
        echo "An initial-password file already exists; configure htpasswd explicitly to preserve it." >&2
        exit 1
    fi
    (umask 077; openssl rand -base64 24 > "$password_file")
    chown "$dashboard_uid:$dashboard_gid" "$password_file"
    htpasswd -iBc "$cert_dir/htpasswd" dashboard < "$password_file" >/dev/null
fi
chown root:www-data "$cert_dir/htpasswd"
chmod 0640 "$cert_dir/htpasswd"

site=/etc/nginx/sites-available/ninja-dashboard
enabled=/etc/nginx/sites-enabled/ninja-dashboard
backup=$(mktemp -d "$cert_dir/backup.XXXXXXXX")
chmod 0700 "$backup"
had_site=false
had_enabled=false
if [[ -e $site || -L $site ]]; then cp -a "$site" "$backup/site"; had_site=true; fi
if [[ -e $enabled || -L $enabled ]]; then cp -a "$enabled" "$backup/enabled"; had_enabled=true; fi
restore() {
    rm -f -- "$enabled" "$site"
    if $had_site; then cp -a "$backup/site" "$site"; fi
    if $had_enabled; then cp -a "$backup/enabled" "$enabled"; fi
}
export PUBLIC_IP=$public_ip
envsubst '${PUBLIC_IP}' < "$template" > "$backup/rendered"
install -o root -g root -m 0644 "$backup/rendered" "$site"
ln -sfn "$site" "$enabled"
if ! nginx -t; then restore; echo "Nginx validation failed; previous site restored." >&2; exit 1; fi
if systemctl is-active --quiet nginx; then
    if ! systemctl reload nginx; then restore; nginx -t && systemctl reload nginx; exit 1; fi
else
    if ! systemctl enable --now nginx; then restore; exit 1; fi
fi
# A successful reload command only sends a signal. Detect bind failures that
# would otherwise leave Nginx serving its previous configuration.
ready=false
for attempt in {1..10}; do
    status=$(curl --silent --max-time 3 --cacert "$cert_dir/fullchain.pem" \
        -o /dev/null -w '%{http_code}' "https://$public_ip/healthz" || true)
    if [[ $status == 401 ]]; then ready=true; break; fi
    sleep 0.3
done
if ! $ready; then
    restore
    nginx -t && systemctl reload nginx
    echo "HTTPS authentication check failed; previous site restored." >&2
    echo "Replacing an existing wildcard listener can require a one-time Nginx restart." >&2
    exit 1
fi
printf 'Dashboard: https://%s/\nUsername: dashboard\n' "$public_ip"
printf 'Read the initial password on the Ubuntu console: cat %q\n' "$password_dir/initial-password"
echo 'Verify and trust the lab certificate before entering credentials. Certificate SHA-256:'
openssl x509 -in "$cert_dir/fullchain.pem" -noout -fingerprint -sha256
printf 'Site backup: %s\n' "$backup"
