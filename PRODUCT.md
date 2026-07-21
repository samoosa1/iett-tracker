# Product

## Register

product

## Users
One person (the owner), on their phone, usually outdoors and often in a hurry —
deciding whether to leave now to catch the İETT **522** bus (going: board at
Ümraniye → Mecidiyeköy; home: board at Mecidiyeköy → Ümraniye). Context is a
5-second glance in bright daylight, one-handed, sometimes with flaky signal.
Occasionally checked from a laptop.

## Product Purpose
Answer one question faster and more honestly than the official app: **"when does my
next 522 come, and should I leave now?"** It learns real stop-to-stop times from
live GPS, so its ETAs beat the timetable — and it tells the truth about
uncertainty (ranges, confidence, staleness) instead of faking precision. Success =
the owner trusts the number enough to time their walk to the stop by it.

## Brand Personality
Calm, precise, quietly confident. A well-made **departure board**, not an app
fighting for attention. Information-first: the answer is the interface. Three
words: **glanceable, honest, unhurried.**

## Anti-references
- **A generic Maps clone** — a pin on stock Google/Apple Maps with no identity of
  its own. The map serves the answer here; it is not the point.
- Cluttered SaaS dashboards, card grids, chart-junk, numbers competing for
  attention.
- Sterile corporate transit-authority navy-and-gray.

## Design Principles
1. **The answer in one glance.** One primary number (the ETA) owns the hero;
   everything else is secondary and quiet. If it takes more than a beat to read
   "when's my bus," the design failed.
2. **Honest about uncertainty.** Show confidence, ranges, staleness, "GPS lost,"
   "waiting at origin" truthfully. Never fake a precise position or time. Trust is
   the product; a confident wrong number destroys it.
3. **Its own object, not a map with a pin.** The identity lives in the hero card,
   the type, the motion of the buses — the map is a calm substrate beneath the
   answer.
4. **Outdoor-legible, thumb-reachable.** High contrast for sunlight (not elegant
   light-gray), primary controls in thumb range, big enough to read at a glance.
5. **Quiet by default; motion only where it informs.** Movement conveys liveness
   and state (gliding buses, breathing "live" dot, ghosting on GPS loss), never
   decoration. Respect reduced-motion.

## Accessibility & Inclusion
- **Sunlight contrast is a first-class requirement**, not a nicety: body/label
  text ≥4.5:1, primary ETA far higher. No elegant low-contrast gray.
- `prefers-reduced-motion`: replace glides/breathing with instant/crossfade.
- One-handed phone use: key toggles (Going/Home) and the login within thumb reach.
- Colour is never the only signal (leg direction also encoded by tooltip/shape,
  staleness by hollow arrow + text, confidence by dot + label).
