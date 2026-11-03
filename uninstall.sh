#!/usr/bin/env bash
# KeepAliveHD uninstaller.   Usage: sudo bash uninstall.sh [--purge] [--restore-old]
#
#   (no flags)      stop and remove the service, program files and CLI;
#                   KEEP /etc/keepalivehd (config + password) and /var/lib/keepalivehd
#   --purge         also delete config, password and state
#   --restore-old   re-install the systemd units that install.sh backed up
#                   from a previous KeepAliveHD version, and re-enable them
#
# Never touches /mnt/hdd, Jellyfin, Immich or Docker.
set -euo pipefail

PURGE=0 RESTORE=0
for a in "$@"; do
  case "$a" in
    --purge) PURGE=1 ;;
    --restore-old) RESTORE=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "Unknown option: $a" >&2; exit 1 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo bash uninstall.sh" >&2; exit 1; }
say() { printf '==> %s\n' "$*"; }

say "Stopping and disabling keepalivehd.service"
systemctl stop keepalivehd.service 2>/dev/null || true
systemctl disable keepalivehd.service 2>/dev/null || true
# only delete the unit if it is ours
if grep -q 'Managed-by: keepalivehd-ubuntu' /etc/systemd/system/keepalivehd.service 2>/dev/null; then
  rm -f /etc/systemd/system/keepalivehd.service
fi
systemctl daemon-reload

say "Removing program files"
rm -rf /opt/keepalivehd
rm -f /usr/local/bin/keepalivehd
rm -rf /run/keepalivehd

if [ -f /etc/keepalivehd/ufw_rule.txt ]; then
  rule=$(sed 's/^ufw //' /etc/keepalivehd/ufw_rule.txt)
  say "Removing UFW rule: $rule"
  # shellcheck disable=SC2086
  ufw delete $rule 2>/dev/null || echo "    could not delete it automatically; check 'sudo ufw status numbered'"
fi

if [ "$PURGE" -eq 1 ]; then
  say "Purging config, password and state"
  rm -rf /etc/keepalivehd /var/lib/keepalivehd
  getent group keepalivehd >/dev/null && groupdel keepalivehd 2>/dev/null || true
else
  say "Kept /etc/keepalivehd and /var/lib/keepalivehd (use --purge to remove)"
fi

if [ "$RESTORE" -eq 1 ]; then
  b=$(ls -1d /var/backups/keepalivehd-migration-* 2>/dev/null | sort | tail -n1 || true)
  if [ -z "$b" ]; then
    echo "No migration backup found in /var/backups; nothing to restore."
  else
    say "Restoring previous units from $b"
    while IFS=$'\t' read -r unit enabled; do
      [ -n "$unit" ] || continue
      cp -a "$b/$unit" "/etc/systemd/system/$unit"
    done < "$b/manifest.txt"
    systemctl daemon-reload
    while IFS=$'\t' read -r unit enabled; do
      [ -n "$unit" ] || continue
      if [ "$enabled" = "enabled" ]; then
        systemctl enable --now "$unit" || echo "    could not start $unit"
      fi
    done < "$b/manifest.txt"
    echo "Previous KeepAliveHD units restored."
  fi
fi
echo "Done."
