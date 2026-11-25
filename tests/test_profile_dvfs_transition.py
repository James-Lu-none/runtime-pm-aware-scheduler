#!/usr/bin/env python3
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from profile_dvfs_transition import trace_events, trace_overrun, aggregate

class TransitionTests(unittest.TestCase):
    def test_trace_filtering(self):
        trace = """
 worker-1 [000] ... 10.000100: cpu_frequency: state=1500000 cpu_id=0
 worker-1 [000] ... 10.000200: cpu_frequency_limits: min=1500000 max=2400000 cpu_id=0
 worker-1 [000] ... 10.000300: cpu_frequency: state=2400000 cpu_id=0
 worker-1 [000] ... 10.000301: cpu_frequency: state=2400000 cpu_id=1
 worker-1 [000] ... 10.000302: cpu_frequency: state=2400000 cpu_id=5
 worker-1 [000] ... 10.002000: cpu_frequency: state=2400000 cpu_id=0
"""
        events=trace_events(trace,10_000_150_000,10_001_000_000,{0,1},2400000)
        self.assertEqual(events["cpu_frequency"],[.15,.151])
        self.assertEqual(events["cpu_frequency_limits"],[.05])

    def test_overrun(self):
        self.assertTrue(trace_overrun("# entries-in-buffer/entries-written: 10/20   #P:4"))
        self.assertFalse(trace_overrun("# entries-in-buffer/entries-written: 10/10   #P:4"))

    def test_summary_keeps_missing_trace_missing(self):
        row=dict(from_khz=1500000,to_khz=2400000,valid=1,write_ms=.05,
                 policy_readback_ms=.1,freq_readback_ms=.2,ready_ms=.21,
                 kernel_limits_event_ms=None,kernel_frequency_event_ms=None)
        result=aggregate([row],[1500000,2400000])
        self.assertEqual(result[0]["ready_ms_p95"],.21)
        self.assertIsNone(result[0]["kernel_frequency_event_ms_p95"])
        self.assertEqual(result[1]["valid_samples"],0)

if __name__=="__main__":
    unittest.main()
