# powerlog

Visual timeline of macOS power state activity, built from `pmset -g log`.

Produces a self-contained HTML page (no network, no dependencies) showing:

- **Power state** lane: awake / dark wake / asleep, plus gaps where the log is silent before a boot
- **Display** on/off, **SleepService** windows, **battery** charge with AC periods shaded
- **Wakes & events**: full wakes, dark wakes, boots, battery health warnings
- **Power assertions** as bars, one row per process, type, or process + type, colored by effect
  (prevents system sleep, prevents display sleep, activity, background/maintenance)
- **Breakdown** histograms for the visible time range: wake reasons, scheduled wake requests,
  sleep reasons, assertion types, assertion holders, and slow sleep/wake clients

Hover for details, click an item for the full record (wake driver reasons, pending wake
requests, slow PM/kernel clients, assertion IDs). Click a histogram row to filter or highlight
it in the timeline.

## Usage

```sh
./powerlog                       # run pmset, write HTML to $TMPDIR, open it
./powerlog --since 24h           # limit range (also: 90m, 2d, 1w, 'YYYY-MM-DD HH:MM')
./powerlog -o power.html --no-open
./powerlog -i saved-pmset.log    # analyze a log captured elsewhere ('-' for stdin)
./powerlog --text                # terminal summary: transitions, wake reasons, top holders
./powerlog --json                # parsed data
```

A single self-contained script; copy it anywhere on your `PATH`. Requires Python 3.8+ (stock
macOS `python3` works). `pmset -g log` doesn't need root.

Timeline controls: drag to pan, pinch or option/cmd + two-finger scroll (or `+`/`-`) to zoom
around the pointer (sideways motion pans at the same time), shift+scroll or arrow keys to pan,
drag on the overview strip to select a range, double-click or `0` to show all.

## How the log is interpreted

- An assertion's span runs from `Created`/`TurnedOn` to `Released`/`TimedOut`/`ClientDied`/
  `CapExpired`/`TurnedOff`. When the creation predates the log, the start is derived from the
  logged duration and marked approximate. `Summary` lines open spans for assertions not yet seen.
- Assertions still open at a `Start` (boot) entry are closed at the last entry
  before the reboot; ones open at the end of the log are shown as still held.
- "Time held" merges overlapping assertions within a group, so a process holding 50 concurrent
  assertions for an hour counts one hour.
- Wake reason categories use the token after the final `/` in powerd's `due to` text
  (e.g. `SleepService`, `Maintenance`, `UserActivity Assertion`); switch to "Full reason" for the
  raw string. "Scheduled wake requests" credits each wake to the earliest pending request
  listed when the system last went to sleep.

## Tests

```sh
python3 -m unittest discover -s tests
```
