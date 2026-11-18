# Focus device: serial protocol (v1)

The ESP32 firmware and the PC app talk over USB serial at **115200 baud**, one message per line (`\n`).
The PC sends plain-text commands. The device sends **one JSON object per line**: the PC app parses
it, and a person can still read it in any serial monitor.

`firmware/main.cpp` implements the device side. `app/device.py` is the PC client, and
`app/simulator.py` is a fake device that follows the same rules (the tests in `tests/test_app.py`
check the client against it).

## Rules

- Every command gets exactly one reply: `{"t":"ok","cmd":"<name>",...}` or
  `{"t":"err","cmd":"<name>","msg":"..."}`. The reply's data is in the same object.
- Anything else the device prints is an **event** (its `"t"` is not `ok` or `err`). Events can arrive
  between a command and its reply.
- A command may start with `#<id> ` (e.g. `#7 info`). The reply then carries `"id":7`. For that reason
  no reply uses `id` for anything else; for example, `calibrate` names the phone `phone`.
- Words are case-insensitive. Distances are in metres, RSSI in dBm, and times in seconds unless the name ends in `_ms`.
- Lines that don't start with `{` (ROM boot messages, crash dumps) are not part of the protocol. Ignore them.

## Phones and how they're measured

A phone is known once it has **paired (bonded)** with the device, up to 3 phones. Each has a stable
`id` (its slot, 0-2) and its Bluetooth identity address `addr`. `irk` says whether the phone shared
its identity key, which is needed to recognise its broadcasts.

| `src`  | Meaning |
|--------|---------|
| `conn` | The phone is connected (`hid` mode). RSSI comes from the live link, 10 times a second. |
| `adv`  | RSSI comes from the phone's own Bluetooth broadcasts, matched with its IRK. Mostly iPhones. Sparse when the phone is locked. |
| `none` | No fresh signal. After `away_s` seconds of nothing, the phone is `away`. |

Signal processing, in three steps:
1. **Level.** The last 2 s (`conn`, at least 5 samples) or 15 s (`adv`, at least 3) of samples, outliers
   removed with a Tukey fence, then averaged **as power** (milliwatts) and converted back to dBm. Fading
   only pulls single readings down, so a plain dB average reads low by an amount that depends on the
   room; a power average does not.
2. **Smoothing.** The level is smoothed with time constant `smooth_s` while it stays within 1.5 dB of
   the current estimate (a phone lying still). The further a reading is off, the more it is trusted;
   at 5 dB it is taken as is, so a phone being picked up shows at once. `rssi_f` is this smoothed level.
3. **Distance.** The log-distance model `d = 10^((ref - rssi_f) / (10 * path_n))`. `ref` is `ref_conn`
   or `ref_adv`, depending on the source.

Calibration uses the same level as step 1. `calibrate` measures one spot and sets `ref`. `autocal`
measures two spots and sets both `ref` and `path_n`, which is what makes distances right in a given room.

**Orientation.** A phone's antenna is stronger in some directions than others, so turning or flipping
the phone changes its level by several dB, and one receiver cannot tell that from a change of distance.
`autocal` therefore also measures the phone in up to four orientations. It stores the spread as
`orient_db` and moves `ref` to the average orientation, so any orientation is off by about half the
spread at most. The zones then use a hysteresis of at least `orient_db / 2` (never more than 8 dB),
so turning the phone in place cannot change its zone. The distance shown still moves when the phone
is turned; only a second receiver could remove that.

Zones, nearest first: `near` (closer than `near_m`), `room`, `far` (beyond `far_m`), and `away` (no signal).
Each zone boundary has a hysteresis band of +/- `hyst_db` (default 3 dB, so a 6 dB gap; wider if
`orient_db` asks for it, see Orientation above). A change only
counts once the readings have pointed away from the current zone for long enough:
- entering `near` takes `near_in_s` (3 s) of continuous `near` readings
- leaving `near` takes `near_out_s` (5 s)
- moving between `room` and `far` takes `zone_s` (5 s)

The clock for leaving the current zone keeps running while readings flip between other zones. For
example, a phone leaving `near` whose readings alternate between `room` and `far` still leaves
after `near_out_s`. While a change is pending, `phone` events carry `pending` and `pending_ms`.
When the live connection drops, a `near`/`room` phone is moved to `far` straight away.

With no fresh signal (`src` = `none`), `rssi_f` and `dist_m` are `null`. The device never repeats
a stale distance. The zone stays where it is (capped at `far`) until `away_s` passes.

## Modes (`cfg mode`)

| Mode | What the phone sees | Signal | Notes |
|---|---|---|---|
| `hid` (default) | A media remote called "Focus Device", connected | `conn`, plus `adv` when available | Works on iPhone and Android. iOS may hide its on-screen keyboard while connected. |
| `scan` | A heart-rate sensor; it disconnects right after pairing | `adv` only | iPhone only (idle Android phones barely broadcast). Keyboard unaffected. |

Changing the mode restarts the device. Phones paired in the other mode should be paired again.

## Commands

| Command | Reply data |
|---|---|
| `help` | `cmds`: list of command strings |
| `info` | `fw`, `ver`, `chip`, `mac`, `uptime_s`, `ble` (started OK), `rpa_selftest` (bool), `pairing`, `max_phones`, `phones`, `cfg`, `focus` |
| `phones` | `phones`: `[{"id","addr","irk","connected","src","zone","dist_m","rssi_f"}]` |
| `pair [seconds]` | Opens pairing (default 120, 10-600). `pairing`: true, `left_s`. Error if all slots are used |
| `pair stop` | `pairing`: false |
| `forget <id>` / `forget all` | `forgot`: number of phones removed (their bonds are deleted too) |
| `focus` | `focus` object |
| `focus start [minutes]` | Starts a session (default `focus_min`; 0 = no time limit). `focus` object |
| `focus stop` | Ends the session. `focus` object with the final numbers |
| `cfg` | `cfg` object |
| `cfg <key> <value>` | Sets one setting (saved across reboots). `cfg` object, `restarting` (true after a mode change) |
| `cfg reset` | Restores the defaults (keeps the mode). `cfg` object |
| `calibrate [seconds] [metres]` | Measures the strongest phone for `seconds` (3-60, default 8) held at `metres` (0.1-5, default 1). Reply: `calibrating`, `phone`, `seconds`, `metres`. The result arrives later as a `calibrated` event |
| `autocal [near_m] [far_m] [turns]` | Two-spot calibration of the strongest phone, at `near_m` (0.2-3, default 0.5) and then `far_m` (at least 1.5 x `near_m`, max 10, default 2), followed by `turns` (0-3, default 3) more orientations at the second spot. Reply: `autocal`: true, `phone`, `near_m`, `far_m`, `turns`. Progress and the result arrive as `autocal` events (see below). Error if a calibration is already running |
| `autocal stop` | Cancels it; nothing is saved. `autocal`: false |
| `stream on` / `stream off` | Turns the periodic `phone` events on or off (default on) |
| `restart` | Replies, then reboots |

### `cfg` keys

| Key | Default | Range | Meaning |
|---|---|---|---|
| `near_m` | 1.0 | 0.1 to < `far_m` | Closer than this = `near` (phone within reach) |
| `far_m` | 4.0 | > `near_m` to 30 | Further than this = `far` |
| `path_n` | 2.7 | 1.5-5 | Path-loss exponent (2 = open space, 3-4 = walls/people; set by `autocal`) |
| `ref_conn` | -60 | -100 to -20 | Connection RSSI at 1 m (set by `calibrate` and `autocal`) |
| `ref_adv` | -65 | -100 to -20 | Broadcast RSSI at 1 m (set by `calibrate` and `autocal`) |
| `hyst_db` | 3 | 0-10 | Hysteresis around each zone boundary, in dB (bigger = steadier but slower) |
| `near_in_s` | 3 | 0-30 | Seconds of `near` readings before the zone becomes `near` |
| `near_out_s` | 5 | 0-60 | Seconds of not-`near` readings before `near` ends |
| `zone_s` | 5 | 0-60 | Seconds before a `room` <-> `far` change counts |
| `smooth_s` | 4 | 0-20 | Smoothing time while the signal only jitters (steadier distance for a phone lying still). 0 = off |
| `orient_db` | 0 | 0-20 | How many dB the signal changes between the phone's best and worst orientation (set by `autocal`). The zone hysteresis is at least half of it, up to 8 dB. 0 = not measured |
| `away_s` | 30 | 5-600 | Seconds without any signal before a phone is `away` |
| `grace_s` | 5 | 0-600 | During focus, how long the phone may be `near` before it counts as a pickup |
| `focus_min` | 25 | 1-1440 | Session length for the board's button and for `focus start` with no number |
| `stream_ms` | 500 | 100-10000 | Interval of `phone` events |
| `buzzer_pin` | -1 | -1 or a free output GPIO | Optional buzzer (-1 = none). Flash, USB, serial, LED and button pins are refused |
| `mode` | `hid` | `hid`, `scan` | See Modes. Changing it restarts the device |

### `focus` object

`{"active":true,"limit_s":1500,"elapsed_s":312,"left_s":1188,"near_s":14,"pickups":1,"distracted":false}`

- `pickups`: how many times the phone came `near` and stayed past `grace_s`.
- `near_s`: total seconds the phone was `near` during the session.
- `distracted`: the phone is `near` right now and past its grace period.
- `left_s` is `-1` when the session has no time limit.

## Events

| `t` | Fields | When |
|---|---|---|
| `hello` | `fw`, `ver`, `chip`, `mac` | After boot |
| `phone` | `id`, `src`, `rssi` (latest raw; absent if never heard), `rssi_f` (filtered; null without fresh signal), `dist_m` (same), `zone`, `age_ms` (since the last sample; -1 = never), `connected`; plus `pending` (zone) and `pending_ms` (time left) while a zone change waits out its dwell time | Every `stream_ms` per known phone |
| `zone` | `id`, `zone`, `prev`, `dist_m` | A phone changes zone |
| `conn` | `id`, `addr`, `state` (`connected`/`disconnected`), `reason` (disconnects) | A known phone's link changes |
| `pair` | `state`: `open` (`left_s`, `mode`), `bonded` (`id`, `addr`, `irk`), `failed`, `closed` (`why`: `bonded`/`timeout`/`stopped`/`already paired`) | Pairing progress |
| `focus` | `state` (`started`/`distracted`/`recovered`/`done`/`stopped`) plus the `focus` object fields | Session changes |
| `calibrated` | `ok`, `id` (phone), `metres`, `samples_conn`, `samples_adv`, `ref_conn` and/or `ref_adv` (the new values), `msg` (on failure) | Calibration finished |
| `autocal` | `state` plus: `place` (`step` 1 or 2, `metres`, `left_s`, `retry` when the spot is being measured again), `turn` (`turn` 1-3, `of`, `how`: `quarter` or `face_down`, `retry`), `measuring` (`step`, `metres`, `seconds`, and `turn` when it is an orientation), `done` (`id` (phone), `src`, `path_n`, `ref_conn`, `ref_adv`, `rssi1`, `rssi2`, `clamped`; with orientations also `orientations`, `orient_db`, `orient_offset`), `failed` (`msg`), `stopped` | Calibration progress |
| `log` | `msg` | Human-readable diagnostics |

### How `autocal` runs

1. `place` step 1: the device waits (up to `left_s`) until the phone's signal is steady: the last 2 s
   and the 2 s before agree within 2 dB (`adv` source: 6 s halves, 3 dB).
2. `measuring` step 1: 6 s of raw samples. If the first and second half differ by more than 3 dB the
   phone moved: back to `place` with `retry` (at most 4 times).
3. `place` step 2: as step 1, but only once the signal is at least 4 dB weaker than at the first
   spot, so the second spot is never measured where the first one was.
4. `measuring` step 2. `path_n` is fitted through the two levels (`rssi1`, `rssi2`) and kept
   inside 1.5-5 (`clamped` says if it had to be); `ref` is the best fit for that `path_n`. The source
   that was not measured keeps its old offset to the other one.
5. `turn` 1 to `turns`, at the second spot: the user turns the phone a quarter turn (`how`: `quarter`;
   the third is `face_down`) and lets go. The device waits at least 7 s, then until the signal is
   steady (20 s at most, since turning need not change the level), and measures 4 s.
6. `done`: with the levels of all orientations (the first is the one from step 4), `orient_db` is the
   strongest minus the weakest and `orient_offset` is their average minus the first. Both references
   are moved by `orient_offset`. Everything is saved. If an orientation can't be measured, the run
   ends with the ones it has rather than failing.

A `failed` or `stopped` run changes nothing. During `place` the LED blinks evenly, during `turn` it
blinks fast, and during `measuring` it is solid. With a buzzer: one beep when measuring starts, two when a spot is done, three low ones on failure.

## Physical controls (no PC needed)

- **BOOT button, short press**: start or stop a focus session (`focus_min` minutes).
- **BOOT button, hold 3 s**: open pairing for 120 s.
- **LED**: double blink = pairing; fast flashing = phone picked up during focus; solid = phone `near`;
  short blip every 2 s = focusing and the phone is away.
- **Buzzer** (if `buzzer_pin` is set): beeps when a session starts or stops, three beeps on a pickup (then one every 15 s
  while the phone stays near), and two long beeps when the session is done.

## Example

```
> #1 info
< {"t":"ok","cmd":"info","id":1,"fw":"focus-device","ver":"0.2.0","chip":"ESP32-D0WD-V3",...}
> #2 focus start 25
< {"t":"focus","state":"started","active":true,"limit_s":1500,...}
< {"t":"ok","cmd":"focus","id":2,"focus":{"active":true,"limit_s":1500,...}}
< {"t":"phone","id":0,"src":"conn","rssi":-67,"rssi_f":-66.1,"dist_m":2.3,"zone":"room","age_ms":40,"connected":true}
< {"t":"zone","id":0,"zone":"near","prev":"room","dist_m":0.7}
< {"t":"focus","state":"distracted","pickups":1,...}
```
