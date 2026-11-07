import importlib, json, math, os, sys, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot as B, sim, synth
H = importlib.import_module("house_bot")

SAMPLE = json.load(open(os.path.join(os.path.dirname(__file__), "..", "yeno-sample-observation.json")))

class Fees(unittest.TestCase):
    def test_fee_formula_matches_house_bot_and_sim(self):
        for fills in ([(0.9, 5.5)], [(0.5, 3), (0.51, 2.2)], [(0.99, 5.0)]):
            self.assertAlmostEqual(B.fees(fills), H._fees(fills), places=9)
            self.assertAlmostEqual(sim.fees(fills), H._fees(fills), places=9)

    def test_buy_never_exceeds_cap(self):
        for p in (0.55, 0.8, 0.9, 0.97, 0.99):
            for cap in (5.0, 4.975, 2.3):
                f, sh = sim.fill_buy([[p, 1000]], cap)
                self.assertLessEqual(sum(a * b for a, b in f) + sim.fees(f), cap + 1e-9)
                plan = B.buy_plan([[p, 1000]], cap)
                self.assertLessEqual(plan[1] + plan[2], cap + 1e-9)

class Decisions(unittest.TestCase):
    def test_sample_observation_is_valid(self):
        d = B.Bot().decide(SAMPLE)
        self.assertIn(d["action"], ("HOLD", "BUY", "SELL"))
        if d["action"] == "BUY":
            self.assertIn(d["outcome"], ("YES", "NO"))
            self.assertTrue(0 < d["maxCashUsd"] <= 5)

    def test_garbage_input_holds(self):
        self.assertEqual(B.Bot().decide({}), {"action": "HOLD"})
        self.assertEqual(B.Bot().decide({"timestamp": "x"}), {"action": "HOLD"})

    def test_buys_cheap_near_certain_winner(self):
        o = json.loads(json.dumps(SAMPLE))
        o["market"]["secondsToClose"] = 60
        o["reference"].update(btcMidUsd=80700, openingTargetUsd=80424.9, observedAt=o["timestamp"] - 0.1)
        o["books"]["YES"] = {"bids": [[0.84, 200]], "asks": [[0.85, 200]], "minOrderSize": 5}
        o["books"]["NO"] = {"bids": [[0.15, 200]], "asks": [[0.16, 200]], "minOrderSize": 5}
        d = B.Bot().decide(o)
        self.assertEqual(d["action"], "BUY")
        self.assertEqual(d["outcome"], "YES")
        self.assertLessEqual(d["maxCashUsd"], 5)

    def test_no_entry_in_final_seconds_and_stale_reference(self):
        o = json.loads(json.dumps(SAMPLE))
        o["reference"].update(btcMidUsd=80700, observedAt=o["timestamp"] - 0.1)
        o["books"]["YES"]["asks"] = [[0.85, 200]]
        o["market"]["secondsToClose"] = 14
        self.assertEqual(B.Bot().decide(o)["action"], "HOLD")
        o["market"]["secondsToClose"] = 60
        o["reference"]["observedAt"] = o["timestamp"] - 10
        self.assertEqual(B.Bot().decide(o)["action"], "HOLD")
        o["reference"].update(observedAt=o["timestamp"] - 0.1, targetProvisional=True)
        self.assertEqual(B.Bot().decide(o)["action"], "HOLD")

    def test_always_exits_before_close(self):
        o = json.loads(json.dumps(SAMPLE))
        o["market"]["secondsToClose"] = 5
        o["account"]["position"] = {"market_id": "m", "outcome": "YES", "shares": 5, "buy_gross_usd": 4.5,
                                    "buy_fees_usd": 0.07, "sell_gross_usd": 0, "sell_fees_usd": 0}
        self.assertEqual(B.Bot().decide(o)["action"], "SELL")

    def test_fast_enough(self):
        b = B.Bot()
        t = time.perf_counter()
        for _ in range(2000):
            b.decide(SAMPLE)
        self.assertLess((time.perf_counter() - t) / 2000, 0.005)

class Simulator(unittest.TestCase):
    def test_settlement_adds_no_volume_and_pays_winner(self):
        class HoldForever:
            done = False
            def decide(self, o):
                if not self.done and o["account"]["position"] is None and o["market"]["secondsToClose"] < 100:
                    self.done = True
                    return {"action": "BUY", "outcome": "YES", "maxCashUsd": 5}
                return {"action": "HOLD"}
        rows, out = synth.make_day(seed=3, hours=0.1)
        mk = sim.markets_from_rows(rows, out, min_rows=50)
        r = sim.run(HoldForever(), mk[:1])
        self.assertEqual(r["volume"], 0)
        self.assertEqual(r["settled"], 1)

    def test_round_trip_counts_volume(self):
        class Flip:
            def decide(self, o):
                if o["account"]["position"]:
                    return {"action": "SELL"}
                if 60 < o["market"]["secondsToClose"] < 200 and o["account"]["completedCycles"] == 0:
                    return {"action": "BUY", "outcome": "YES", "maxCashUsd": 5}
                return {"action": "HOLD"}
        rows, out = synth.make_day(seed=4, hours=0.1)
        r = sim.run(Flip(), sim.markets_from_rows(rows, out, min_rows=50)[:1])
        self.assertEqual(r["cycles"], 1)
        self.assertGreater(r["volume"], 5)
        self.assertLess(r["cash"], 10)

    def test_stops_at_first_crossing_flat(self):
        rows, out = synth.make_day(seed=0, lag=8.0, noise=0.02)
        r = sim.run(B.Bot(), sim.markets_from_rows(rows, out, min_rows=50))
        if r["qualified"]:
            self.assertGreaterEqual(r["volume"], 1000)
            self.assertLess(r["volume"], 1015)

if __name__ == "__main__":
    unittest.main()
