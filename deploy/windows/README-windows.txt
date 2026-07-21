==========================================================
  İETT TRACKER — run it on a Windows laptop (24/7)
==========================================================

You need THREE files together in one folder (e.g. a new folder
called  iett-takip  on the Desktop):
    - iett_tracker.py        (the app)
    - setup-windows.bat      (one-time setup)
    - run-iett.bat           (starts the tracker)

----------------------------------------------------------
STEP 1 — Install Python
----------------------------------------------------------
- Go to https://www.python.org/downloads/  and install Python 3.
- On the FIRST install screen, TICK the box:
      [x] Add python.exe to PATH
  (This is the #1 thing people forget.)

----------------------------------------------------------
STEP 2 — One-time setup
----------------------------------------------------------
- Double-click  setup-windows.bat
- It installs the dependencies and downloads the traffic data
  (about 135 MB, one time). Wait for "Setup complete!".

----------------------------------------------------------
STEP 3 — Start it
----------------------------------------------------------
- Double-click  run-iett.bat
- A black window opens and stays open (that's the server running).
- On THIS laptop, open a browser to:   http://localhost:8731/
- Leave the black window open. If it ever crashes it restarts itself.

----------------------------------------------------------
STEP 4 — Make it run 24/7 (never sleep, auto-start)
----------------------------------------------------------
NEVER SLEEP (so it keeps collecting while you're away):
  Settings > System > Power
    - "When plugged in, PC goes to sleep after"  ->  Never
    - "When I close the lid"  ->  Do nothing   (if a laptop)
  Keep it PLUGGED IN.

AUTO-START when the laptop boots:
  1. Press  Win + R , type:  shell:startup   , press Enter.
     (A "Startup" folder opens.)
  2. Right-click  run-iett.bat  ->  Copy.
  3. Right-click inside the Startup folder ->  Paste shortcut.
  Now the tracker launches automatically every time the laptop starts.
  (Tip: turn on auto-login so it boots straight to the desktop.)

----------------------------------------------------------
STEP 5 — See it on your phone from anywhere (Tailscale)
----------------------------------------------------------
A home laptop has no public address, so we use Tailscale (free):
  1. Make a free account at  https://tailscale.com
  2. Install Tailscale on the Windows laptop, sign in.
  3. Install the Tailscale app on your phone, sign in (same account).
  4. In the Tailscale app, find the laptop's address (looks like 100.x.x.x).
  5. On your phone open:   http://100.x.x.x:8731/
  That works on cellular, other wifi, anywhere — privately.

----------------------------------------------------------
CHECK WHAT IT HAS LEARNED
----------------------------------------------------------
  Open a Command Prompt in the folder and run:
      py -3 iett_tracker.py --eta-stats

----------------------------------------------------------
NOTES
----------------------------------------------------------
- First time you run it, Windows Firewall may ask to allow Python
  on the network — click "Allow access" (needed for phone viewing).
- The learned database lives in your user folder as
  .iett_segments.db  and survives restarts.
