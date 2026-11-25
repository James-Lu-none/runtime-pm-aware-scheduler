import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from run import next_due, timing_stats, trace_to_csv, cpuidle_states

class TelemetryTests(unittest.TestCase):
    def test_skip_missed_ticks(self):
        self.assertAlmostEqual(next_due(1.,.1,1.35),1.4)
        self.assertAlmostEqual(next_due(1.,.1,1.01),1.1)
    def test_gaps(self):
        self.assertEqual(timing_stats([])["samples"],0)
        self.assertAlmostEqual(timing_stats([0,.03,.06])["median_gap_ms"],30)
    def test_trace_cpu_filter(self):
        text="worker [000] .... 10.000100: cpu_frequency: state=1500000 cpu_id=0\nworker [002] .... 10.000200: cpu_frequency: state=2000000 cpu_id=2\n"
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"events.csv"
            self.assertEqual(trace_to_csv(text,path,{0}),1)
            self.assertIn("10.000100,cpu_frequency,0,1500000",path.read_text())
    def test_cpuidle_discovery(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            policy=root/"policy"
            policy.mkdir()
            (policy/"related_cpus").write_text("0 1")
            state=root/"cpu0/cpuidle/state0"
            state.mkdir(parents=True)
            for name,value in (("time","100"),("usage","2"),("name","WFI")):
                (state/name).write_text(value)
            self.assertEqual(cpuidle_states(policy,root),[(0,"state0","WFI",state)])

if __name__=="__main__":
    unittest.main()
