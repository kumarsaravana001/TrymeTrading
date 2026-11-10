import time
from types import SimpleNamespace

import anthropic
import ccxt
import httpx2
import pytest

from conftest import SYM, FixedReflex, decision
from test_risk_sizing import snapshot
from tryme.alerts import Alerts
from tryme.exchange.base import OrderRejected, TransientError
from tryme.exchange.ccxt_adapter import CcxtExchange
from tryme.exchange.paper import TF_MS, PaperExchange
from tryme.models import OrderIntent, ReflexDecision, Setup
from tryme.reflex import HaikuReflex, ReflexUnavailable, RuleReflex
from tryme.runtime import build, healthy, heartbeat, step, tradable_setups

S = Setup(symbol=SYM, bias="long", thesis="t", quality_3="q", invalidation=[], as_of="d")


# --- REFLEX --------------------------------------------------------------------------------------


class FakeMessages:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.kw = result, exc, None

    def parse(self, **kw):
        self.kw = kw
        if self.exc:
            raise self.exc
        return self.result


def haiku(result=None, exc=None):
    m = FakeMessages(result, exc)
    return HaikuReflex(client=SimpleNamespace(messages=m)), m


def test_haiku_request_shape_and_parse():
    d = decision()
    r, m = haiku(SimpleNamespace(stop_reason="end_turn", parsed_output=d))
    assert r.decide(snapshot(), S) == d and r.is_model
    assert m.kw["model"] == "claude-haiku-5-5"
    assert m.kw["output_config"] == {"effort": "low"}
    assert m.kw["output_format"] is ReflexDecision
    assert "temperature" not in m.kw and "thinking" not in m.kw
    user = m.kw["messages"][0]["content"]
    assert "mid=100.0" in user and "bias=long" in user
    assert len(user) / 3 < 500  # snapshot plus setup stays small


def test_haiku_refusal_or_truncation_is_unavailable():
    for sr, out in (("refusal", None), ("max_tokens", None), ("end_turn", None)):
        r, _ = haiku(SimpleNamespace(stop_reason=sr, parsed_output=out))
        with pytest.raises(ReflexUnavailable):
            r.decide(snapshot(), S)


def test_haiku_api_errors_are_unavailable():
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    errs = [
        anthropic.APIConnectionError(request=req),
        anthropic.RateLimitError("x", response=httpx2.Response(429, request=req), body=None),
        anthropic.InternalServerError("x", response=httpx2.Response(500, request=req), body=None),
    ]
    for e in errs:
        r, _ = haiku(exc=e)
        with pytest.raises(ReflexUnavailable):
            r.decide(snapshot(), S)


def test_schema_rejects_out_of_range_model_output():
    for bad in (
        {"setup_quality": 4},
        {"direction_confidence": 1.2},
        {"regime": "bullish"},
        {"risk_state": "yolo"},
        {"extra_field": 1},
    ):
        with pytest.raises(ValueError):
            decision(**bad)


def test_rule_reflex_regimes_and_direction():
    r = RuleReflex()
    up = r.decide(snapshot(trend=0.02, ret_12=0.01, ret_48=0.03), S)
    assert (up.regime, up.direction) == ("trending", "long") and up.direction_confidence > 0.8
    assert r.decide(snapshot(trend=0.0), S).direction == "neutral"
    assert r.decide(snapshot(vol=0.05), S).regime == "crisis"
    assert r.decide(snapshot(ret_12=-0.2), S).regime == "crisis"
    assert r.decide(snapshot(vol=0.02), S).regime == "high_vol"
    assert r.decide(snapshot(risk_state="reduce"), S).risk_state == "reduce"
    tox = r.decide(snapshot(trend=0.02, ret_12=0.01, ret_48=0.03, imbalance=-0.9), S)
    assert tox.toxic_flow is True


# --- ccxt adapter with a fake client -------------------------------------------------------------


class FakeCcxt:
    def __init__(self):
        self.orders, self.fail = [], None

    def fetch_time(self):
        return 1_700_000_000_000

    def fetch_ohlcv(self, s, tf, limit):
        return [[1_700_000_000_000 + i * 900_000, 1, 2, 0.5, 1.5, 10] for i in range(3)]

    def fetch_order_book(self, s, limit):
        return {"timestamp": 1_700_000_000_000, "bids": [[99, 1]], "asks": [[101, 2]]}

    def fetch_balance(self):
        return {"info": {"totalMarginBalance": "1234.5"}}

    def fetch_positions(self):
        return [{"symbol": SYM, "contracts": 2, "side": "short"}, {"symbol": "X", "contracts": 0}]

    def create_order(self, symbol, type_, side, amount, price, params):
        if self.fail:
            raise self.fail
        self.orders.append((symbol, type_, side, amount, params))
        return {
            "symbol": symbol,
            "side": side,
            "filled": amount,
            "average": 100.0,
            "status": "closed",
            "fee": {"cost": 0.05},
            "timestamp": 1,
        }


def test_ccxt_adapter_maps_data_and_orders():
    fake = FakeCcxt()
    x = CcxtExchange(mode="testnet", client=fake)
    cs = x.candles(SYM, "15m", 3)
    assert cs[0].close_ts == cs[1].ts and cs[0].close == 1.5
    assert x.book(SYM).asks[0] == (101.0, 2.0)
    assert x.equity() == 1234.5 and x.positions() == {SYM: -2.0}
    f = x.submit(OrderIntent(SYM, "buy", 2.0, True, "cid1", 100.0))
    assert f.status == "filled" and f.detail == "TESTNET"
    assert fake.orders[0][4] == {"newClientOrderId": "cid1", "reduceOnly": True}
    x.place_stop(SYM, "sell", 2.0, 95.0, "s1")
    assert fake.orders[1][1] == "STOP_MARKET" and fake.orders[1][4]["reduceOnly"] is True


def test_ccxt_errors_are_classified_and_never_echo_details():
    fake = FakeCcxt()
    x = CcxtExchange(mode="testnet", client=fake)
    fake.fail = ccxt.NetworkError("GET https://x?signature=SECRET")
    with pytest.raises(TransientError) as e:
        x.submit(OrderIntent(SYM, "buy", 1.0, False, "c", 100.0))
    assert "SECRET" not in str(e.value)
    fake.fail = ccxt.InsufficientFunds("margin")
    with pytest.raises(OrderRejected):
        x.submit(OrderIntent(SYM, "buy", 1.0, False, "c", 100.0))


def test_public_client_cannot_trade_and_keys_are_required(monkeypatch):
    x = CcxtExchange(mode="public", client=FakeCcxt())
    with pytest.raises(PermissionError):
        x.submit(OrderIntent(SYM, "buy", 1.0, False, "c", 100.0))
    with pytest.raises(PermissionError):
        x.equity()
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    with pytest.raises(PermissionError, match="BINANCE_TESTNET_API_KEY"):
        CcxtExchange(mode="testnet")
    monkeypatch.delenv("BINANCE_LIVE_API_KEY", raising=False)
    with pytest.raises(PermissionError, match="BINANCE_LIVE_API_KEY"):
        CcxtExchange(mode="live")


# --- runtime -------------------------------------------------------------------------------------


def test_heartbeat_and_health(tmp_path):
    tf = TF_MS["15m"]
    assert not healthy(tmp_path, tf)[0]
    now = int(time.time() * 1000)
    heartbeat(tmp_path, now)
    assert healthy(tmp_path, tf, now + tf)[0]
    assert not healthy(tmp_path, tf, now + 3 * tf)[0]


def engine(cfg, feed, **kw):
    return build(
        cfg,
        exchange=PaperExchange(feed, 1000.0, cfg.costs),
        reflex=FixedReflex(),
        alerts=Alerts("", ""),
        **kw,
    )


def test_liquidity_floor_refuses_thin_or_unknown_markets(cfg, feed, setup_long):
    from tryme.exchange.base import MarketInfo

    cfg.setups = [setup_long, setup_long.model_copy(update={"enabled": False})]
    eng = engine(cfg, feed)
    assert tradable_setups(eng) == []  # replay feed reports no volume -> refused
    assert any("liquidity floor" in a for a in eng.alerts.sent)
    feed.market = lambda s: MarketInfo(s, quote_volume_24h=9e7)
    assert tradable_setups(eng) == [setup_long]
    feed.market = lambda s: MarketInfo(s, quote_volume_24h=1e6)
    assert tradable_setups(eng) == []


def test_step_skips_everything_on_clock_skew(cfg, feed, candles, setup_long):
    eng = engine(cfg, feed)  # replay clock is in 2020, local clock is now -> huge skew
    step(eng, [setup_long], candles[-1].close_ts, 4)
    assert eng.positions == {} and eng.journal.mem[-1]["reason"].startswith("clock")


def test_step_trades_reconciles_and_beats(cfg, feed, candles, setup_long, monkeypatch):
    eng = engine(cfg, feed)
    monkeypatch.setattr(time, "time", lambda: candles[-1].close_ts / 1000)
    step(eng, [setup_long], candles[-1].close_ts, 4)
    assert SYM in eng.positions
    kinds = [j["kind"] for j in eng.journal.mem]
    assert "reconcile" in kinds and "fill" in kinds
    assert healthy(cfg.var_dir, TF_MS["15m"], candles[-1].close_ts)[0]


def test_shadow_policy_override_is_paper_only(cfg, feed, monkeypatch):
    from tryme.policy import PolicyStore

    PolicyStore(cfg.policy_dir).propose({"min_direction_confidence": 0.9}, "cand")
    monkeypatch.setenv("TRYME_POLICY", "v002")
    assert engine(cfg, feed).policy.version == "v002"
    cfg.mode = "testnet"
    assert engine(cfg, feed).policy.version == "v001"


def test_telegram_alert_posts_and_survives_failure():
    calls = []

    def post(url, json, timeout):
        calls.append((url, json))
        return SimpleNamespace(ok=True)

    a = Alerts("TOKEN", "42", post=post)
    assert a.send("hello") and calls[0][1] == {"chat_id": "42", "text": "hello"}

    def boom(*a, **k):
        raise OSError("down")

    assert Alerts("TOKEN", "42", post=boom).send("x") is False
    assert Alerts("", "").send("x") is False
