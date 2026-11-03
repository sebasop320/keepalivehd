#!/usr/bin/env bash
# KeepAliveHD installer for Ubuntu Server.   Usage: sudo bash install.sh [options]
#
#   --mountpoint PATH   mountpoint of the external HDD        (default /mnt/hdd)
#   --start HH:MM       keep-awake window start               (default 05:00)
#   --end HH:MM         keep-awake window end                 (default 22:00)
#   --interval SEC      seconds between keepalive reads       (default 120)
#   --port N            dashboard port                        (default 8787)
#   --lan CIDR          add a UFW rule allowing only this network, e.g. 192.168.1.0/24
#
# Safe to re-run: existing config and dashboard password are preserved.
set -euo pipefail

MOUNTPOINT=/mnt/hdd START="" END="" INTERVAL="" PORT="" LAN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mountpoint) MOUNTPOINT=${2:?}; shift 2 ;;
    --start)      START=${2:?}; shift 2 ;;
    --end)        END=${2:?}; shift 2 ;;
    --interval)   INTERVAL=${2:?}; shift 2 ;;
    --port)       PORT=${2:?}; shift 2 ;;
    --lan)        LAN=${2:?}; shift 2 ;;
    -h|--help)    sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root:  sudo bash install.sh"
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
for f in app.py keepalivehd keepalivehd.service web/index.html web/style.css web/app.js; do
  [ -f "$SRC/$f" ] || die "missing $SRC/$f (run from the unzipped keepalivehd-ubuntu folder)"
done
command -v systemctl >/dev/null || die "systemd is required"

INSTALL_DIR=/opt/keepalivehd
CONF_DIR=/etc/keepalivehd
UNIT=/etc/systemd/system/keepalivehd.service
OWN_MARK='Managed-by: keepalivehd-ubuntu'
TS=$(date +%Y%m%d-%H%M%S)

# ---------------------------------------------------------------- 1. dependencies
say "1/7  Checking dependencies"
need=()
command -v python3  >/dev/null || need+=(python3)
command -v smartctl >/dev/null || need+=(smartmontools)
command -v findmnt  >/dev/null || need+=(util-linux)
command -v lsblk    >/dev/null || need+=(util-linux)
if [ ${#need[@]} -gt 0 ]; then
  note "installing: ${need[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${need[@]}"
else
  note "python3, smartctl, findmnt and lsblk are already present"
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' || die "Python 3.8+ required"

# ---------------------------------------------------------------- 2. migrate old version
say "2/7  Looking for a previous KeepAliveHD (systemd timer/service)"
BACKUP=""
old_units=()
while IFS= read -r f; do
  [ -f "$f" ] || continue
  grep -q "$OWN_MARK" "$f" 2>/dev/null && continue
  old_units+=("$f")
done < <(find /etc/systemd/system -maxdepth 1 -type f \( -iname 'keepalive*.timer' -o -iname 'keepalive*.service' \) 2>/dev/null | sort)

if [ ${#old_units[@]} -gt 0 ]; then
  BACKUP=/var/backups/keepalivehd-migration-$TS
  mkdir -p "$BACKUP"
  note "found previous units; backing them up to $BACKUP"
  : > "$BACKUP/manifest.txt"
  # stop and disable timers first, then services
  for pass in timer service; do
    for f in "${old_units[@]}"; do
      u=$(basename "$f")
      case "$u" in *."$pass") ;; *) continue ;; esac
      en=$(systemctl is-enabled "$u" 2>/dev/null || true)
      printf '%s\t%s\n' "$u" "$en" >> "$BACKUP/manifest.txt"
      cp -a "$f" "$BACKUP/$u"
      systemctl stop "$u" 2>/dev/null || true
      systemctl disable "$u" 2>/dev/null || true
      rm -f "$f"
      note "disabled and removed $u (copy kept in backup)"
    done
  done
  systemctl daemon-reload
  note "old scripts were NOT deleted; only their systemd units were disabled"
else
  note "none found"
fi
# cron-based leftovers are never touched, only reported
for u in root "${SUDO_USER:-}"; do
  [ -n "$u" ] || continue
  if crontab -l -u "$u" 2>/dev/null | grep -Eiq 'iflag=direct|keepalive'; then
    note "WARNING: crontab of '$u' contains a keepalive/dd entry. It will keep running in"
    note "         parallel; remove it with:  sudo crontab -e -u $u"
  fi
done
if grep -rIlEi 'iflag=direct' /etc/cron.d /etc/cron.hourly /etc/cron.daily 2>/dev/null | grep -q .; then
  note "WARNING: /etc/cron.* contains a dd iflag=direct job; consider removing it."
fi

# ---------------------------------------------------------------- 3. directories and files
say "3/7  Installing files"
install -d -m 0755 "$INSTALL_DIR" "$INSTALL_DIR/web"
install -d -m 0750 "$CONF_DIR" /var/lib/keepalivehd
install -m 0755 "$SRC/app.py" "$INSTALL_DIR/app.py"
install -m 0644 "$SRC/web/index.html" "$SRC/web/style.css" "$SRC/web/app.js" "$INSTALL_DIR/web/"
install -m 0755 "$SRC/keepalivehd" /usr/local/bin/keepalivehd
install -m 0644 "$SRC/README.md" "$INSTALL_DIR/README.md" 2>/dev/null || true
install -m 0644 "$SRC/keepalivehd.service" "$UNIT"
note "$INSTALL_DIR, /usr/local/bin/keepalivehd, $UNIT"

# group that may use the CLI without sudo
getent group keepalivehd >/dev/null || groupadd --system keepalivehd
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
  usermod -aG keepalivehd "$SUDO_USER"
  note "added '$SUDO_USER' to group keepalivehd (log out/in to use the CLI without sudo)"
fi

# ---------------------------------------------------------------- 4. config
say "4/7  Configuration"
args=(--init-config --mountpoint "$MOUNTPOINT")
[ -n "$START" ]    && args+=(--start "$START")
[ -n "$END" ]      && args+=(--end "$END")
[ -n "$INTERVAL" ] && args+=(--interval "$INTERVAL")
[ -n "$PORT" ]     && args+=(--port "$PORT")
python3 "$INSTALL_DIR/app.py" "${args[@]}" | sed 's/^/    /'
python3 -c "import json;json.load(open('$CONF_DIR/config.json'))" || die "config.json is not valid JSON"
PORT_IN_CFG=$(python3 -c "import json;print(json.load(open('$CONF_DIR/config.json'))['web']['port'])")

# ---------------------------------------------------------------- 5. password
say "5/7  Dashboard credentials"
NEWPASS=$(python3 "$INSTALL_DIR/app.py" --init-auth --username admin)
if [ -n "$NEWPASS" ]; then note "generated a new random password"; else note "existing credentials kept"; fi

# ---------------------------------------------------------------- 6. service
say "6/7  Enabling the systemd service"
systemctl daemon-reload
systemctl enable keepalivehd.service >/dev/null 2>&1
systemctl restart keepalivehd.service
for _ in $(seq 1 20); do
  [ -S /run/keepalivehd/control.sock ] && break
  sleep 0.5
done
if systemctl is-active --quiet keepalivehd.service; then
  note "keepalivehd.service is active and enabled at boot"
else
  journalctl -u keepalivehd -n 20 --no-pager || true
  die "service failed to start (see log above)"
fi

# ---------------------------------------------------------------- 7. firewall
say "7/7  Firewall"
if [ -n "$LAN" ]; then
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q 'Status: active'; then
    ufw allow from "$LAN" to any port "$PORT_IN_CFG" proto tcp comment 'KeepAliveHD dashboard' >/dev/null
    echo "ufw allow from $LAN to any port $PORT_IN_CFG proto tcp" > "$CONF_DIR/ufw_rule.txt"
    note "UFW rule added: only $LAN may reach port $PORT_IN_CFG"
  else
    note "UFW is not active; no rule added. (If you enable it later, see the README.)"
  fi
else
  note "no --lan given; UFW untouched. To restrict access to your LAN run for example:"
  note "  sudo ufw allow from 192.168.1.0/24 to any port $PORT_IN_CFG proto tcp"
  note "The app itself also rejects non-private client addresses (allowed_networks in config.json)."
fi

# ---------------------------------------------------------------- summary
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "------------------------------------------------------------"
echo " KeepAliveHD installed"
echo "------------------------------------------------------------"
echo " Dashboard:  http://${IP:-SERVER-IP}:${PORT_IN_CFG}"
echo " Username:   admin"
if [ -n "$NEWPASS" ]; then
  echo " Password:   $NEWPASS     (shown once - save it now)"
else
  echo " Password:   unchanged (reset with: sudo keepalivehd passwd)"
fi
echo
echo " Try:        keepalivehd status"
echo " Logs:       journalctl -u keepalivehd -f"
if [ -n "$BACKUP" ]; then
  echo " Old units backed up in: $BACKUP   (restore with: sudo bash uninstall.sh --restore-old)"
fi
echo "------------------------------------------------------------"
echo
/usr/local/bin/keepalivehd status || true
