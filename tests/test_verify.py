"""Exercise verify_candidates and the gate logic without touching the network."""
import sys; sys.path.insert(0,'.')
from ebay_scanner import verify

# passes() gate
cases = [
    (None, False, "no verdict"),
    ({"still_listed": False, "live_comps": 9, "is_lowest": True, "live_low": 5.0}, False, "dead listing"),
    ({"still_listed": True, "live_comps": 2, "is_lowest": True, "live_low": 5.0}, False, "too few comps"),
    ({"still_listed": True, "live_comps": 9, "is_lowest": False, "live_low": 42.0}, False, "not lowest"),
    ({"still_listed": True, "live_comps": 9, "is_lowest": True, "live_low": 50.0}, True, "good"),
]
for v, want, label in cases:
    got, why = verify.passes(v)
    assert got == want, f"{label}: expected {want} got {got} ({why})"
    print(f"  ok  {label:14} -> {'KEEP' if got else 'DROP'}: {why}")

# comp filtering and self-exclusion
class Stub:
    def __init__(self, items): self.items = items
    def search(self, params): return {"total": len(self.items), "itemSummaries": self.items}
    def get(self, url, **kw):
        class R: status_code = 200
        return R()

items = [
    {"itemId": "SELF", "title": "Wemby Prizm #136 PSA 10", "price": {"value": "149.99"}},
    {"itemId": "A", "title": "Wemby Prizm #136 PSA 10", "price": {"value": "300.00"}},
    {"itemId": "B", "title": "Some other 2023 Prizm 136 no hash", "price": {"value": "9.99"}},
    {"itemId": "C", "title": "Wemby #136 Prizm", "price": {"value": "250.00"}},
    {"itemId": "D", "title": "Wemby #136 Prizm", "price": {"value": "275.00"}},
    {"itemId": "E", "title": "Wemby #136 Prizm", "price": {"value": "260.00"}},
]
cfg = {"category_ids": ["261328"], "price_min": 10, "price_max": 800,
       "currency": "USD", "buying_options": ["FIXED_PRICE"], "sort": "price", "limit": 100}
sl = {"aspect_filter": "Set:{2023 Panini Prizm}", "set": "2023 Panini Prizm",
      "grader": "PSA", "grade": "10", "name": "x"}
KEY = "2023|panini prizm|136|base|PSA|10"

# Cheapest at $149.99 against a $250 low: comps filtered, and it survives.
v = verify.check(Stub(items), cfg, sl, KEY, "SELF", 149.99)
assert v["live_comps"] == 4, v          # B excluded (bare number), SELF excluded
assert v["live_low"] == 250.0, v
assert v["is_lowest"] is True, v
ok, why = verify.passes(v)
assert ok, why
print(f"  ok  comp filter    -> kept {v['live_comps']} of 6 "
      f"(self excluded, bare-number title rejected), low ${v['live_low']}")
print(f"  ok  cheapest       -> KEEP: {why}")

# Same market, but the candidate is no longer the cheapest.
undercut = items + [{"itemId": "F", "title": "Wemby #136 Prizm",
                     "price": {"value": "99.00"}}]
v2 = verify.check(Stub(undercut), cfg, sl, KEY, "SELF", 149.99)
assert v2["live_low"] == 99.0, v2
assert v2["is_lowest"] is False, v2
ok2, why2 = verify.passes(v2)
assert not ok2
print(f"  ok  undercut       -> DROP: {why2}")

# A listing that has ended is dropped whatever the comps say.
class Dead(Stub):
    def get(self, url, **kw):
        class R: status_code = 404
        return R()
v3 = verify.check(Dead(items), cfg, sl, KEY, "SELF", 149.99)
ok3, why3 = verify.passes(v3)
assert not ok3
print(f"  ok  ended listing  -> DROP: {why3}")
print("all assertions passed")
