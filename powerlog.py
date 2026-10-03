#!/usr/bin/env python3
"""powerlog - visual timeline of macOS power state activity from `pmset -g log`."""

import argparse, bisect, json, re, socket, subprocess, sys, tempfile, webbrowser
import datetime as dt
from collections import Counter
from pathlib import Path

LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [-+]\d{4}) ([^\t]*)\t?(.*)$")
CHARGE_RE = re.compile(r"Using (AC|BATT|Batt)\s*\(Charge:\s*(\d+)%?\)", re.I)
ASSERT_RE = re.compile(r'^PID (\d+)\((.*?)\) (\w+) (\w+) "(.*)" +(\d+):(\d\d):(\d\d) +id:(\S+) +\[System: ([^\]]*)\]')
WAKEREQ_RE = re.compile(r"\[(\*?)process=(\S+) request=(\S+) deltaSecs=(\d+) wakeAt=(\S+ \S+)(?: info=\"(.*?)\")?\]")
SLOW_RE = re.compile(r"\[(.*?) is slow\((?:msg: (.*?)\))?\(?(\d+) ms\)\]")
ASSERT_END = {"Released", "TimedOut", "ClientDied", "CapExpired", "TurnedOff"}
SECTION_BREAK = ("Time stamp", "==========", "UUID:", "Sleep/Wakes", "Total Sleep", "PM ASL", "Assertion status",
                 "Listed by", "Kernel Assertions", " ", "\t")


def parse_ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")


def read_records(lines):
    """Yield (datetime, domain, message, continuation lines) for each timestamped entry."""
    cur = None
    for line in lines:
        line = line.rstrip("\n")
        m = LINE_RE.match(line)
        if m or (cur and line.strip() and line.startswith(SECTION_BREAK)):
            if cur:
                yield cur
            cur = m and (parse_ts(m[1]), m[2].strip(), m[3].split("\t")[0].strip(), [])
        elif cur and line.strip():
            cur[3].append(line.strip())
    if cur:
        yield cur


class Model:
    def __init__(self):
        self.transitions, self.display, self.battery, self.assertions, self.events, self.sleepservice = [], [], [], [], [], []
        self.open_assert, self.open_ss, self.times, self.tz = {}, None, [], 0
        self.first = self.last = None

    def add_battery(self, ms, msg):
        m = CHARGE_RE.search(msg)
        if m:
            self.battery.append({"t": ms, "ac": m[1].upper() == "AC", "pct": int(m[2])})

    def add_transition(self, ms, state, reason, msg, **kw):
        self.transitions.append({"t": ms, "state": state, "reason": reason, "msg": msg, **kw})
        self.add_battery(ms, msg)

    def feed(self, t, domain, msg, cont):
        ms = int(t.timestamp() * 1000)
        prev = self.times[-1] if self.times else ms
        self.times.append(ms)
        self.tz = int(t.utcoffset().total_seconds() // 60)
        tr = self.transitions[-1] if self.transitions else None
        if domain == "Assertions":
            self.assertion(ms, msg)
        elif domain == "Sleep":
            m, secs = re.search(r"due to '([^']*)'", msg), re.search(r"(\d+) secs\s*$", msg)
            self.add_transition(ms, "sleep", m[1] if m else msg, msg, slept=secs and int(secs[1]))
        elif domain in ("DarkWake", "Wake"):
            m, frm = re.search(r"due to (.*?)(?: Using |$)", msg), re.search(r"from (.*?) \[", msg)
            self.add_transition(ms, "awake" if domain == "Wake" else "dark", m[1].strip() if m else msg, msg,
                                **{"from": frm and frm[1], "promoted": msg.startswith("DarkWake to FullWake")})
        elif domain == "WakeDetails" and tr:
            tr["drivers"] = [re.sub(r"^DriverReason:|\s*- DriverDetails:.*$", "", x) for x in [msg] + cont]
        elif domain == "WakeTime" and tr:
            m = re.search(r"([\d.]+) sec", msg)
            if m:
                tr["wakeTime"] = float(m[1])
        elif domain == "HibernateStats" and tr:
            tr["hib"] = msg
        elif domain == "Wake Requests":
            sl = next((x for x in reversed(self.transitions[-3:]) if x["state"] == "sleep"), None)
            if sl:
                sl["wakeRequests"] = [{"next": bool(a), "proc": p, "req": r, "delta": int(d), "at": w, "info": i}
                                      for a, p, r, d, w, i in WAKEREQ_RE.findall(msg)]
        elif domain in ("PM Client Acks", "Kernel Client Acks"):
            slow = [{"who": w, "msg": m or None, "ms": int(n)} for w, m, n in SLOW_RE.findall(msg)]
            if tr and slow:
                # keep the worst delay per client across repeated reports
                key, best = "pmAcks" if domain.startswith("PM") else "kernelAcks", {}
                for x in sorted(tr.get(key, []) + slow, key=lambda x: x["ms"]):
                    best[x["who"], x["msg"]] = x
                tr[key] = sorted(best.values(), key=lambda x: -x["ms"])[:10]
        elif domain == "Notification" and "Display is turned o" in msg:
            self.display.append({"t": ms, "on": "turned on" in msg})
        elif domain == "Start":
            self.close_open(prev, "reboot")  # assertions don't survive a restart
            self.add_transition(ms, "boot", msg, msg)
        elif domain == "com.apple.sleepservices.sessionStarted":
            self.open_ss = ms
        elif domain == "com.apple.sleepservices.sessionTerminated":
            if self.open_ss is not None:
                self.sleepservice.append({"s": self.open_ss, "e": ms})
                self.open_ss = None
        elif not domain.startswith(":"):
            self.events.append({"t": ms, "kind": "Battery health" if domain == "BatteryHealth" else domain, "msg": msg})

    def assertion(self, ms, msg):
        m = ASSERT_RE.match(msg)
        if not m:
            if msg.startswith("Summary- [System:"):
                self.add_battery(ms, msg)
            return
        pid, proc, action, atype, name, h, mi, s, aid, sysflags = m.groups()
        key, base = (pid, aid), {"pid": int(pid), "proc": proc, "type": atype, "name": name, "id": aid}
        # start derived from the logged duration when creation isn't in the log
        inferred = {**base, "s": ms - (int(h) * 3600 + int(mi) * 60 + int(s)) * 1000, "inferred": True}
        if action in ("Created", "TurnedOn", "Summary"):
            self.open_assert.setdefault(key, inferred if action == "Summary" else {**base, "s": ms})
        elif action in ASSERT_END:
            a = self.open_assert.pop(key, inferred)
            self.assertions.append({**a, "e": ms, "end": action, "sys": sysflags.strip()})

    def close_open(self, ms, why):
        self.assertions += [{**a, "e": max(ms, a["s"]), "end": why} for a in self.open_assert.values()]
        self.open_assert = {}

    def finish(self):
        if not self.times:
            return
        self.first, self.last = self.times[0], self.times[-1]
        self.close_open(self.last, "ongoing")
        if self.open_ss is not None:
            self.sleepservice.append({"s": self.open_ss, "e": self.last})
        self.assertions.sort(key=lambda a: a["s"])
        self.transitions.sort(key=lambda x: x["t"])

    def intervals(self):
        out, trs = [], self.transitions
        for i, tr in enumerate(trs):
            nxt = trs[i + 1] if i + 1 < len(trs) else None
            end = nxt["t"] if nxt else self.last
            state = "awake" if tr["state"] == "boot" else tr["state"]
            if nxt and nxt["state"] == "boot":
                # shutdown isn't logged; the machine was off for an unknown span after the last entry before boot
                j = bisect.bisect_left(self.times, end)
                off = min(end, max(tr["t"], self.times[j - 1] if j else end))
                out += [{"s": tr["t"], "e": off, "state": state, "i": i}, {"s": off, "e": end, "state": "unknown", "i": None}]
            else:
                out.append({"s": tr["t"], "e": end, "state": state, "i": i})
        return [x for x in out if x["e"] > x["s"]]

    def clip(self, since, until):
        lo, hi = since or float("-inf"), until or float("inf")
        before = [x for x in self.transitions if x["t"] < lo]
        for k in ("transitions", "display", "battery", "events"):
            setattr(self, k, [x for x in getattr(self, k) if lo <= x["t"] <= hi])
        for k in ("assertions", "sleepservice"):
            setattr(self, k, [x for x in getattr(self, k) if x["e"] >= lo and x["s"] <= hi])
        if before:
            # carry the state in effect at the cutoff into the window
            self.transitions.insert(0, {**before[-1], "t": since, "clipped": True})
        self.first, self.last = max(self.first, lo), min(self.last, hi)

    def to_json(self, meta):
        return {"meta": {**meta, "first": self.first, "last": self.last, "tz": self.tz},
                "transitions": self.transitions, "intervals": self.intervals(), "display": self.display,
                "battery": self.battery, "assertions": pack_assertions(self.assertions),
                "sleepservice": self.sleepservice, "events": self.events}


ASSERT_COLS = ["s", "e", "pid", "inferred", "proc", "type", "name", "id", "end", "sys"]


def pack_assertions(items):
    """Columnar rows; columns from index `nums` on are indexes into the shared `strings` list."""
    index = {}
    rows = [[a.get(c, 0) for c in ASSERT_COLS[:4]] + [index.setdefault(a.get(c, ""), len(index)) for c in ASSERT_COLS[4:]]
            for a in items]
    return {"cols": ASSERT_COLS, "nums": 4, "strings": list(index), "rows": rows}


def parse(lines):
    m = Model()
    for rec in read_records(lines):
        m.feed(*rec)
    m.finish()
    return m


def parse_when(s, now):
    """Relative durations (90m, 6h, 2d, 1w) or absolute 'YYYY-MM-DD[ HH:MM[:SS]]' (local time), as epoch ms."""
    if s is None:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhdw])", s.strip())
    if m:
        return int((now - float(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m[2]]) * 1000)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(dt.datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            pass
    raise SystemExit(f"powerlog: can't parse time '{s}'")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=True).stdout


def machine_meta():
    meta = {"host": socket.gethostname().split(".")[0]}
    for key, cmd in (("model", ["sysctl", "-n", "hw.model"]), ("os", ["sw_vers", "-productVersion"])):
        try:
            meta[key] = run(cmd).strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    return meta


def fmt_dur(secs):
    d, r = divmod(int(secs), 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return f"{d}d{h:02d}h" if d else f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s" if m else f"{s}s"


def union_secs(ivs):
    total, end = 0, float("-inf")
    for s, e in sorted(ivs):
        if e > end:
            total, end = total + e - max(s, end), e
    return total / 1000


def print_text(m):
    tz = dt.timezone(dt.timedelta(minutes=m.tz))
    label = {"awake": "AWAKE", "dark": "dark ", "sleep": "sleep", "unknown": "?off "}
    totals = Counter()
    for iv in m.intervals():
        secs = (iv["e"] - iv["s"]) / 1000
        totals[iv["state"]] += secs
        reason = m.transitions[iv["i"]]["reason"] if iv["i"] is not None else "no log entries (powered off?)"
        print(f"{dt.datetime.fromtimestamp(iv['s'] / 1000, tz):%Y-%m-%d %H:%M:%S}  {label[iv['state']]}  "
              f"{fmt_dur(secs):>8}  {reason}")
    print("\nTime in state: " + ", ".join(f"{k} {fmt_dur(v)}" for k, v in sorted(totals.items())))
    print("\nWake reasons:")
    reasons = Counter(("Wake " if t["state"] == "awake" else "DarkWake ") + t["reason"] for t in m.transitions
                      if t["state"] in ("dark", "awake") and not t.get("clipped"))
    for k, n in reasons.most_common(15):
        print(f"  {n:>6}  {k}")
    groups = {}
    for a in m.assertions:
        s, e = max(a["s"], m.first), min(a["e"], m.last)
        groups.setdefault((a["proc"], a["type"]), []).append((s, max(s, e)))
    print("\nTop assertion holders (time held, overlapping assertions merged):")
    for secs, n, (proc, atype) in sorted(((union_secs(v), len(v), k) for k, v in groups.items()), reverse=True)[:15]:
        print(f"  {fmt_dur(secs):>8}  {n:>6}x  {proc:<28} {atype}")


def main():
    ap = argparse.ArgumentParser(description="Visual timeline of macOS power state activity (pmset -g log).")
    ap.add_argument("-i", "--input", help="read a saved `pmset -g log` file ('-' for stdin) instead of running pmset")
    ap.add_argument("-o", "--output", help="HTML output path (default: a temp file)")
    ap.add_argument("--since", help="start of range: relative (6h, 2d) or 'YYYY-MM-DD[ HH:MM]'")
    ap.add_argument("--until", help="end of range, same formats as --since")
    ap.add_argument("--no-open", action="store_true", help="don't open the timeline in a browser")
    ap.add_argument("--json", action="store_true", help="print parsed data as JSON instead of HTML")
    ap.add_argument("--text", action="store_true", help="print a state-transition summary to the terminal")
    args = ap.parse_args()

    if args.input:
        with sys.stdin if args.input == "-" else open(args.input, errors="replace") as f:
            lines = f.read().splitlines()
    else:
        try:
            lines = run(["pmset", "-g", "log"]).splitlines()
        except (OSError, subprocess.CalledProcessError) as e:
            raise SystemExit(f"powerlog: failed to run pmset: {e}")

    model = parse(lines)
    if model.first is None:
        raise SystemExit("powerlog: no log entries found")
    now = dt.datetime.now()
    model.clip(parse_when(args.since, now.timestamp()), parse_when(args.until, now.timestamp()))
    if args.text:
        return print_text(model)
    meta = {**({} if args.input else machine_meta()), "source": args.input or "pmset -g log",
            "generated": int(now.timestamp() * 1000)}
    data = json.dumps(model.to_json(meta), separators=(",", ":"))
    if args.json:
        return print(data)

    out = Path(args.output or Path(tempfile.gettempdir()) / f"powerlog-{now:%Y%m%d-%H%M%S}.html")
    out.write_text(TEMPLATE.replace("/*__POWERLOG_DATA__*/null", data.replace("</", "<\\/")))
    print(out)
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())


TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>powerlog</title>
<style>
:root {
  color-scheme: light dark;
  --surface-0: light-dark(#f4f3f0, #121211);
  --surface-1: light-dark(#fcfcfb, #1a1a19);
  --surface-2: light-dark(#efeee9, #252523);
  --border: light-dark(#dddcd6, #383835);
  --grid: light-dark(#e8e7e2, #2a2a28);
  --text-primary: light-dark(#0b0b0b, #ffffff);
  --text-secondary: light-dark(#52514e, #c3c2b7);
  --text-muted: light-dark(#8a8984, #85847d);
  --blue: light-dark(#2a78d6, #3987e5);
  --st-dark: light-dark(#4a3aa7, #9085e9);
  --st-sleep: light-dark(#d3d1ca, #3a3a37);
  --st-unknown: light-dark(#b9b7b0, #4a4a46);
  --amber: light-dark(#eda100, #c98500);
  --orange: light-dark(#eb6834, #d95926);
  --green: light-dark(#1baf7a, #199e70);
  --sleepsvc: light-dark(#9085e9, #6a5fc7);
  --batt-ac: light-dark(#cdeee0, #1d3d31);
  --highlight: light-dark(#e34948, #e66767);
}
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--surface-0); color: var(--text-primary); font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif; }
h1 { font-size: 20px; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 15px; margin: 0; }
h3 { font-size: 13px; margin: 0; }
b, h3 { font-weight: 600; }
hr { border: 0; border-top: 1px solid var(--border); margin: 6px 0; }
.mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; }
.secondary { color: var(--text-secondary); }
.muted, .hint, .tile .sub, .hist .sub, .empty { color: var(--text-muted); }
.hint { font-size: 12px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; margin-bottom: 12px; }
.card, .tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 12px; margin-bottom: 16px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-bottom: 16px; }
.tile { padding: 10px 12px; margin: 0; }
.tile .label { color: var(--text-secondary); font-size: 12px; display: flex; align-items: center; gap: 6px; }
.tile .value { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .sub { font-size: 12px; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 3px; flex: none; }
.controls, .group, .legend { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; margin-bottom: 8px; }
.group { gap: 4px; margin: 0; }
button, select, input:not([type=checkbox]) { font: inherit; color: var(--text-primary); background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; padding: 3px 8px; }
button { cursor: pointer; }
button:hover { border-color: var(--text-muted); }
button.on { background: var(--blue); border-color: var(--blue); color: #fff; }
#filter { width: 200px; }
#minDur { width: 64px; }
.legend { gap: 4px 12px; color: var(--text-secondary); font-size: 12px; margin: 0; }
.legend > * { display: inline-flex; align-items: center; gap: 5px; user-select: none; }
.legend label { cursor: pointer; }
.legend input { margin: 0; }
#overview { width: 100%; height: 34px; display: block; cursor: crosshair; margin-bottom: 6px; touch-action: none; }
#scroller { position: relative; overflow: hidden auto; border-top: 1px solid var(--border); }
#tl { position: sticky; top: 0; display: block; width: 100%; cursor: grab; touch-action: pan-y; }
#tl.dragging { cursor: grabbing; }
#tip { position: fixed; pointer-events: none; z-index: 10; display: none; background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 4px 16px rgba(0,0,0,0.18); padding: 8px 10px; max-width: min(460px, calc(100vw - 16px)); font-size: 12px; }
@media (hover: none) { .hint-mouse { display: none; } }
@media (hover: hover) { .hint-touch { display: none; } }
#tip .row { display: flex; gap: 6px; align-items: baseline; }
#details:not(.show) { display: none; }
#details table { border-collapse: collapse; width: 100%; }
#details td { padding: 2px 8px 2px 0; vertical-align: top; }
#details td:first-child { color: var(--text-secondary); white-space: nowrap; width: 1%; }
.bd-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(380px, 100%), 1fr)); gap: 16px; }
.bd-grid .card { margin: 0; }
.bd-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
.seg { display: inline-flex; }
.seg button { border-radius: 0; margin-left: -1px; font-size: 12px; padding: 2px 8px; }
.seg button:first-child { border-radius: 6px 0 0 6px; }
.seg button:last-child { border-radius: 0 6px 6px 0; }
.hist { display: grid; grid-template-columns: minmax(0, 1fr) minmax(60px, 38%) auto; gap: 3px 8px; align-items: center; }
.hist .lbl { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.hist .lbl:hover { text-decoration: underline; }
.hist .active { font-weight: 600; }
.fill { height: 12px; border-radius: 0 4px 4px 0; display: flex; overflow: hidden; }
.val { text-align: right; font-variant-numeric: tabular-nums; color: var(--text-secondary); white-space: nowrap; font-size: 12px; }
.more { margin-top: 8px; font-size: 12px; }
.empty { font-style: italic; }
@media (max-width: 600px) { #filter { width: 140px; } }
</style>
</head>
<body>
<header><h1>powerlog</h1><span id="meta" class="secondary"></span><span id="range" class="muted"></span></header>
<div class="tiles" id="tiles"></div>
<section class="card">
  <div class="controls">
    <div class="group">
      <button data-range="0">All</button><button data-range="86400">24h</button><button data-range="21600">6h</button><button data-range="3600">1h</button>
      <button data-zoom="2" title="Zoom out (-)">&minus;</button><button data-zoom="0.5" title="Zoom in (+)">+</button>
    </div>
    <div class="group">
      <label class="secondary" for="groupBy">Rows</label>
      <select id="groupBy"><option value="proc">by process</option><option value="type">by assertion type</option><option value="proctype">by process + type</option></select>
    </div>
    <div class="group">
      <input type="search" id="filter" placeholder="Filter assertions">
      <label class="secondary" for="minDur">min</label>
      <input type="number" id="minDur" min="0" value="0" title="Hide assertions held less than this many seconds"><span class="secondary">s</span>
    </div>
  </div>
  <div class="controls"><div class="legend" id="stateLegend"></div><div class="legend" id="catLegend"></div></div>
  <canvas id="overview" aria-label="Overview of the full log; drag to select a range"></canvas>
  <div id="scroller"><canvas id="tl" role="img" aria-label="Power state timeline"></canvas><div id="spacer"></div></div>
  <p class="hint hint-touch">Tap for info, tap again for details · drag sideways to pan · pinch to zoom · drag on the overview strip to select a range</p>
  <p class="hint hint-mouse">Drag to pan · pinch, &#8997;/&#8984;+scroll or +/- to zoom · shift+scroll to pan · scroll for more rows · drag on the overview strip to select a range · double-click to zoom out fully · click an item for details</p>
</section>
<section class="card" id="details"><div class="bd-head"><h2 id="detailsTitle"></h2><button id="detailsClose">Close</button></div><div id="detailsBody"></div></section>
<section>
  <div class="bd-head">
    <h2>Breakdown <span class="muted" id="bdRange" style="font-weight:400"></span></h2>
    <span class="hint">Counts cover the visible time range. Click a row to filter or highlight it in the timeline.</span>
  </div>
  <div class="bd-grid" id="bd"></div>
</section>
<div id="tip"></div>
<script>
const DATA = /*__POWERLOG_DATA__*/null;
</script>
<script>
(() => {
"use strict";
const D = DATA, M = D.meta, TZ = M.tz * 60000, DAY = 86400000;
const T0 = M.first, T1 = Math.max(M.last, M.first + 60000);
const $ = (id) => document.getElementById(id);

// ---------- formatting ----------
const pad = (n) => String(n).padStart(2, "0");
const local = (ms) => new Date(ms + TZ);
const fmtTime = (ms, secs = true) => { const d = local(ms); return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}${secs ? ":" + pad(d.getUTCSeconds()) : ""}`; };
const fmtDate = (ms) => local(ms).toLocaleDateString("en-US", { timeZone: "UTC", weekday: "short", month: "short", day: "numeric" }).replace(",", "");
const fmtFull = (ms) => `${fmtDate(ms)} ${fmtTime(ms)}`;
function fmtDur(ms) {
  const s = Math.round(ms / 1000), d = Math.floor(s / 86400), h = Math.floor(s / 3600) % 24, m = Math.floor(s / 60) % 60;
  return d ? `${d}d ${h}h` : h ? `${h}h ${pad(m)}m` : m ? `${m}m ${pad(s % 60)}s` : `${s}s`;
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const sw = (c) => `<span class="swatch" style="background:var(${c})"></span>`;
const legendItem = ([label, c]) => `<span>${sw(c)}${label}</span>`;
// canvas needs resolved colors; CSS vars use light-dark(), so resolve them through a probe element
const probe = document.body.appendChild(document.createElement("i"));
probe.hidden = true;
let colors = {};
const col = (v) => colors[v] ??= (probe.style.color = `var(${v})`, getComputedStyle(probe).color);

// merged length of intervals clipped to [t0, t1]
function unionMs(ivs, t0 = T0, t1 = T1) {
  const xs = ivs.map((a) => [Math.max(a.s, t0), Math.min(a.e, t1)]).filter(([s, e]) => e > s).sort((a, b) => a[0] - b[0]);
  let tot = 0, end = -Infinity;
  for (const [s, e] of xs) if (e > end) { tot += e - Math.max(s, end); end = e; }
  return tot;
}

// ---------- model ----------
const STATES = { awake: ["Awake", "--blue"], dark: ["Dark wake", "--st-dark"], sleep: ["Asleep", "--st-sleep"], unknown: ["No data (off?)", "--st-unknown"] };
const CATS = {
  system: ["Prevents system sleep", "--blue"], display: ["Prevents display sleep", "--orange"], activity: ["User / system activity", "--green"],
  background: ["Background / maintenance", "--amber"], other: ["Other", "--text-muted"],
};
function catOf(type) {
  if (/Display/i.test(type)) return "display";
  if (/IsActive|NetworkClientActive/i.test(type)) return "activity";
  if (/Background|PushService|Maintenance|SoftwareUpdate|DarkWake/i.test(type)) return "background";
  return /Sleep/i.test(type) ? "system" : "other";
}
const A = D.assertions;
const assertions = A.rows.map((r) => {
  const a = {};
  A.cols.forEach((c, j) => { a[c] = j < A.nums ? r[j] : A.strings[r[j]]; });
  a.cat = catOf(a.type);
  return a;
});
const transitions = D.transitions;
const isWake = (t) => t.state === "awake" || t.state === "dark";
// a "clipped" transition is the state carried in from before --since, not an event in range
const events = transitions.filter((t) => !t.clipped);
// the wake request due next when the system went to sleep, keyed to the wake that followed
transitions.forEach((tr, i) => { const nx = transitions[i + 1]; if (nx && isWake(nx) && tr.wakeRequests) nx.scheduled = tr.wakeRequests.find((w) => w.next); });
const runs = (pts, on) => pts.flatMap((p, i) => on(p) ? [{ s: p.t, e: pts[i + 1]?.t ?? T1 }] : []);
const displayOn = runs(D.display, (p) => p.on);
if (D.display.length && !D.display[0].on) displayOn.unshift({ s: T0, e: D.display[0].t });
const acOn = runs(D.battery, (p) => p.ac);
const stateAt = (t) => D.intervals.find((iv) => iv.s <= t && iv.e >= t);
function wakeCategory(r = "") {
  // the token after the final "/" is powerd's high-level reason; when empty, use the last driver before it
  const i = r.lastIndexOf("/");
  return (i < 0 ? r : r.slice(i + 1).trim() || r.slice(0, i).trim().split(/\s+/).at(-1)) || "unknown";
}

const S = { v0: T0, v1: T1, groupBy: "proc", filter: "", minDur: 0, cats: new Set(Object.keys(CATS)), highlight: null, hover: null };

// ---------- header, tiles, legends ----------
$("meta").textContent = [M.host, M.model, M.os && `macOS ${M.os}`].filter(Boolean).join(" · ") || M.source;
$("range").textContent = `${fmtFull(T0)} - ${fmtFull(T1)} (${fmtDur(T1 - T0)})`;
if (M.host) document.title = `powerlog · ${M.host}`;

function renderTiles() {
  const { v0: t0, v1: t1 } = S, span = t1 - t0, tot = { awake: 0, dark: 0, sleep: 0, unknown: 0 };
  for (const iv of D.intervals) tot[iv.state] += Math.max(0, Math.min(iv.e, t1) - Math.max(iv.s, t0));
  const n = (st) => events.filter((t) => t.state === st && t.t >= t0 && t.t <= t1).length;
  const as = assertions.filter((a) => a.e >= t0 && a.s <= t1);
  const tile = (label, value, sub, c) => `<div class="tile"><div class="label">${c ? sw(c) : ""}${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  const st = (k) => tile(STATES[k][0], fmtDur(tot[k]), `${Math.round((tot[k] / span) * 100)}% of range`, STATES[k][1]);
  $("tiles").innerHTML = st("awake") + st("dark") + st("sleep") + tile("Sleeps", n("sleep"), `${n("awake")} full wakes`) +
    tile("Dark wakes", n("dark"), `${(n("dark") / (span / 3600000)).toFixed(1)} per hour`) +
    tile("Assertions", as.length.toLocaleString(), `${new Set(as.map((a) => a.proc)).size} processes`);
}
$("stateLegend").innerHTML = [...Object.values(STATES), ["Display on", "--amber"], ["SleepService window", "--sleepsvc"]].map(legendItem).join("");
$("catLegend").innerHTML = Object.entries(CATS).map(([k, [label, c]]) => `<label><input type="checkbox" data-cat="${k}" checked>${sw(c)}${label}</label>`).join("");
$("catLegend").onchange = (e) => { const k = e.target.dataset.cat; e.target.checked ? S.cats.add(k) : S.cats.delete(k); refresh(); };

// ---------- rows ----------
let rows = [];
function passes(a, useText = true) {
  if (!S.cats.has(a.cat) || a.e - a.s < S.minDur * 1000) return false;
  return !useText || !S.filter || `${a.proc} ${a.type} ${a.name} ${a.pid}`.toLowerCase().includes(S.filter.toLowerCase());
}
function rebuildRows() {
  const map = new Map(), g = S.groupBy;
  for (const a of assertions) {
    if (!passes(a)) continue;
    const k = g === "type" ? a.type : g === "proc" ? a.proc : `${a.proc}\0${a.type}`;
    if (!map.has(k)) map.set(k, { label: g === "type" ? a.type : a.proc, sub: g === "proctype" ? a.type : "", items: [] });
    map.get(k).items.push(a);
  }
  rows = [...map.values()];
  rows.forEach((r) => { r.total = unionMs(r.items); });
  rows.sort((a, b) => b.total - a.total);
  layout(); draw();
}
const refresh = () => { rebuildRows(); renderBreakdown(); };
function setFilter(v, toggle = true) {
  S.filter = $("filter").value = toggle && S.filter === v ? "" : v;
  scroller.scrollTop = 0;
  refresh();
}

// ---------- canvas ----------
const AXIS_H = 26, ROW_H = 16, GAP = 6, OV_H = 34, FONT = "12px -apple-system, BlinkMacSystemFont, sans-serif";
const lane = {};
let laneY = AXIS_H + 4;
for (const [id, label, h] of [["state", "Power state", 24], ["display", "Display", 12], ["sleepsvc", "SleepService", 10], ["battery", "Battery", 38], ["events", "Wakes & events", 18]]) {
  lane[id] = { y: laneY, h, label };
  laneY += h + GAP;
}
const fixedH = laneY + 22;
const tl = $("tl"), ctx = tl.getContext("2d"), ov = $("overview"), octx = ov.getContext("2d"), scroller = $("scroller"), tip = $("tip");
let W = 0, H = 0, dpr = 1, LABEL_W = 220, DUR_W = 64;

function layout() {
  const total = fixedH + rows.length * ROW_H + 24;
  H = Math.min(total, Math.max(380, innerHeight - 240));
  scroller.style.height = H + "px";
  $("spacer").style.height = total - H + "px";
  dpr = devicePixelRatio || 1;
  W = scroller.clientWidth;
  [LABEL_W, DUR_W] = W < 600 ? [140, 30] : [220, 64];
  tl.width = W * dpr; tl.height = H * dpr; tl.style.height = H + "px";
  ov.width = ov.clientWidth * dpr; ov.height = OV_H * dpr;
}
const plotW = () => Math.max(50, W - LABEL_W - 8);
const xOf = (t) => LABEL_W + ((t - S.v0) / (S.v1 - S.v0)) * plotW();
const tOf = (x) => S.v0 + ((x - LABEL_W) / plotW()) * (S.v1 - S.v0);
const isMidnight = (t) => (((t + TZ) % DAY) + DAY) % DAY === 0;
const TICKS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
function ticks(minPx) {
  const step = 1000 * (TICKS.find((s) => ((s * 1000) / (S.v1 - S.v0)) * plotW() >= minPx) || 604800), out = [];
  for (let t = Math.ceil((S.v0 + TZ) / step) * step - TZ; t <= S.v1; t += step) out.push(t);
  return { step, out };
}
function line(x0, y0, x1, y1) { ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke(); }
const vline = (x, y0, y1) => line(Math.round(x) + 0.5, y0, Math.round(x) + 0.5, y1);
function text(s, x, y, c, align = "left", font = FONT) { ctx.font = font; ctx.fillStyle = col(c); ctx.textAlign = align; ctx.fillText(s, x, y); }
function shape(x, y, pts) { ctx.beginPath(); for (const [dx, dy] of pts) ctx.lineTo(x + dx, y + dy); ctx.fill(); }
function clip(x, y, w, h) { ctx.save(); ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip(); }
const fitCache = new Map();
function fitText(s, w) {
  const k = s + "|" + w;
  if (!fitCache.has(k)) {
    let lo = 0, hi = s.length;
    if (ctx.measureText(s).width <= w) lo = hi;
    else while (lo < hi) { const mid = (lo + hi + 1) >> 1; ctx.measureText(s.slice(0, mid) + "…").width <= w ? (lo = mid) : (hi = mid - 1); }
    fitCache.set(k, lo === s.length ? s : s.slice(0, lo) + "…");
  }
  return fitCache.get(k);
}
function drawIntervals(list, y, h, colorFn) {
  // coalesce sub-pixel neighbors of the same color so dense ranges stay fast and solid
  let cur = null, cx0 = 0, cx1 = 0;
  const flush = () => { if (cur) { ctx.fillStyle = cur; ctx.fillRect(cx0, y, Math.max(1, cx1 - cx0), h); } };
  for (const it of list) {
    if (it.e < S.v0 || it.s > S.v1) continue;
    const c = colorFn(it), x0 = Math.max(LABEL_W, xOf(it.s)), x1 = Math.min(W, xOf(it.e));
    if (c === cur && x0 <= cx1 + 0.5) { cx1 = Math.max(cx1, x1); continue; }
    flush();
    cur = c; cx0 = x0; cx1 = Math.max(x1, x0 + 1);
  }
  flush();
}

function draw() {
  if (!W) return;
  const { v0: t0, v1: t1 } = S, scrollY = scroller.scrollTop, tk = ticks(90);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = col("--surface-1"); ctx.fillRect(0, 0, W, H);
  ctx.textBaseline = "middle"; ctx.font = FONT; ctx.lineWidth = 1;
  ctx.globalAlpha = 0.5;
  for (let d = Math.floor((t0 + TZ) / DAY) * DAY - TZ; d < t1; d += DAY) {
    if (Math.round((d + TZ) / DAY) % 2 === 0) drawIntervals([{ s: d, e: d + DAY }], AXIS_H, H - AXIS_H, () => col("--surface-2"));
  }
  ctx.globalAlpha = 1;
  ctx.strokeStyle = col("--grid");
  for (const t of tk.out) vline(xOf(t), AXIS_H, H);

  // assertion rows, virtually scrolled under the fixed lanes
  clip(0, fixedH, W, H - fixedH);
  const first = Math.max(0, Math.floor(scrollY / ROW_H) - 1);
  for (let i = first; i < Math.min(rows.length, first + Math.ceil(H / ROW_H) + 2); i++) {
    const r = rows[i], y = fixedH + i * ROW_H - scrollY;
    if (S.hover?.row === i) { ctx.fillStyle = col("--grid"); ctx.fillRect(0, y, W, ROW_H); }
    drawIntervals(r.items, y + 3, ROW_H - 6, (a) => col(CATS[a.cat][1]));
    text(fitText(r.label, LABEL_W - DUR_W - 24), 8, y + ROW_H / 2, "--text-primary");
    text(fitText(r.sub || (DUR_W < 64 ? fmtDur(r.total).split(" ")[0] : fmtDur(r.total)), DUR_W), LABEL_W - 8, y + ROW_H / 2, "--text-muted", "right");
  }
  if (!rows.length) text("No assertions match the current filters.", LABEL_W + 8, fixedH + 14, "--text-muted");
  ctx.restore();

  ctx.strokeStyle = col("--border");
  line(0, fixedH - 0.5, W, fixedH - 0.5);
  line(LABEL_W, AXIS_H - 0.5, W, AXIS_H - 0.5);
  for (const t of tk.out) {
    const x = xOf(t), mid = isMidnight(t);
    if (x + 4 > LABEL_W && x < W - 30) text(tk.step >= DAY || mid ? fmtDate(t) : fmtTime(t, tk.step < 60000), x + 4, AXIS_H / 2, "--text-secondary", "left", mid ? "600 " + FONT : FONT);
  }
  if (tk.step < DAY) text(fmtDate(t0), 8, AXIS_H / 2, "--text-primary");
  for (const l of Object.values(lane)) text(l.label, 8, l.y + l.h / 2, "--text-secondary");
  text(`Assertions (${rows.length} rows)`, 8, fixedH - 9, "--text-muted");
  const bt = lane.battery, yOf = (p) => bt.y + bt.h - 1 - (p / 100) * (bt.h - 2);
  text("100%", LABEL_W - 6, bt.y + 5, "--text-muted", "right", "10px sans-serif");
  text("0%", LABEL_W - 6, bt.y + bt.h - 5, "--text-muted", "right", "10px sans-serif");

  clip(LABEL_W, 0, W - LABEL_W, fixedH);
  drawIntervals(D.intervals, lane.state.y, lane.state.h, (iv) => col(STATES[iv.state][1]));
  drawIntervals(displayOn, lane.display.y + 2, lane.display.h - 4, () => col("--amber"));
  drawIntervals(D.sleepservice, lane.sleepsvc.y + 2, lane.sleepsvc.h - 4, () => col("--sleepsvc"));
  drawIntervals(acOn, bt.y, bt.h, () => col("--batt-ac"));
  if (D.battery.length) {
    ctx.strokeStyle = col("--text-secondary"); ctx.lineWidth = 1.5; ctx.beginPath();
    for (const b of D.battery) ctx.lineTo(xOf(b.t), yOf(b.pct));
    ctx.lineTo(xOf(T1), yOf(D.battery.at(-1).pct)); ctx.stroke(); ctx.lineWidth = 1;
  }
  const ev = lane.events, cy = ev.y + ev.h / 2;
  for (const tr of events) {
    if (tr.t < t0 || tr.t > t1) continue;
    const x = xOf(tr.t), hit = S.highlight && matchesHighlight(tr, S.highlight);
    ctx.fillStyle = col(hit ? "--highlight" : tr.state === "dark" ? "--st-dark" : tr.state === "awake" ? "--blue" : "--text-primary");
    if (tr.state === "awake") shape(x, cy, [[0, -6], [6, 5], [-6, 5]]);
    else if (tr.state === "dark" && !hit) ctx.fillRect(x - 0.5, ev.y + 4, 1, ev.h - 8);
    else if (tr.state === "boot" || hit) ctx.fillRect(x - 1.5, ev.y, 3, ev.h);
  }
  ctx.fillStyle = col("--text-secondary");
  for (const e of D.events) if (e.t >= t0 && e.t <= t1) shape(xOf(e.t), cy, [[0, -4], [4, 0], [0, 4], [-4, 0]]);
  ctx.restore();

  if (S.hover?.x >= LABEL_W) { ctx.strokeStyle = col("--text-muted"); ctx.setLineDash([3, 3]); vline(S.hover.x, AXIS_H, H); ctx.setLineDash([]); }
  drawOverview();
}

function drawOverview() {
  const ow = ov.clientWidth, x = (t) => LABEL_W + ((t - T0) / (T1 - T0)) * (ow - LABEL_W - 8), x0 = x(S.v0), x1 = x(S.v1);
  octx.setTransform(dpr, 0, 0, dpr, 0, 0);
  octx.clearRect(0, 0, ow, OV_H);
  octx.font = FONT; octx.textBaseline = "middle"; octx.fillStyle = col("--text-secondary");
  octx.fillText("Overview", 8, 12);
  for (const iv of D.intervals) { octx.fillStyle = col(STATES[iv.state][1]); octx.fillRect(x(iv.s), 4, Math.max(0.5, x(iv.e) - x(iv.s)), 16); }
  octx.fillStyle = col("--text-primary"); octx.globalAlpha = 0.12;
  octx.fillRect(LABEL_W, 0, x0 - LABEL_W, OV_H); octx.fillRect(x1, 0, ow - x1, OV_H);
  octx.globalAlpha = 1; octx.strokeStyle = col("--blue"); octx.lineWidth = 2;
  octx.strokeRect(x0, 1, Math.max(2, x1 - x0), OV_H - 2);
  octx.fillStyle = col("--text-muted"); octx.font = "10px -apple-system, sans-serif";
  const every = Math.max(1, Math.ceil((T1 - T0) / DAY / ((ow - LABEL_W) / 70)));
  for (let d = Math.ceil((T0 + TZ) / DAY) * DAY - TZ, n = 0; d < T1; d += DAY, n++) {
    if (n % every === 0 && x(d) < ow - 60) { octx.fillRect(x(d), 20, 1, 4); octx.fillText(fmtDate(d), x(d) + 3, 28); }
  }
}

// ---------- view control ----------
let bdTimer = null;
function scheduleBreakdown() { clearTimeout(bdTimer); bdTimer = setTimeout(() => { renderTiles(); renderBreakdown(); }, 120); }
function setView(v0, v1, settled = true) {
  const span = Math.max(20000, Math.min(T1 - T0, v1 - v0));
  S.v0 = Math.max(T0, Math.min(v0, T1 - span));
  S.v1 = S.v0 + span;
  draw();
  if (settled) scheduleBreakdown();
}
const zoomAt = (t, f, dt = 0) => setView(t + dt - (t - S.v0) * f, t + dt + (S.v1 - t) * f);
const center = () => (S.v0 + S.v1) / 2;
document.querySelectorAll("[data-range]").forEach((b) => b.onclick = () => {
  const span = b.dataset.range * 1000;
  if (!span) setView(T0, T1);
  // from the full view, jump to the most recent span; otherwise zoom around the current center
  else if (S.v0 <= T0 && S.v1 >= T1) setView(T1 - span, T1);
  else setView(center() - span / 2, center() + span / 2);
});
document.querySelectorAll("[data-zoom]").forEach((b) => b.onclick = () => zoomAt(center(), +b.dataset.zoom));
$("groupBy").onchange = (e) => { S.groupBy = e.target.value; scroller.scrollTop = 0; rebuildRows(); };
let filterTimer;
$("filter").oninput = (e) => { clearTimeout(filterTimer); filterTimer = setTimeout(() => setFilter(e.target.value.trim(), false), 150); };
$("minDur").oninput = (e) => { S.minDur = Math.max(0, +e.target.value || 0); refresh(); };
addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea")) return;
  const pan = (S.v1 - S.v0) * 0.2, keys = {
    "+": () => zoomAt(center(), 0.5), "=": () => zoomAt(center(), 0.5), "-": () => zoomAt(center(), 2), "_": () => zoomAt(center(), 2),
    ArrowLeft: () => setView(S.v0 - pan, S.v1 - pan), ArrowRight: () => setView(S.v0 + pan, S.v1 + pan), 0: () => setView(T0, T1), Escape: hideDetails,
  };
  if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
});
scroller.onscroll = () => { S.hover = null; hideTip(); draw(); };
const xy = (e) => { const r = tl.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
// wheel deltas are accumulated and applied once per frame
let wheelQ = null;
tl.addEventListener("wheel", (e) => {
  const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? plotW() : 1, dx = e.deltaX * unit, dy = e.deltaY * unit;
  const zoom = e.ctrlKey || e.altKey || e.metaKey, horiz = Math.abs(dx) > Math.abs(dy);
  if (!zoom && !e.shiftKey && !horiz) return;
  e.preventDefault();
  const q = wheelQ ??= { zoom: 0, pan: 0, raf: requestAnimationFrame(flushWheel) };
  q.x = xy(e)[0];
  // ctrlKey without a held key is a trackpad pinch, whose deltas are small
  if (zoom) { q.zoom += dy * (e.ctrlKey ? 0.01 : 0.005); q.pan += dx; } else q.pan += horiz ? dx : dy;
}, { passive: false });
function flushWheel() {
  const q = wheelQ;
  wheelQ = null; S.hover = null; hideTip();
  zoomAt(q.x >= LABEL_W ? tOf(q.x) : center(), Math.exp(q.zoom), (q.pan / plotW()) * (S.v1 - S.v0));
}
let drag = null, brush = null, pinned = null, lastTouch = false;
const touches = new Map(), capture = (el, e) => { try { el.setPointerCapture(e.pointerId); } catch {} };
const pinchSpan = () => { const [a, b] = touches.values(); return Math.max(1, Math.abs(a - b)); };
tl.onpointerdown = (e) => {
  lastTouch = e.pointerType === "touch";
  if (lastTouch) touches.set(e.pointerId, e.clientX);
  if (touches.size === 2) {
    hideTip();
    const [a, b] = touches.values();
    drag = { pinch: pinchSpan(), mid: tOf((a + b) / 2 - tl.getBoundingClientRect().left), v0: S.v0, v1: S.v1, moved: true };
  } else drag = { x: e.clientX, v0: S.v0, v1: S.v1, moved: false };
  capture(tl, e);
};
tl.onpointermove = (e) => {
  if (touches.has(e.pointerId)) touches.set(e.pointerId, e.clientX);
  if (drag?.pinch) {
    if (touches.size === 2) { const f = drag.pinch / pinchSpan(); setView(drag.mid - (drag.mid - drag.v0) * f, drag.mid + (drag.v1 - drag.mid) * f, false); }
    return;
  }
  if (drag && (drag.moved ||= Math.abs(e.clientX - drag.x) > 3)) {
    tl.classList.add("dragging"); hideTip();
    const dt = ((e.clientX - drag.x) / plotW()) * (drag.v1 - drag.v0);
    return setView(drag.v0 - dt, drag.v1 - dt, false);
  }
  if (e.pointerType !== "touch") hover(...xy(e), e.clientX, e.clientY);
};
tl.onpointerup = tl.onpointercancel = (e) => {
  tl.classList.remove("dragging");
  touches.delete(e.pointerId);
  if (drag?.moved) { scheduleBreakdown(); drag = touches.size ? { pinch: -1, moved: true } : null; return; }
  if (drag && e.type === "pointerup") {
    const hit = hitTest(...xy(e));
    // touch has no hover: the first tap pins the tooltip, a second tap on the same item opens details
    if (e.pointerType !== "touch" || (hasDetails(hit) && sameHit(hit, pinned))) { hideTip(); if (hit) showDetails(hit); }
    else { hover(...xy(e), e.clientX, e.clientY, true); pinned = tip.style.display === "block" ? hit : null; }
  }
  drag = null;
};
tl.onpointerleave = (e) => { if (!drag && e.pointerType !== "touch") { S.hover = null; hideTip(); draw(); } };
tl.ondblclick = () => { if (!lastTouch) setView(T0, T1); };
const dismiss = (e) => { if (pinned && !tl.contains(e.target)) { S.hover = null; hideTip(); draw(); } };
document.addEventListener("pointerdown", dismiss);
addEventListener("scroll", dismiss, { passive: true });
const ovT = (e) => { const r = ov.getBoundingClientRect(); return T0 + ((e.clientX - r.left - LABEL_W) / (r.width - LABEL_W - 8)) * (T1 - T0); };
ov.onpointerdown = (e) => { brush = { t: ovT(e), x: e.clientX }; capture(ov, e); };
ov.onpointermove = (e) => { if (brush && Math.abs(e.clientX - brush.x) >= 3) { const t = ovT(e); setView(Math.min(brush.t, t), Math.max(brush.t, t), false); } };
ov.onpointerup = (e) => {
  if (brush && Math.abs(e.clientX - brush.x) < 3) setView(brush.t - (S.v1 - S.v0) / 2, brush.t + (S.v1 - S.v0) / 2);
  else scheduleBreakdown();
  brush = null;
};

// ---------- hit testing / tooltip ----------
const rowAt = (y) => y >= fixedH ? Math.floor((y - fixedH + scroller.scrollTop) / ROW_H) : -1;
function hitTest(x, y) {
  const i = rowAt(y), r = rows[i], t = tOf(x), tol = (4 / plotW()) * (S.v1 - S.v0);
  const near = (it) => it.s - tol <= t && it.e + tol >= t, inLane = (k) => y >= lane[k].y - 2 && y <= lane[k].y + lane[k].h + 2;
  if (x < LABEL_W) return r ? { kind: "row", row: r } : null;
  if (i >= 0) return r && { kind: "assert", row: r, t, hits: r.items.filter(near).sort((a, b) => b.e - b.s - (a.e - a.s)) };
  if (inLane("events")) {
    const cands = [...events.filter((tr) => tr.state !== "sleep").map((tr) => ({ kind: "transition", tr, at: tr.t })), ...D.events.map((ev) => ({ kind: "event", ev, at: ev.t }))];
    const best = cands.reduce((b, c) => !b || Math.abs(c.at - t) < Math.abs(b.at - t) ? c : b, null);
    return best && Math.abs(best.at - t) <= tol * 1.5 ? { ...best, t } : null;
  }
  if (inLane("state")) { const iv = stateAt(t); if (iv) return { kind: "state", iv, t }; }
  if (inLane("sleepsvc")) { const w = D.sleepservice.find(near); if (w) return { kind: "sleepsvc", w, t }; }
  for (const k of ["display", "battery"]) if (inLane(k)) return { kind: k, t };
  return { kind: "time", t };
}
const hideTip = () => { tip.style.display = "none"; pinned = null; };
const hasDetails = (h) => h && (h.kind === "state" ? h.iv.i != null : h.kind === "assert" ? h.hits.length > 0 : ["row", "transition", "event"].includes(h.kind));
const hitObj = (h) => h.tr || h.ev || h.iv || h.w || h.hits?.[0] || h.row;
const sameHit = (a, b) => a && b && a.kind === b.kind && hitObj(a) === hitObj(b);
function hover(x, y, cx, cy, touch = false) {
  S.hover = { x, row: rowAt(y) };
  draw();
  const hit = hitTest(x, y), html = hit && tipHtml(hit);
  if (!html) return hideTip();
  tip.innerHTML = html + (touch && hasDetails(hit) ? `<hr><div class="muted">Tap again ${hit.kind === "row" ? "to filter" : "for details"}</div>` : "");
  tip.style.display = "block";
  const tw = tip.offsetWidth, th = tip.offsetHeight, clamp = (v, max) => Math.max(8, Math.min(v, max - 8));
  // touch: centered above the finger so it isn't covered
  if (touch) {
    tip.style.left = clamp(cx - tw / 2, innerWidth - tw) + "px";
    tip.style.top = (cy - th - 28 >= 8 ? cy - th - 28 : clamp(cy + 28, innerHeight - th)) + "px";
  } else {
    tip.style.left = Math.max(8, cx + 14 + tw > innerWidth - 8 ? cx - tw - 14 : cx + 14) + "px";
    tip.style.top = Math.max(8, cy + 14 + th > innerHeight - 8 ? cy - th - 14 : cy + 14) + "px";
  }
}
const rowName = (r) => esc(r.label) + (r.sub ? ` <span class="secondary">${esc(r.sub)}</span>` : "");
const stateLine = (st, extra = "") => `<div class="row">${sw(STATES[st.state][1])}<b>${STATES[st.state][0]}</b>${extra}</div>`;
const sched = (r) => r && esc(`${r.proc} ${r.req}${r.info ? " - " + r.info : ""}`);
const wakeLabel = (tr) => tr.state === "awake" ? (tr.promoted ? "DarkWake to full wake" : "Full wake") : tr.state === "dark" ? "Dark wake" : "Boot";
function tipHtml(h) {
  const when = (t) => `<div class="muted">${fmtFull(t)}</div>`, st = stateAt(h.t);
  switch (h.kind) {
    case "state": {
      const { iv } = h, tr = transitions[iv.i];
      return stateLine(iv, ` <span class="muted">${fmtDur(iv.e - iv.s)}</span>`) + `<div class="muted">${fmtFull(iv.s)} - ${fmtTime(iv.e)}</div><hr>` +
        (tr ? `<div>${tr.state === "sleep" ? "Sleep" : "Wake"}: ${esc(tr.reason)}</div>` + (tr.scheduled ? `<div class="secondary">Scheduled by ${sched(tr.scheduled)}</div>` : "")
          : "<div>No log entries; the machine was likely shut down.</div>");
    }
    case "transition": {
      const { tr } = h;
      return `<b>${wakeLabel(tr)}</b>${when(tr.t)}<hr><div>${esc(tr.reason)}</div>` + (tr.scheduled ? `<div class="secondary">Scheduled: ${sched(tr.scheduled)}</div>` : "") +
        (tr.wakeTime != null ? `<div class="muted">Wake time ${tr.wakeTime}s</div>` : "");
    }
    case "event": return `<b>${esc(h.ev.kind)}</b>${when(h.ev.t)}<div>${esc(h.ev.msg)}</div>`;
    case "display": return D.display.length ? `<b>Display ${displayOn.some((iv) => iv.s <= h.t && iv.e >= h.t) ? "on" : "off"}</b>${when(h.t)}` : null;
    case "sleepsvc": return `<b>SleepService window</b><div class="muted">${fmtFull(h.w.s)} - ${fmtTime(h.w.e)} (${fmtDur(h.w.e - h.w.s)})</div>`;
    case "battery": { const b = D.battery.findLast((p) => p.t <= h.t); return b && `<b>${b.pct}%</b> on ${b.ac ? "AC power" : "battery"}<div class="muted">as of ${fmtFull(b.t)}</div>`; }
    case "row": return `<b>${rowName(h.row)}</b><div class="muted">${h.row.items.length.toLocaleString()} assertions · ${fmtDur(h.row.total)} held</div>`;
    case "assert": return `<div class="row"><b>${rowName(h.row)}</b></div>${when(h.t)}${st ? stateLine(st) : ""}` +
      h.hits.slice(0, 6).map((a) => `<hr><div class="row">${sw(CATS[a.cat][1])}<b>${esc(a.type)}</b></div><div>${esc(a.proc)} (${a.pid}) "${esc(a.name)}"</div>
        <div class="muted">${a.inferred ? "≈" : ""}${fmtFull(a.s)} - ${a.end === "ongoing" ? "still held" : a.end === "reboot" ? "until reboot" : `${fmtTime(a.e)} ${esc(a.end)}`} · ${fmtDur(a.e - a.s)}</div>`).join("") +
      (h.hits.length > 6 ? `<hr><div class="muted">+${h.hits.length - 6} more overlapping</div>` : "");
    case "time": return st && stateLine(st) + when(h.t);
  }
}

// ---------- details panel ----------
const hideDetails = () => $("details").classList.remove("show");
$("detailsClose").onclick = hideDetails;
const kv = (pairs) => `<table>${pairs.filter(([, v]) => v != null && v !== "").map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</table>`;
const mono = (s) => s && `<span class="mono">${esc(s)}</span>`;
const lines = (xs) => xs?.map((x) => `<div class="mono">${esc(x)}</div>`).join("");
const transitionDetails = (tr) => kv([
  ["Time", fmtFull(tr.t)], ["Message", mono(tr.msg)], ["Reason", esc(tr.reason)], ["From", esc(tr.from)],
  ["Scheduled wake", sched(tr.scheduled)], ["Driver reasons", lines(tr.drivers)],
  ["Wake time", tr.wakeTime != null ? `${tr.wakeTime}s` : null], ["Slept", tr.slept != null ? fmtDur(tr.slept * 1000) : null], ["Hibernate", mono(tr.hib)],
  ["Pending wake requests", lines(tr.wakeRequests?.map((w) => `${w.next ? "* " : "  "}${w.at}  ${w.proc} ${w.req}${w.info ? " - " + w.info : ""}`))],
  ["Slow PM clients", lines(tr.pmAcks?.map((a) => `${a.ms} ms  ${a.who}`))],
  ["Slow kernel clients", lines(tr.kernelAcks?.map((a) => `${a.ms} ms  ${a.who}${a.msg ? ` (${a.msg})` : ""}`))],
]);
function showDetails(hit) {
  let title, body;
  if (hit.kind === "row") return setFilter(hit.row.label);
  if (hit.kind === "state" && hit.iv.i != null) {
    const { iv } = hit, prev = transitions[iv.i - 1];
    title = `${STATES[iv.state][0]} · ${fmtFull(iv.s)} (${fmtDur(iv.e - iv.s)})`;
    // pending wake requests are recorded on the sleep before a wake
    body = transitionDetails(transitions[iv.i]) + (prev?.state === "sleep" ? `<h3 style="margin-top:12px">Preceding sleep</h3>${transitionDetails(prev)}` : "");
  } else if (hit.kind === "transition") {
    title = `${wakeLabel(hit.tr)} · ${fmtFull(hit.tr.t)}`;
    body = transitionDetails(hit.tr);
  } else if (hit.kind === "assert" && hit.hits.length) {
    const n = hit.hits.length;
    title = `${hit.row.label}${hit.row.sub ? " · " + hit.row.sub : ""} · ${n} assertion${n > 1 ? "s" : ""} at ${fmtFull(hit.t)}`;
    body = hit.hits.slice(0, 50).map((a) => kv([
      ["Type", `${sw(CATS[a.cat][1])} ${esc(a.type)} <span class="muted">(${CATS[a.cat][0]})</span>`],
      ["Process", `${esc(a.proc)} (pid ${a.pid})`], ["Name", mono(a.name)],
      ["Start", `${a.inferred ? "≈ " : ""}${fmtFull(a.s)}${a.inferred ? ' <span class="muted">(derived from duration; creation not in log)</span>' : ""}`],
      ["End", a.end === "ongoing" ? "still held at end of log" : `${fmtFull(a.e)} (${a.end === "reboot" ? "last log entry before reboot" : esc(a.end)})`],
      ["Duration", fmtDur(a.e - a.s)], ["ID", mono(a.id)], ["System at end", mono(a.sys)],
    ])).join("<hr>");
  } else if (hit.kind === "event") {
    title = `${hit.ev.kind} · ${fmtFull(hit.ev.t)}`;
    body = kv([["Message", mono(hit.ev.msg)]]);
  } else return;
  $("detailsTitle").textContent = title;
  $("detailsBody").innerHTML = body;
  $("details").classList.add("show");
}

// ---------- breakdown views ----------
const BD = { wake: "cat", atype: "time", holder: "time", expand: {} };
const wakeKey = (tr, mode = BD.wake) => mode === "full" ? tr.reason : wakeCategory(tr.reason);
const schedKey = (r) => `${r.proc} · ${r.req}${r.info ? " · " + r.info : ""}`;
function matchesHighlight(tr, { kind, key, mode }) {
  if (kind === "wake") return isWake(tr) && wakeKey(tr, mode) === key;
  if (kind === "sched") return tr.scheduled && schedKey(tr.scheduled) === key;
  return tr.state === "sleep" && tr.reason === key;
}
function group(list, keyFn) {
  const m = new Map();
  for (const x of list) { const k = keyFn(x); m.has(k) ? m.get(k).items.push(x) : m.set(k, { key: k, label: k, items: [x] }); }
  return [...m.values()];
}
// items: {key, label, sub?, title?, value, parts?: [[cssVar, weight]]}
function histogram(id, title, subtitle, items, o = {}) {
  items.sort((a, b) => b.value - a.value);
  const max = Math.max(1, ...items.map((x) => x.value)), shown = BD.expand[id] ? items : items.slice(0, 12), card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `<div class="bd-head"><div><h3>${title}</h3><div class="hint">${subtitle}</div></div>${o.seg ? `<div class="seg">${o.seg.map(([k, l]) => `<button data-seg="${k}" class="${BD[id] === k ? "on" : ""}">${l}</button>`).join("")}</div>` : ""}</div>
    ${o.legend ? `<div class="legend" style="margin-bottom:6px">${o.legend}</div>` : ""}
    ${items.length ? `<div class="hist">${shown.map((x, i) => `<div class="lbl${o.active === x.key ? " active" : ""}" data-i="${i}" title="${esc(x.title || x.label)}">${esc(x.label)}${x.sub ? ` <span class="sub">${esc(x.sub)}</span>` : ""}</div>
      <div><div class="fill" style="width:${Math.max(0.5, (x.value / max) * 100)}%">${(x.parts || [["--blue", 1]]).map(([c, v]) => `<span style="flex:${v};background:var(${c})"></span>`).join("")}</div></div>
      <div class="val">${o.fmt ? o.fmt(x) : x.value.toLocaleString()}</div>`).join("")}</div>` : `<div class="empty">Nothing in this range.</div>`}
    ${items.length > 12 ? `<div class="more"><button data-more>${BD.expand[id] ? "Show top 12" : `Show all ${items.length}`}</button></div>` : ""}`;
  card.querySelectorAll("[data-seg]").forEach((b) => b.onclick = () => { BD[id] = b.dataset.seg; if (id === "wake") { S.highlight = null; draw(); } renderBreakdown(); });
  if (o.onClick) card.querySelectorAll(".lbl").forEach((el) => el.onclick = () => o.onClick(shown[el.dataset.i]));
  card.querySelector("[data-more]")?.addEventListener("click", () => { BD.expand[id] = !BD.expand[id]; renderBreakdown(); });
  $("bd").append(card);
}

function renderBreakdown() {
  const { v0: t0, v1: t1 } = S, inR = (t) => t.t >= t0 && t.t <= t1, hl = S.highlight;
  $("bdRange").textContent = `${fmtFull(t0)} - ${fmtFull(t1)}`;
  $("bd").innerHTML = "";
  const highlight = (kind, mode) => ({
    active: hl?.kind === kind ? hl.key : null,
    onClick: (x) => { S.highlight = hl?.kind === kind && hl.key === x.key ? null : { kind, key: x.key, mode }; draw(); renderBreakdown(); },
  });
  const wakeLegend = legendItem(["Full wake", "--blue"]) + legendItem(["Dark wake", "--st-dark"]);
  const byWakeType = (r) => {
    const full = r.items.filter((t) => t.state === "awake").length;
    return Object.assign(r, { value: r.items.length, parts: [["--blue", full], ["--st-dark", r.items.length - full]] });
  };
  const wk = events.filter((t) => isWake(t) && inR(t));
  histogram("wake", "Wake reasons", `${wk.length} wakes`, group(wk, (t) => wakeKey(t)).map(byWakeType),
    { seg: [["cat", "Category"], ["full", "Full reason"]], legend: wakeLegend, ...highlight("wake", BD.wake) });
  histogram("sched", "Scheduled wake requests", "Earliest pending request when the system went to sleep",
    group(wk.filter((t) => t.scheduled), (t) => schedKey(t.scheduled)).map((r) => {
      const s = r.items[0].scheduled;
      return Object.assign(byWakeType(r), { label: `${s.proc} · ${s.req}`, sub: s.info, title: r.key });
    }), { legend: wakeLegend, ...highlight("sched") });
  const sl = events.filter((t) => t.state === "sleep" && inR(t));
  histogram("sleep", "Sleep reasons", `${sl.length} sleeps`, group(sl, (t) => t.reason).map((r) => Object.assign(r, { value: r.items.length })),
    { fmt: (x) => `${x.value} · ${fmtDur(x.items.reduce((s, t) => s + (t.slept || 0), 0) * 1000)} asleep`, ...highlight("sleep") });

  // the text filter is what these lists set, so it isn't applied to them
  const as = assertions.filter((a) => a.e >= t0 && a.s <= t1 && passes(a, false));
  const held = (r, metric) => { r.held = unionMs(r.items, t0, t1); r.value = metric === "time" ? r.held : r.items.length; return r; };
  const metric = (id) => ({
    seg: [["time", "Time held"], ["count", "Count"]], active: S.filter, onClick: (x) => setFilter(x.key),
    fmt: (x) => BD[id] === "time" ? `${fmtDur(x.held)} · ${x.items.length.toLocaleString()}×` : `${x.value.toLocaleString()} · ${fmtDur(x.held)}`,
  });
  histogram("atype", "Assertion types", "Time held merges overlapping assertions",
    group(as, (a) => a.type).map((r) => Object.assign(held(r, BD.atype), { parts: [[CATS[r.items[0].cat][1], 1]] })),
    { ...metric("atype"), legend: Object.values(CATS).map(legendItem).join("") });
  histogram("holder", "Assertion holders", "Processes, bar split by assertion category", group(as, (a) => a.proc).map((r) => {
    const types = new Set(r.items.map((a) => a.type));
    // each category's share is merged within that category
    r.parts = Object.entries(CATS).map(([k, [, c]]) => { const its = r.items.filter((a) => a.cat === k); return [c, BD.holder === "time" ? unionMs(its, t0, t1) : its.length]; });
    return Object.assign(held(r, BD.holder), { sub: `${types.size} type${types.size > 1 ? "s" : ""}`, title: `${r.label}: ${[...types].join(", ")}` });
  }), metric("holder"));
  histogram("acks", "Slow sleep/wake clients", "Worst reported notification delay",
    group(events.filter(inR).flatMap((t) => [...(t.pmAcks || []), ...(t.kernelAcks || [])]), (a) => a.who).map((r) =>
      Object.assign(r, { value: Math.max(...r.items.map((a) => a.ms)), avg: r.items.reduce((s, a) => s + a.ms, 0) / r.items.length })),
    { fmt: (x) => `${x.value.toLocaleString()} ms max · ${Math.round(x.avg).toLocaleString()} avg` });
}

addEventListener("resize", () => { fitCache.clear(); layout(); draw(); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { colors = {}; draw(); });
refresh();
renderTiles();
})();
</script>
</body>
</html>
'''

if __name__ == "__main__":
    main()
