# Moving the İETT tracker to an online host

The easiest reliable path: a small **VPS** (one normal Linux server). Why not
Vercel/Supabase/Render/Railway/Fly? They're serverless/ephemeral — your SQLite
`~/.iett_segments.db` (all the learned ETAs) would reset on every restart unless
you fight with mounted volumes. A VPS has a normal disk, so it "just works", and
the collector can run 24/7.

## What you need
- A VPS (~€4/month). Good options: **Hetzner** (Germany/Finland), **DigitalOcean**,
  or a **Turkish provider**. Pick **Turkey or nearby EU** — see Step 0.
- ~10 minutes.

## Steps

**Step 0 — confirm İBB allows the host's IP** (the one real gotcha)
On the new server, run:
```bash
bash check-ibb.sh
```
If the timetable line says *reachable* → good. If *UNREACHABLE* → İBB is
geo-blocking that region; use a Turkey/EU host instead and re-test.

**Step 1 — copy two files to the server**
From your Mac:
```bash
scp ~/Projects/iett-takip/iett_tracker.py \
    ~/Projects/iett-takip/deploy/setup.sh \
    user@YOUR_SERVER_IP:~/
```

**Step 2 — install**
On the server:
```bash
bash setup.sh
```
That installs Python, the app, dependencies, a `systemd` service (auto-start +
auto-restart, the Linux version of the launchd service we used on your Mac), and
seeds the ETA baselines. Done.

**Step 3 — reach it safely**
The app has **no password**. Don't leave the port open to the whole internet.
Easiest secure access: install **Tailscale** on the server and your phone, then
open the server's Tailscale address — private, encrypted, works anywhere. (Or
restrict the firewall to your own IP.)

## After it's running
- Status:  `sudo systemctl status iett-takip`
- Logs:    `journalctl -u iett-takip -f`
- Learned: `~/iett-takip/.venv/bin/python ~/iett-takip/iett_tracker.py --eta-stats`
- It now collects 24/7 — including the evening rush hours your laptop was missing.

## Turning off your Mac's copy
Once the server runs, stop the Mac service so you're not running two:
```bash
launchctl unload ~/Library/LaunchAgents/com.<you>.iett-takip.plist
```
(The learned database doesn't transfer automatically — the server starts its own
and re-learns. If you want your Mac's history on the server, copy
`~/.iett_segments.db` over before first start.)
