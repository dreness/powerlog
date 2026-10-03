import importlib.machinery
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixture.log"

loader = importlib.machinery.SourceFileLoader("powerlog", str(ROOT / "powerlog.py"))
spec = importlib.util.spec_from_loader("powerlog", loader)
powerlog = importlib.util.module_from_spec(spec)
loader.exec_module(powerlog)


def unpack_assertions(p):
    return [{c: v if j < p["nums"] else p["strings"][v] for j, (c, v) in enumerate(zip(p["cols"], row))} for row in p["rows"]]


def load():
    model = powerlog.parse(FIXTURE.read_text().splitlines())
    data = model.to_json({})
    data["assertions"] = unpack_assertions(data["assertions"])
    return data


def ts(s):
    return int(powerlog.parse_ts(s + " -0700").timestamp() * 1000)


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = load()

    def test_transitions(self):
        states = [t["state"] for t in self.d["transitions"]]
        self.assertEqual(states, ["sleep", "dark", "sleep", "awake", "boot"])
        dark = self.d["transitions"][1]
        self.assertEqual(dark["reason"], "NUB.SPMI0Sw3IRQ rtc/SleepService")
        self.assertEqual(dark["drivers"], ["NUB.SPMI0Sw3IRQ", "rtc"])
        self.assertEqual(dark["wakeTime"], 0.306)

    def test_sleep_details(self):
        sl = self.d["transitions"][0]
        self.assertEqual(sl["reason"], "Maintenance Sleep")
        self.assertEqual(sl["slept"], 600)
        self.assertEqual([w["proc"] for w in sl["wakeRequests"]], ["dasd", "powerd"])
        self.assertTrue(sl["wakeRequests"][0]["next"])
        self.assertEqual(sl["pmAcks"][0], {"who": "mDNSResponder", "msg": None, "ms": 2637})

    def test_intervals_mark_gap_before_boot(self):
        ivs = self.d["intervals"]
        self.assertEqual([i["state"] for i in ivs], ["sleep", "dark", "sleep", "awake", "unknown", "awake"])
        gap = ivs[4]
        self.assertEqual(gap["s"], ts("2026-09-26 13:40:00"))
        self.assertEqual(gap["e"], ts("2026-09-26 14:00:00"))

    def by_proc(self, proc):
        return [a for a in self.d["assertions"] if a["proc"] == proc]

    def test_created_released_pair(self):
        (a,) = self.by_proc("apsd")
        self.assertEqual((a["s"], a["e"], a["end"]), (ts("2026-09-26 13:01:00"), ts("2026-09-26 13:12:02"), "Released"))
        self.assertFalse(a.get("inferred"))

    def test_release_without_create_uses_duration(self):
        (a,) = self.by_proc("coreaudiod")
        self.assertEqual(a["s"], ts("2026-09-26 12:50:00"))
        self.assertTrue(a["inferred"])

    def test_summary_opens_and_reboot_closes(self):
        old, new = sorted(self.by_proc("nfsd"), key=lambda a: a["s"])
        self.assertEqual(old["s"], ts("2026-09-26 13:29:00") - 100 * 3600 * 1000)
        self.assertEqual(old["end"], "reboot")
        self.assertEqual(old["e"], ts("2026-09-26 13:40:00"))
        self.assertEqual(new["end"], "ongoing")

    def test_client_died(self):
        (a,) = self.by_proc("caffeinate")
        self.assertEqual(a["end"], "ClientDied")
        self.assertEqual(a["e"] - a["s"], 240000)

    def test_display_sleepservice_battery(self):
        self.assertEqual([x["on"] for x in self.d["display"]], [True, False])
        self.assertEqual(len(self.d["sleepservice"]), 1)
        self.assertEqual(self.d["battery"][0], {"t": ts("2026-09-26 13:02:00"), "ac": False, "pct": 89})
        self.assertTrue(self.d["battery"][-1]["ac"])

    def test_union(self):
        self.assertEqual(powerlog.union_secs([(0, 10000), (5000, 15000), (20000, 21000)]), 16)


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "powerlog.py"), "-i", str(FIXTURE), *args],
                              capture_output=True, text=True, check=True).stdout

    def test_text(self):
        out = self.run_cli("--text")
        self.assertIn("Maintenance Sleep", out)
        self.assertIn("Wake reasons:", out)

    def test_html(self):
        out_path = ROOT / "tests" / "_out.html"
        try:
            self.run_cli("-o", str(out_path), "--no-open")
            html = out_path.read_text()
            self.assertNotIn("/*__POWERLOG_DATA__*/", html)
            blob = html.split("const DATA = ", 1)[1].split(";\n</script>", 1)[0]
            self.assertEqual(len(json.loads(blob)["transitions"]), 5)
        finally:
            out_path.unlink(missing_ok=True)

    def test_since_clips(self):
        data = json.loads(self.run_cli("--json", "--since", "2026-09-26 13:20"))
        self.assertEqual([t["state"] for t in data["transitions"]], ["sleep", "awake", "boot"])
        self.assertEqual(data["transitions"][0]["t"], ts("2026-09-26 13:20:00"))
        self.assertTrue(data["transitions"][0]["clipped"])


if __name__ == "__main__":
    unittest.main()
