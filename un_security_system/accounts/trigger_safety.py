import sys, django, logging
sys.path.insert(0, "/home/claude/undss/un_security_system")
from django.conf import settings
settings.configure(DEBUG=True, INSTALLED_APPS=["django.contrib.contenttypes","django.contrib.auth"], USE_TZ=True)
django.setup()
logging.disable(logging.CRITICAL)          # the failures below are logged on purpose
from accounts import module_events as ME, trigger_engine_esign as TE

class R:
    pk = 1
    agency_id = None
    def __str__(self): return "record"

# 1. an event nobody registered
print("unknown event      ->", ME.fire("nothing.at.all", R()), "(no exception)")

# 2. the engine itself blowing up
original = TE.handle
TE.handle = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
print("engine explodes    ->", ME.fire("asset_request.received", R()), "(no exception)")
TE.handle = original

# 3. the database being unreachable when the trigger list is read
class Boom:
    class objects:
        @staticmethod
        def filter(*a, **k): raise RuntimeError("database is down")
sys.modules["accounts.models_esign_triggers"] = type(sys)("accounts.models_esign_triggers")
sys.modules["accounts.models_esign_triggers"].ModuleTrigger = Boom
print("db unreachable     ->", ME.fire("asset_request.received", R()), "(no exception)")
del sys.modules["accounts.models_esign_triggers"]

# 4. a record missing every relation the event expects
class Bare:
    pk = 99
    agency_id = None
    def __str__(self): return "bare record"
event = ME.get("asset_request.received")
ctx = ME.context_for(event, Bare())
print("bare record        -> asset_name =", repr(ctx["asset_name"]), "| label =", repr(ctx["record_label"]))
assert ctx["asset_name"] == ""

# 5. the model hook itself — the shape used in AssetRequest.verify_receipt
class FakeRequest:
    pk = 5
    requester_id = 1
    status = "assigned"
    def save(self, **k): pass
    def verify_receipt(self, by_user):
        self.status = "received"
        self.save(update_fields=["status"])
        from accounts import module_events
        module_events.fire("asset_request.received", self, actor=by_user)
        return "the module finished its own work"
print("module hook        ->", FakeRequest().verify_receipt(None))
print("\nNOTHING THE AUTOMATION DOES CAN BREAK A MODULE")
