import sys
import unittest
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from benchmark_cpu_dvfs import make_replay, hint_spec, dynamic_policy

class HintReplayTests(unittest.TestCase):
    def test_balanced_replay(self):
        models=["small","medium","large","large"]
        deadlines=[18,45,68,100]
        a=make_replay(models,deadlines,120,500,42)
        b=make_replay(models,deadlines,120,500,42)
        c=make_replay(models,deadlines,120,500,43)
        self.assertEqual(a,b)
        self.assertNotEqual(a,c)
        self.assertEqual(Counter((x["model"],x["deadline_ms"]) for x in a),
                         Counter({k:30 for k in zip(models,deadlines)}))
        self.assertTrue(all(x["release_ms"]==i*500 and 0<=x["notice_token"]<=1000000
                            for i,x in enumerate(a)))
    def test_labels(self):
        self.assertEqual(hint_spec("hint_mixed_150"),("mixed",150))
        self.assertEqual(hint_spec("hint_static_25"),("static",25))
        self.assertTrue(dynamic_policy("hint_fixed_0"))
        self.assertFalse(dynamic_policy("ondemand"))

if __name__=="__main__":
    unittest.main()
