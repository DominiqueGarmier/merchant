from __future__ import annotations

import functools
import inspect
from abc import ABCMeta
from collections import defaultdict
from collections.abc import Callable
from collections.abc import Collection
from decimal import Decimal
from typing import Any
from typing import Concatenate
from typing import NewType
from typing import ParamSpec
from typing import TypeVar

import pandas as pd

from merchant.core.clock import ClockHook
from merchant.core.clock import HasInternalClock
from merchant.core.clock import NSClock
from merchant.core.numeric import NormedDecimal
from merchant.trading.market import MarketSimulation
from merchant.trading.portfolio.benchmarks import Benchmark
from merchant.trading.portfolio.benchmarks import BoundBenchmark
from merchant.trading.portfolio.trade import ClosedPosition
from merchant.trading.portfolio.trade import Trade
from merchant.trading.portfolio.trade import ValuedTrade
from merchant.trading.tools.asset import Asset
from merchant.trading.tools.asset import Valuation
from merchant.trading.tools.instrument import Instrument


class _StaticPortfolio(metaclass=ABCMeta):
    '''
    'StaticPortfolio' represents the state of a trading portfolio a single point in time
    '''

    _assets: dict[Instrument, Asset]

    def __init__(self, /, *, assets: Collection[Asset] | None = None) -> None:
        self._assets = {asset.instrument: asset for asset in assets or []}

    @property
    def assets(self) -> list[Asset]:
        return list(self._assets.values())

    def __getitem__(self, instrument: Instrument) -> Asset:
        if instrument not in self._assets:
            return Asset(
                instrument,
                quantity=NormedDecimal(Decimal(0), prec=instrument.precision),
            )
        return self._assets[instrument]

    def add_asset(self, asset: Asset) -> None:
        owned = self[asset.instrument]
        self._assets[owned.instrument] = owned + asset

    def remove_asset(self, asset: Asset) -> None:
        owned = self[asset.instrument]
        if self.has_asset(asset) is False:
            raise ValueError(f'Cannot remove {asset!r} from {self!r}')
        new = owned - asset
        if new.quantity > 0:
            del self._assets[owned.instrument]
        else:
            self._assets[owned.instrument] = new

    def has_asset(self, asset: Asset) -> bool:
        return self[asset.instrument].quantity >= asset.quantity

    def __iadd__(self, asset: Asset) -> _StaticPortfolio:
        self.add_asset(asset)
        return self

    def __isub__(self, asset: Asset) -> _StaticPortfolio:
        self.remove_asset(asset)
        return self

    def __contains__(self, instrument: Instrument) -> bool:
        return self[instrument].quantity > 0

    def __str__(self) -> str:
        return f'{type(self)}(...)'

    def __repr__(self) -> str:
        return str(self)


# buy amount, valuation wrt. benchmark, trade
TradeTuple = tuple[Asset, ValuedTrade]


class _OpenPositionStack:
    _positions: dict[Instrument, list[TradeTuple]]
    _benchmark: Instrument  # we don't track positions on the benchmark (since their performance is always trivial)

    def __init__(self, /, *, benchmark: Instrument) -> None:
        self._positions = defaultdict(list)
        self._benchmark = benchmark

    def _push(self, trade: ValuedTrade) -> None:
        trade_tuple: TradeTuple = (trade._buy, 0, trade)  # type: ignore
        self._positions[trade._buy.instrument].append(trade_tuple)

    def _pop(self, trade: ValuedTrade) -> list[ClosedPosition]:
        ret: list[ClosedPosition] = []
        sold_asset = trade._sell
        while sold_asset > 0:
            asset, open = self._positions[trade._sell.instrument].pop()

            if sold_asset <= asset:
                self._positions[trade._sell.instrument].append(
                    (asset - trade._sell, open)
                )
                ret.append(ClosedPosition(amount=trade._sell, open=open, close=trade))
                continue

            ret.append(ClosedPosition(amount=asset, open=open, close=trade))
            sold_asset -= asset
        return ret

    def handle_trade(self, trade: ValuedTrade) -> list[ClosedPosition]:
        if trade._buy.instrument != self._benchmark:
            self._push(trade)
        if trade._sell.instrument != self._benchmark:
            return self._pop(trade)
        return []


TPortfolio = TypeVar('TPortfolio', bound='Portfolio')
P = ParamSpec('P')
R = TypeVar('R')


def invalidates_cache(
    func: Callable[Concatenate[TPortfolio, P], R]
) -> Callable[Concatenate[TPortfolio, P], R]:
    @functools.wraps(func)
    def wrapper(self: TPortfolio, *args: P.args, **kwargs: P.kwargs) -> R:
        self._invalidate_cache()
        return func(self, *args, **kwargs)

    return wrapper


ArgsKwargs = tuple[tuple[Any], dict[str, Any]]


class Portfolio(HasInternalClock[NSClock], _StaticPortfolio):
    '''
    'Portfolio' represents a trading portfolio, this includes:
        - the current state of the portfolio
        - performance (relative to a connected market)
        - value and trading history
    '''

    _benchmark_instrument: Instrument  # with respect to which instrument the portfolio is valued
    _benchmarks: dict[Benchmark, tuple[ArgsKwargs, ...]]
    _bound_benchmarks: dict[Benchmark, BoundBenchmark]

    _clock_hook: ClockHook

    _market: MarketSimulation

    _value_history: pd.Series

    _trade_history: list[Trade]
    _open_positions: _OpenPositionStack
    _closed_positions: list[ClosedPosition]

    def __init__(
        self, /, *, market: MarketSimulation, assets: Collection[Asset] | None = None
    ) -> None:
        super().__init__(assets=assets)

        self._market = market
        self._clock = self._market.clock

        for benchmark, arg_tuple in self._benchmarks.items():
            bound_benchmark = benchmark(self)
            self._bound_benchmarks[benchmark] = bound_benchmark
            # check types
            sig = inspect.signature(bound_benchmark)
            for args, kwargs in arg_tuple:
                try:
                    sig.bind(*args, **kwargs)
                except TypeError as e:
                    raise TypeError(
                        f'Invalid arguments for {benchmark!r} bound to {self!r}'
                    ) from e

        # attach cache invalidation hook to clock
        hook = ClockHook(lambda: self._invalidate_cache())
        self._clock_hook = hook
        self._clock_hook = self.clock.attach(hook=hook)

    @property
    def market(self) -> MarketSimulation:
        return self._market

    @property
    def value(self) -> Valuation:
        raise NotImplementedError

    @property
    def benchmark_instrument(self) -> Instrument:
        return self._benchmark

    @benchmark_instrument.setter
    def benchmark_instrument(self, instrument: Instrument) -> None:
        self._benchmark = instrument

    @property
    def benchmarks(self) -> tuple[Benchmark, ...]:
        return tuple(self._benchmarks.keys())

    def _invalidate_cache(self) -> None:
        for benchmark in self._bound_benchmarks.values():
            benchmark.invalidate_cache()

    def _append_trade_to_history(self, trade: ValuedTrade) -> None:
        self._trade_history.append(trade)
        closed_positions = self._open_positions.handle_trade(trade)
        self._closed_positions.extend(closed_positions)
