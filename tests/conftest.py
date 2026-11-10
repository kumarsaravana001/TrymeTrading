import pytest

from tryme.backtest import synthetic
from tryme.config import Config
from tryme.exchange.paper import TF_MS, PaperExchange, ReplayFeed
from tryme.models import Account, ReflexDecision, Setup

TF = TF_MS["15m"]
SYM = "SIM/USDT:USDT"


@pytest.fixture
def cfg(tmp_path):
    return Config(var_dir=str(tmp_path / "var"), policy_dir=str(tmp_path / "policies"))


@pytest.fixture
def candles():
    return synthetic(400, TF, seed=3)


@pytest.fixture
def setup_long():
    return Setup(
        symbol=SYM, bias="long", thesis="t", quality_3="q", invalidation=["x"], as_of="2026-11-10"
    )


@pytest.fixture
def feed(candles):
    f = ReplayFeed({SYM: candles})
    f.set_time(candles[-1].close_ts)
    return f


@pytest.fixture
def paper(feed, cfg):
    return PaperExchange(feed, 1000.0, cfg.costs)


def decision(**kw):
    base = dict(
        regime="trending",
        direction="long",
        direction_confidence=0.9,
        toxic_flow=False,
        setup_quality=3,
        risk_state="safe",
    )
    return ReflexDecision(**{**base, **kw})


def account(equity=1000.0, positions=None):
    return Account(equity, equity, positions or {})


class FixedReflex:
    is_model = False

    def __init__(self, d=None, delay=0.0, exc=None):
        self.d, self.delay, self.exc, self.calls = d or decision(), delay, exc, 0

    def decide(self, snap, setup):
        import time

        self.calls += 1
        time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.d
