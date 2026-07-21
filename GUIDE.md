# The 522 Tracker, in plain language

This is the "what am I looking at" guide. No jargon. If you want the mechanics, read
[PREDICTION.md](PREDICTION.md); if you want the origin story and the setup, read
[README.md](README.md).

The app answers one question: **when does my next 522 actually come, and should I leave now?**

---

## Reading the screen

The big number at the top is the wait for the next bus. Everything else on screen exists
to tell you how much to believe it.

| What you see | What it means |
| --- | --- |
| **The big number** | Minutes until the next bus reaches your stop. |
| **A range under it** (e.g. "9 to 14") | The realistic spread. Real buses vary; a single number would be a lie. |
| **A coloured dot / confidence word** | How much real live data is behind the number. |
| **"pos 40s ago"** | How old the bus's last GPS fix is. İBB only refreshes roughly once a minute, so this is normally 40 to 100 seconds. That is not a fault. |
| **A badge on the bus** | Where the number came from. See below. |
| **Going / Home toggle** | Which direction you are travelling. The colours are the bus colours on the map. |

## The four kinds of prediction

Not every number is produced the same way, and the badge tells you which one you got.
This matters because they are trustworthy in different amounts.

**1. Live (the normal case).**
The app has watched real buses drive this route hundreds of times, so it knows how long
each stop-to-stop leg actually takes at this hour on this kind of day. It adds up the legs
between the bus and you. This is the good one.

**2. Waiting at origin.**
The bus exists, but it has not set off yet. It is parked at the terminus filling up. Its
distance to you is short, but the wait is not, because it will sit there first. The app
uses the timetable's next departure instead of pretending the bus is about to arrive, and
it says "waiting at origin" so you know. See the section below, this one used to be wrong.

**3. Estimate.**
The app has no learned history for that piece of road yet, so it falls back to plain
distance and speed. Rough, and marked as such. In practice you only see this for a bus
that is basically already at your stop.

**4. Estimated, İBB down.**
İBB's feed has dropped (it does this often). Rather than going blank, the app takes the
last known buses and rolls them forward along the road at the speed they were doing. The
longer the outage, the vaguer this gets, and it is clearly labelled.

## "Waiting at origin", and why it used to lie

Your home stop is the **second stop** out of the Mecidiyeköy Mezarlık terminus. A bus
parked at that terminus is only about a minute of *driving* away from you, but it can sit
there for eight to twenty minutes before it departs, filling up with passengers.

The app used to just measure the driving distance and cheerfully tell you "1 minute". You
would run for the stop and then stand there for a quarter of an hour.

It now recognises a bus that has not left yet and quotes the **timetable departure plus
the drive**, and it labels it. It also learns how late buses really leave that terminus
compared to the printed schedule, and folds that lateness in.

## The hollow arrow

Buses on the map are arrows.

- **Solid arrow**: a live, fresh GPS fix. This is where the bus really is.
- **Hollow / outline arrow**: the bus has lost GPS. The app holds it at its last known
  spot rather than making it vanish, and tells you when the last fix was. If it stays lost
  for five minutes it disappears.

This exists because buses used to blink off the map while still showing an ETA, which
looked like a bug and was really just İBB dropping a coordinate.

## How much should you trust the number?

Honestly, it depends entirely on how far out the bus is. Measured against buses that
actually turned up:

| If the bus is... | How good the number is |
| --- | --- |
| **Under 6 minutes away** | Very good. Usually within a minute. |
| **6 to 20 minutes away** | Good. Typically within about a minute and a half. From 10 minutes out it is close to dead-on; between 6 and 10 it leans very slightly toward the bus arriving a touch later than shown. This is the range you normally look at. |
| **20 to 25 minutes away** | Usable, drifting a couple of minutes optimistic. |
| **Over 25 minutes away** | Do not plan around it. It can be off by five to ten minutes, because that far out the answer depends on dispatch decisions nobody has made yet. Use it as "there is a bus coming eventually", not as a time. |

The short version: **inside about 20 minutes you can time your walk by it. Beyond 25
minutes, treat it as a rumour.**

## When it errs, which way does it err?

Deliberately, it prefers to send you to the stop slightly **early** rather than slightly
late. Being told "5 minutes" when the bus takes 6 means you wait a minute, which is
annoying. Being told "6 minutes" when it takes 5 means you miss the bus entirely. The
corrections are tuned to stay on the annoying side of that trade rather than the
bus-missing side.

## It corrects itself

Every prediction is written down, and so is every actual arrival. Every so often the two
get compared, which is how we know all the numbers above. When a systematic lean shows up
(for example, "predictions made 10 minutes ahead run four minutes optimistic"), a
correction gets measured and applied, and then the same machinery checks whether the
correction actually worked.

That is why the app can tell you its own accuracy instead of just asserting it is good.

## If something looks wrong

| Symptom | What is probably happening |
| --- | --- |
| **"pos 120s ago"** | Normal-ish. İBB refreshes about once a minute and the app polls every 40 seconds, so 100 seconds is the realistic floor. The bus position shown is projected forward anyway. |
| **An ETA but no bus on the map** | The bus lost GPS. It should be showing as a hollow arrow. |
| **Everything says "estimated, İBB down"** | İBB's gateway is having one of its regular episodes. The app deliberately backs off and goes quiet instead of hammering it, and comes back on its own. |
| **The page will not load at all** | Check Tailscale is on, on the phone and on the laptop. That is the usual answer. |
| **A bus at the terminus shows a long wait** | Correct behaviour, not a bug. It has not departed yet. |

## The one rule to remember

The app is built to be **honest rather than confident**. A range, a staleness note, a
"waiting at origin" label and a hollow arrow are all it admitting what it does not know.
When it gives you a tight number with high confidence inside 20 minutes, that number has
been checked against thousands of real arrivals, and you can walk to the stop on it.
