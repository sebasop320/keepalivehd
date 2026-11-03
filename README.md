# KeepAliveHD for Ubuntu Server

Keeps an external USB HDD (Seagate ST2000DM001 in a UGREEN US222 / Realtek 0bda:9201) awake
for low-latency Jellyfin and Immich access during the hours you choose, and lets it sleep the
rest of the time.

One Python service does everything: scheduler, drive detection, keepalive reads, SMART
monitoring, web dashboard and CLI backend. Standard library only. No Docker, no database.

## How it works

- Every `interval_seconds` (default 120) during active hours it does **one 4 KiB `O_DIRECT`
  read** from the physical disk behind `/mnt/hdd`. Read-only: nothing is ever written to the HDD.
- The disk is found through the mountpoint (`findmnt` -> `lsblk`), never from a hard-coded
  `/dev/sda`. If the mount is missing, the device vanished, or the disk is behind a different USB
  bridge than `expected_usb_id`, **no read is performed**. When the drive comes back, reads
  resume automatically.
- Mode priority: **ON** (always keep awake) / **OFF** (never) override the schedule until you
  choose **AUTO**. AUTO follows the schedule (default 05:00-22:00, server local time, midnight
  crossing supported; start == end means always active).
- Outside active hours the service does not touch the drive at all: no reads, and no SMART
  queries unless you set `smart.poll_when_inactive` or press "Check now"/`status --probe`.
- SMART (temperature/health/standby) is polled every 5 minutes while active, using
  `smartctl -n standby`, which refuses to wake a sleeping drive.

## Install

```bash
unzip keepalivehd-ubuntu.zip
cd keepalivehd-ubuntu
sudo bash install.sh --lan 192.168.1.0/24      # --lan is optional, see "Firewall"
```

Other options: `--mountpoint /mnt/hdd --start 05:00 --end 22:00 --interval 120 --port 8787`.

What the installer does, step by step:

1. **Dependencies**: installs `smartmontools` only if `smartctl` is missing (python3, findmnt,
   lsblk are already on Ubuntu Server).
2. **Migration**: finds old `keepalive*.timer/.service` units in `/etc/systemd/system`, copies
   them to `/var/backups/keepalivehd-migration-<timestamp>/`, stops and disables them (so two
   things don't read the disk), and removes only those unit files. Your old scripts are not
   deleted. Leftover cron jobs are reported, not touched.
3. **Files**: `/opt/keepalivehd/`, `/usr/local/bin/keepalivehd`,
   `/etc/systemd/system/keepalivehd.service`; creates group `keepalivehd` and adds you to it.
4. **Config**: writes `/etc/keepalivehd/config.json` if absent (existing one is kept),
   auto-detecting the USB bridge id of the disk mounted at `/mnt/hdd`.
5. **Password**: generates a random password for user `admin` (stored only as a PBKDF2 hash in
   `/etc/keepalivehd/auth.json`, mode 600). It is printed once at the end.
6. **Service**: `systemctl enable` + restart; waits for it to come up.
7. **Firewall**: with `--lan CIDR` and UFW active, adds one allow rule. Otherwise untouched.

It prints the dashboard URL and credentials. It never touches `/mnt/hdd`, Jellyfin, Immich or Docker.

## Use

```bash
keepalivehd status            # mode, disk, mount, temperature, standby state, last keepalive
keepalivehd on                # keep awake now (overrides schedule)
keepalivehd off               # stop now (overrides schedule)
keepalivehd auto              # follow the schedule again
keepalivehd schedule          # show schedule
keepalivehd schedule set 06:00 23:30 --interval 90
keepalivehd logs -n 50        # recent events  (--errors, --all)
keepalivehd status --probe    # force a SMART query now (can wake the drive)
sudo keepalivehd passwd       # new random dashboard password (--set to type your own)
```

Without sudo you need to be in group `keepalivehd` (log out/in once after install).

Dashboard: `http://SERVER-IP:8787` - keepalive state, ON/AUTO/OFF buttons, drive connection and
mount, temperature, HDD state, last keepalive, schedule editor, log viewer; refreshes every 5 s
(paused when the tab is hidden).

## Test it

```bash
systemctl status keepalivehd                  # active (running)
keepalivehd status
journalctl -u keepalivehd -f                  # watch "Keepalive read OK" every 2 minutes
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8787/api/status   # 401 = auth works
```

Check that stopping really stops the reads and lets the drive sleep:

```bash
keepalivehd off
# wait longer than the enclosure's spin-down time (try 15-30 min), then:
sudo smartctl -n standby -d sat -i /dev/sda   # "Device is in STANDBY mode" = it slept
keepalivehd auto
```

Check disk resolution without waiting: `sudo python3 /opt/keepalivehd/app.py --detect`.

## Configuration

`/etc/keepalivehd/config.json` (changes to schedule/interval via CLI or dashboard are written
here). After editing by hand: `sudo systemctl reload keepalivehd` (listen address/port need
`restart`).

| Key | Default | Meaning |
|---|---|---|
| `mountpoint` | `/mnt/hdd` | disk is located through this mount |
| `expected_usb_id` | detected at install | only touch a disk behind this USB bridge; `""` disables |
| `interval_seconds` | 120 | keepalive period (10-3600) |
| `schedule.start/end` | 05:00 / 22:00 | active window |
| `read_timeout_seconds` | 45 | allows for spin-up (~10 s) |
| `rotate_offsets` | false | cycle read position (use if one repeated sector doesn't keep the enclosure awake) |
| `smart.interval_seconds` | 300 | SMART polling while active |
| `smart.poll_when_inactive` | false | keep false so monitoring never wakes the drive at night |
| `smart.device_type` | `sat` | smartctl `-d` value |
| `temperature.warn/critical/protect` | 45/50/55 | dashboard/log levels |
| `temperature.protect_action` | `none` | `pause_keepalive` pauses reads at `protect` until < protect-3 |
| `web.host/port` | 0.0.0.0 / 8787 | listen address |
| `web.allowed_networks` | private ranges | clients outside these get 403 |

### About temperature
Your drive reports ~51 C. Keepalive reads add almost no heat; the heat comes from the platters
spinning all day. Keeping it awake 17 h/day means it runs warm for 17 h. Shortening the window,
improving airflow around the enclosure, or `protect_action: pause_keepalive` are the real levers.
Levels: 45-49 warning, 50-54 critical, >= 55 protect.

## Firewall (UFW) - LAN only

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8787 proto tcp     # use your LAN range
sudo ufw status numbered
```

Make sure SSH is allowed before enabling UFW (`sudo ufw allow OpenSSH`). Never port-forward 8787
on your router. The dashboard is plain HTTP; the app additionally rejects non-private client
addresses, locks out repeated wrong passwords per IP, and uses HttpOnly/SameSite=Strict
session cookies. For remote access use a VPN (WireGuard/Tailscale), not an open port.

## Troubleshooting

| Symptom | Check |
|---|---|
| `keepalivehd: the service is not running` | `systemctl status keepalivehd`, `journalctl -u keepalivehd -e` |
| "permission denied" on CLI | use `sudo`, or `sudo usermod -aG keepalivehd $USER` and re-login |
| Status says `/mnt/hdd is not mounted` | `findmnt /mnt/hdd`; mount the drive. Service resumes on its own within 10 s |
| "behind USB id X, expected Y" | bridge differs: set `expected_usb_id` to X or `""`, then `sudo systemctl reload keepalivehd` |
| SMART shows `unknown` | `sudo smartctl -d sat -A /dev/sda`; try `"device_type": "auto"` |
| Drive still sleeps while ACTIVE | enclosure may count only some commands: set `"rotate_offsets": true`, try interval 60 |
| Dashboard unreachable | `ss -ltnp | grep 8787`; UFW rule; port conflict is logged and retried every 30 s |
| 403 from dashboard | client IP not in `web.allowed_networks` |
| Forgot password | `sudo keepalivehd passwd` |
| Wrong schedule times | `timedatectl` (service follows the server timezone; picks up changes within a minute) |

## Rollback / uninstall

```bash
sudo bash uninstall.sh                 # remove service + program, keep config
sudo bash uninstall.sh --purge         # also delete config, password, state
sudo bash uninstall.sh --restore-old   # also bring back the old KeepAliveHD timer/service units
```

Uninstalling never touches your drive, mounts, Jellyfin, Immich or Docker. After removal the
enclosure simply goes back to its own spin-down behaviour.

## Files

```
/opt/keepalivehd/app.py, web/          program
/usr/local/bin/keepalivehd             CLI
/etc/keepalivehd/config.json           settings
/etc/keepalivehd/auth.json             password hash (600)
/var/lib/keepalivehd/state.json        current mode, last successful keepalive
/run/keepalivehd/control.sock          CLI socket
```
