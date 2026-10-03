import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixture.log"
sys.path.insert(0, str(ROOT))
import powerlog  # noqa: E402

# the fixture is -0700; CLI --since/--until are parsed as local time
CLI_ENV = {**os.environ, "TZ": "America/Los_Angeles"}


def unpack_assertions(p):
    return [{c: v if j < p["nums"] else p["strings"][v] for j, (c, v) in enumerate(zip(p["cols"], row))} for row in p["rows"]]


def to_data(model):
    data = model.to_json({})
    data["assertions"] = unpack_assertions(data["assertions"])
    return data


def fixture_model():
    return powerlog.parse(FIXTURE.read_text().splitlines())


def ts(s):
    return int(powerlog.parse_ts(s + " -0700").timestamp() * 1000)


def entry(time, domain, msg):
    return f"2026-09-26 {time} -0700 {domain:<20}\t{msg}\t"


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = to_data(fixture_model())

    def test_meta_range(self):
        m = self.d["meta"]
        self.assertEqual((m["first"], m["last"], m["tz"]), (ts("2026-09-26 13:00:00"), ts("2026-09-26 14:10:00"), -420))

    def test_transitions(self):
        states = [t["state"] for t in self.d["transitions"]]
        self.assertEqual(states, ["sleep", "dark", "sleep", "awake", "boot"])
        dark = self.d["transitions"][1]
        self.assertEqual(dark["reason"], "NUB.SPMI0Sw3IRQ rtc/SleepService")
        self.assertEqual(dark["from"], "Deep Idle")
        self.assertFalse(dark["promoted"])
        self.assertEqual(dark["drivers"], ["NUB.SPMI0Sw3IRQ", "rtc"])
        self.assertEqual(dark["wakeTime"], 0.306)

    def test_sleep_details(self):
        sl = self.d["transitions"][0]
        self.assertEqual(sl["reason"], "Maintenance Sleep")
        self.assertEqual(sl["slept"], 600)
        self.assertEqual(sl["wakeRequests"][0], {"next": True, "proc": "dasd", "req": "SleepService", "delta": 590,
                                                 "at": "2026-09-26 13:12:00", "info": "com.apple.dasd:0:idleCheck"})
        self.assertEqual(sl["wakeRequests"][1]["proc"], "powerd")
        self.assertFalse(sl["wakeRequests"][1]["next"])
        self.assertEqual(sl["pmAcks"], [{"who": "mDNSResponder", "msg": None, "ms": 2637},
                                        {"who": "com.apple.bluetooth.sleep", "msg": None, "ms": 1540}])

    def test_intervals_mark_gap_before_boot(self):
        ivs = self.d["intervals"]
        self.assertEqual([i["state"] for i in ivs], ["sleep", "dark", "sleep", "awake", "unknown", "awake"])
        gap = ivs[4]
        self.assertEqual(gap["s"], ts("2026-09-26 13:40:00"))
        self.assertEqual(gap["e"], ts("2026-09-26 14:00:00"))
        self.assertIsNone(gap["i"])
        self.assertEqual(ivs[-1]["e"], ts("2026-09-26 14:10:00"))
        for a, b in zip(ivs, ivs[1:]):
            self.assertEqual(a["e"], b["s"])

    def by_proc(self, proc):
        return [a for a in self.d["assertions"] if a["proc"] == proc]

    def test_created_released_pair(self):
        (a,) = self.by_proc("apsd")
        self.assertEqual((a["s"], a["e"], a["end"]), (ts("2026-09-26 13:01:00"), ts("2026-09-26 13:12:02"), "Released"))
        self.assertEqual((a["pid"], a["type"], a["name"], a["sys"]),
                         (402, "NoIdleSleepAssertion", "com.apple.apsd-outgoing", "PrevIdle"))
        self.assertFalse(a["inferred"])

    def test_release_without_create_uses_duration(self):
        (a,) = self.by_proc("coreaudiod")
        self.assertEqual((a["s"], a["e"]), (ts("2026-09-26 12:50:00"), ts("2026-09-26 13:00:00")))
        self.assertTrue(a["inferred"])

    def test_summary_opens_and_reboot_closes(self):
        # the trailing "currently held" listing must not add a third
        old, new = sorted(self.by_proc("nfsd"), key=lambda a: a["s"])
        self.assertEqual(old["s"], ts("2026-09-26 13:29:00") - 100 * 3600 * 1000)
        self.assertTrue(old["inferred"])
        self.assertEqual(old["end"], "reboot")
        self.assertEqual(old["e"], ts("2026-09-26 13:40:00"))
        self.assertEqual((new["s"], new["e"], new["end"]), (ts("2026-09-26 14:05:00"), ts("2026-09-26 14:10:00"), "ongoing"))

    def test_client_died(self):
        (a,) = self.by_proc("caffeinate")
        self.assertEqual(a["end"], "ClientDied")
        self.assertEqual(a["e"] - a["s"], 240000)

    def test_display_sleepservice_battery(self):
        self.assertEqual([x["on"] for x in self.d["display"]], [True, False])
        self.assertEqual(self.d["sleepservice"], [{"s": ts("2026-09-26 13:12:00"), "e": ts("2026-09-26 13:12:02")}])
        self.assertEqual(self.d["battery"][0], {"t": ts("2026-09-26 13:02:00"), "ac": False, "pct": 89})
        self.assertEqual(self.d["battery"][-1], {"t": ts("2026-09-26 13:28:42"), "ac": True, "pct": 88})
        self.assertEqual(self.d["events"], [])


class ParseEdgeTests(unittest.TestCase):
    def test_client_acks_keep_worst_per_client(self):
        m = powerlog.parse([
            entry("10:00:00", "Sleep", "Entering Sleep state due to 'Idle Sleep' Using AC (Charge:50%) 30 secs"),
            entry("10:00:01", "PM Client Acks", "Delays: [a is slow(100 ms)] [b is slow(msg: 0xe0000280)(300 ms)]"),
            entry("10:00:02", "PM Client Acks", "Delays: [a is slow(500 ms)] [a is slow(50 ms)]"),
            entry("10:00:02", "Kernel Client Acks", "Delays: [AppleFoo driver is slow(msg: SetState to 0)(20 ms)]"),
        ])
        (tr,) = m.transitions
        self.assertEqual(tr["pmAcks"], [{"who": "a", "msg": None, "ms": 500}, {"who": "b", "msg": "0xe0000280", "ms": 300}])
        self.assertEqual(tr["kernelAcks"], [{"who": "AppleFoo driver", "msg": "SetState to 0", "ms": 20}])

    def test_promoted_wake_and_misc_domains(self):
        m = powerlog.parse([
            entry("10:00:00", "DarkWake", "DarkWake to FullWake from Deep Idle [CDNVA] : due to UserActivity Assertion Using AC (Charge:50%)"),
            entry("10:00:01", "HibernateStats", "hibmode=3"),
            entry("10:00:02", "BatteryHealth", "Battery health is Check Battery"),
            entry("10:00:03", "com.apple.sleepservices.sessionStarted", "SleepService: window begins"),
            entry("10:00:09", "Assertions", 'PID 1(x) Created PreventUserIdleSystemSleep "n" 00:00:00  id:0x1 [System: PrevIdle]'),
        ])
        (tr,) = m.transitions
        self.assertEqual((tr["state"], tr["reason"], tr["promoted"], tr["hib"]), ("dark", "UserActivity Assertion", True, "hibmode=3"))
        self.assertEqual(m.events, [{"t": ts("2026-09-26 10:00:02"), "kind": "Battery health", "msg": "Battery health is Check Battery"}])
        # unterminated window and assertion close at the last entry
        self.assertEqual(m.sleepservice, [{"s": ts("2026-09-26 10:00:03"), "e": ts("2026-09-26 10:00:09")}])
        self.assertEqual([(a["proc"], a["end"]) for a in m.assertions], [("x", "ongoing")])

    def test_empty(self):
        m = powerlog.parse(["PM ASL data store: /var/log/powermanagement", "", "garbage"])
        self.assertIsNone(m.first)


class ClipTests(unittest.TestCase):
    def test_since_until(self):
        m = fixture_model()
        lo, hi = ts("2026-09-26 13:20:00"), ts("2026-09-26 13:50:00")
        m.clip(lo, hi)
        d = to_data(m)
        self.assertEqual((d["meta"]["first"], d["meta"]["last"]), (lo, hi))
        self.assertEqual([(t["state"], t.get("clipped", False)) for t in d["transitions"]], [("sleep", True), ("awake", False)])
        self.assertEqual(d["transitions"][0]["t"], lo)
        self.assertEqual(sorted(a["proc"] for a in d["assertions"]), ["caffeinate", "nfsd"])
        self.assertEqual(d["sleepservice"], [])
        self.assertEqual([x["on"] for x in d["display"]], [True, False])
        self.assertEqual([i["state"] for i in d["intervals"]], ["sleep", "awake"])

    def test_since_before_log_is_noop(self):
        m = fixture_model()
        m.clip(ts("2026-09-25 00:00:00"), None)
        self.assertEqual(to_data(m), to_data(fixture_model()))


class HelperTests(unittest.TestCase):
    def test_union(self):
        self.assertEqual(powerlog.union_secs([(0, 10000), (5000, 15000), (20000, 21000)]), 16)
        self.assertEqual(powerlog.union_secs([(0, 10000), (2000, 3000)]), 10)
        self.assertEqual(powerlog.union_secs([]), 0)

    def test_fmt_dur(self):
        self.assertEqual([powerlog.fmt_dur(s) for s in (5, 65, 3725, 90000)], ["5s", "1m05s", "1h02m", "1d01h"])

    def test_parse_when(self):
        now = 1_000_000.0
        self.assertIsNone(powerlog.parse_when(None, now))
        self.assertEqual(powerlog.parse_when("90m", now), (now - 5400) * 1000)
        self.assertEqual(powerlog.parse_when("1.5h", now), (now - 5400) * 1000)
        self.assertEqual(powerlog.parse_when("1w", now), (now - 604800) * 1000)
        local = lambda *a: int(dt.datetime(*a).timestamp() * 1000)
        self.assertEqual(powerlog.parse_when("2026-09-26", now), local(2026, 9, 26))
        self.assertEqual(powerlog.parse_when("2026-09-26 13:20", now), local(2026, 9, 26, 13, 20))
        self.assertEqual(powerlog.parse_when("2026-09-26 13:20:05", now), local(2026, 9, 26, 13, 20, 5))
        with self.assertRaises(SystemExit):
            powerlog.parse_when("yesterday", now)


class CliTests(unittest.TestCase):
    def run_cli(self, *args, input=None, check=True):
        return subprocess.run([sys.executable, str(ROOT / "powerlog.py"), *args], input=input,
                              capture_output=True, text=True, check=check, env=CLI_ENV)

    def test_text(self):
        out = self.run_cli("-i", str(FIXTURE), "--text").stdout
        self.assertIn("13:02:00  sleep    10m00s  Maintenance Sleep", out)
        self.assertIn("13:40:00  ?off     20m00s  no log entries (powered off?)", out)
        self.assertIn("Time in state: awake 21m18s, dark 2s, sleep 26m40s, unknown 20m00s", out)
        self.assertIn("1  DarkWake NUB.SPMI0Sw3IRQ rtc/SleepService", out)
        self.assertIn("45m00s       2x  nfsd", out)

    def test_html_escapes_data(self):
        log = FIXTURE.read_text().replace("(caffeinate)", "(evil</script><b>)")
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "out.html"
            self.assertEqual(self.run_cli("-i", "-", "-o", str(out_path), "--no-open", input=log).stdout.strip(), str(out_path))
            html = out_path.read_text()
        self.assertNotIn("/*__POWERLOG_DATA__*/", html)
        self.assertNotIn("evil</script>", html)
        blob = html.split("const DATA = ", 1)[1].split(";\n</script>", 1)[0]
        data = json.loads(blob)
        self.assertEqual(len(data["transitions"]), 5)
        self.assertIn("evil</script><b>", data["assertions"]["strings"])
        self.assertEqual(data["meta"]["source"], "-")

    def test_since_until_clip(self):
        data = json.loads(self.run_cli("-i", str(FIXTURE), "--json", "--since", "2026-09-26 13:20", "--until", "2026-09-26 13:50").stdout)
        self.assertEqual([t["state"] for t in data["transitions"]], ["sleep", "awake"])
        self.assertEqual(data["transitions"][0]["t"], ts("2026-09-26 13:20:00"))
        self.assertTrue(data["transitions"][0]["clipped"])
        self.assertEqual(data["meta"]["last"], ts("2026-09-26 13:50:00"))

    def test_no_entries(self):
        r = self.run_cli("-i", "-", "--json", input="nothing here\n", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no log entries found", r.stderr)

    def test_bad_since(self):
        r = self.run_cli("-i", str(FIXTURE), "--json", "--since", "soon", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("can't parse time 'soon'", r.stderr)


if __name__ == "__main__":
    unittest.main()
