from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from vnpy.event import Event
from vnpy.trader.constant import Direction, Exchange, Interval, Offset, Product
from vnpy.trader.object import BarData, ContractData, HistoryRequest, TradeData

from vnpy_ctabacktester import CtaBacktesterApp
from vnpy_ctabacktester.engine import (
    APP_NAME,
    EVENT_BACKTESTER_BACKTESTING_FINISHED,
    EVENT_BACKTESTER_LOG,
    EVENT_BACKTESTER_OPTIMIZATION_FINISHED,
    BacktesterEngine,
)
from vnpy_ctabacktester.locale import _
from vnpy_ctastrategy.backtesting import BacktestingEngine, BacktestingMode
from vnpy_ctastrategy.template import CtaTemplate


VT_SYMBOL: str = "rb2501.SHFE"
START: datetime = datetime(2024, 1, 2)
END: datetime = datetime(2024, 1, 3)


class BuyOnceStrategy(CtaTemplate):
    fixed_size: int = 1
    parameters: list[str] = ["fixed_size"]

    def on_init(self) -> None:
        return

    def on_bar(self, bar: BarData) -> None:
        if not self.pos:
            self.buy(bar.close_price, self.fixed_size)


class InlineThread:
    def __init__(
        self,
        target: Callable[..., None],
        args: tuple[object, ...] | list[object] = (),
    ) -> None:
        self._target: Callable[..., None] = target
        self._args: tuple[object, ...] | list[object] = args

    def start(self) -> None:
        self._target(*self._args)


class RecordingEventEngine:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def put(self, event: Event) -> None:
        self.events.append(event)


class FakeMainEngine:
    def __init__(
        self,
        contract: ContractData | None = None,
        bars: list[BarData] | None = None,
    ) -> None:
        self.contract: ContractData | None = contract
        self.bars: list[BarData] = [] if bars is None else bars
        self.history_calls: list[tuple[HistoryRequest, str]] = []

    def get_contract(self, vt_symbol: str) -> ContractData | None:
        if self.contract and self.contract.vt_symbol == vt_symbol:
            return self.contract
        return None

    def query_history(
        self,
        req: HistoryRequest,
        gateway_name: str,
    ) -> list[BarData]:
        self.history_calls.append((req, gateway_name))
        return self.bars


class RecordingDatabase:
    def __init__(self) -> None:
        self.saved_bars: list[list[BarData]] = []

    def save_bar_data(self, bars: list[BarData], stream: bool = False) -> bool:
        self.saved_bars.append(list(bars))
        return True


class RecordingDatafeed:
    def __init__(self, bars: list[BarData] | None = None) -> None:
        self.bars: list[BarData] = [] if bars is None else bars
        self.bar_requests: list[HistoryRequest] = []
        self.outputs: list[object] = []

    def init(self, output: object = None) -> bool:
        return False

    def query_bar_history(
        self,
        req: HistoryRequest,
        output: object = None,
    ) -> list[BarData]:
        self.bar_requests.append(req)
        self.outputs.append(output)
        return self.bars


def make_bar(
    dt: datetime,
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
) -> BarData:
    return BarData(
        symbol="rb2501",
        exchange=Exchange.SHFE,
        datetime=dt,
        interval=Interval.MINUTE,
        volume=1,
        open_price=open_price,
        high_price=high_price,
        low_price=low_price,
        close_price=close_price,
        gateway_name="DB",
    )


def sample_bars() -> list[BarData]:
    # 第一根下单，第二根 low 穿过成交，第三根只做持仓盯市。
    return [
        make_bar(datetime(2024, 1, 2, 9, 0), 100, 100, 100, 100),
        make_bar(datetime(2024, 1, 2, 15, 0), 100, 101, 99, 100),
        make_bar(datetime(2024, 1, 3, 9, 0), 110, 110, 110, 110),
    ]


def log_messages(event_engine: RecordingEventEngine) -> list[str]:
    return [
        event.data
        for event in event_engine.events
        if event.type == EVENT_BACKTESTER_LOG
    ]


def make_engine(
    monkeypatch: pytest.MonkeyPatch,
    main_engine: FakeMainEngine | None = None,
    database: RecordingDatabase | None = None,
    datafeed: RecordingDatafeed | None = None,
) -> tuple[BacktesterEngine, RecordingEventEngine]:
    if main_engine is None:
        main_engine = FakeMainEngine()
    if database is None:
        database = RecordingDatabase()
    if datafeed is None:
        datafeed = RecordingDatafeed()

    def get_database() -> RecordingDatabase:
        return database

    def get_datafeed() -> RecordingDatafeed:
        return datafeed

    monkeypatch.setattr("vnpy_ctabacktester.engine.get_database", get_database)
    monkeypatch.setattr("vnpy_ctabacktester.engine.get_datafeed", get_datafeed)
    monkeypatch.setattr("vnpy_ctabacktester.engine.Thread", InlineThread)
    event_engine: RecordingEventEngine = RecordingEventEngine()
    engine: BacktesterEngine = BacktesterEngine(main_engine, event_engine)  # type: ignore[arg-type]
    backtesting: BacktestingEngine = BacktestingEngine()
    backtesting.output = engine.write_log  # type: ignore[method-assign]
    engine.backtesting_engine = backtesting
    engine.classes[BuyOnceStrategy.__name__] = BuyOnceStrategy
    return engine, event_engine


def install_bar_loader(
    monkeypatch: pytest.MonkeyPatch,
    bars: list[BarData],
) -> None:
    def load_bar_data(
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        start: datetime,
        end: datetime,
    ) -> list[BarData]:
        return [
            bar
            for bar in bars
            if bar.symbol == symbol
            and bar.exchange == exchange
            and bar.interval == interval
            and start <= bar.datetime <= end
        ]

    monkeypatch.setattr("vnpy_ctastrategy.backtesting.load_bar_data", load_bar_data)


class TestBacktesterEngine:
    def test_app_points_at_engine_without_opening_widget(self) -> None:
        app: CtaBacktesterApp = CtaBacktesterApp()
        assert app.app_name == APP_NAME == "CtaBacktester"
        assert app.engine_class is BacktesterEngine
        assert app.widget_name == "BacktesterManager"

    def test_start_backtesting_calculates_statistics(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        install_bar_loader(monkeypatch, sample_bars())
        engine, event_engine = make_engine(monkeypatch)
        started: bool = engine.start_backtesting(
            BuyOnceStrategy.__name__,
            VT_SYMBOL,
            Interval.MINUTE.value,
            START,
            END,
            0.0,
            0.0,
            10,
            1.0,
            1_000_000,
            {"fixed_size": 2},
        )

        assert started is True
        assert engine.thread is None
        backtesting: BacktestingEngine = engine.backtesting_engine
        assert backtesting.mode == BacktestingMode.BAR
        assert backtesting.vt_symbol == VT_SYMBOL
        assert backtesting.symbol == "rb2501"
        assert backtesting.exchange == Exchange.SHFE
        assert backtesting.interval == Interval.MINUTE
        assert backtesting.rate == 0
        assert backtesting.slippage == 0
        assert backtesting.size == 10
        assert backtesting.pricetick == 1
        assert backtesting.capital == 1_000_000
        assert backtesting.start == START
        assert backtesting.end == datetime(2024, 1, 3, 23, 59, 59)
        assert backtesting.strategy.fixed_size == 2
        assert backtesting.strategy.pos == 2
        assert len(backtesting.history_data) == 3

        trades: list[TradeData] = engine.get_all_trades()
        assert len(trades) == 1
        assert trades[0].price == 100
        assert trades[0].volume == 2
        assert trades[0].direction == Direction.LONG
        assert trades[0].offset == Offset.OPEN
        assert len(engine.get_all_orders()) == 1
        assert len(engine.get_all_daily_results()) == 2

        statistics: dict | None = engine.get_result_statistics()
        assert statistics is not None
        assert statistics["capital"] == 1_000_000
        assert statistics["total_trade_count"] == 1
        assert statistics["total_net_pnl"] == 200
        assert statistics["end_balance"] == 1_000_200
        assert statistics["total_turnover"] == 2000
        assert statistics["total_commission"] == 0
        assert statistics["total_slippage"] == 0
        assert statistics["total_days"] == 2
        assert statistics["profit_days"] == 1
        assert statistics["loss_days"] == 0
        result_df = engine.get_result_df()
        assert result_df is not None
        assert len(result_df) == 2
        finished: list[Event] = [
            event
            for event in event_engine.events
            if event.type == EVENT_BACKTESTER_BACKTESTING_FINISHED
        ]
        assert len(finished) == 1
        assert finished[0].data is None

    def test_run_backtesting_without_bars_skips_statistics(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        install_bar_loader(monkeypatch, [])
        engine, _event_engine = make_engine(monkeypatch)

        engine.run_backtesting(
            BuyOnceStrategy.__name__,
            VT_SYMBOL,
            Interval.MINUTE.value,
            START,
            END,
            0.0,
            0.0,
            10,
            1.0,
            1_000_000,
            {},
        )

        assert engine.backtesting_engine.history_data == []
        assert engine.get_result_df() is None
        assert engine.get_result_statistics() is None

    def test_tick_interval_selects_tick_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def load_tick_data(
            symbol: str,
            exchange: Exchange,
            start: datetime,
            end: datetime,
        ) -> list[object]:
            return []

        monkeypatch.setattr("vnpy_ctastrategy.backtesting.load_tick_data", load_tick_data)
        engine, _event_engine = make_engine(monkeypatch)

        engine.run_backtesting(
            BuyOnceStrategy.__name__,
            VT_SYMBOL,
            Interval.TICK.value,
            START,
            END,
            0.0,
            0.2,
            10,
            0.2,
            1_000_000,
            {},
        )

        assert engine.backtesting_engine.mode == BacktestingMode.TICK
        assert engine.backtesting_engine.interval == Interval.TICK
        assert engine.backtesting_engine.slippage == 0.2
        assert engine.backtesting_engine.pricetick == 0.2
        assert engine.get_result_statistics() is None

    def test_start_backtesting_rejects_when_busy(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine, event_engine = make_engine(monkeypatch)
        sentinel: object = object()
        engine.thread = sentinel  # type: ignore[assignment]

        started: bool = engine.start_backtesting(
            BuyOnceStrategy.__name__,
            VT_SYMBOL,
            Interval.MINUTE.value,
            START,
            END,
            0.0,
            0.0,
            10,
            1.0,
            1_000_000,
            {},
        )

        assert started is False
        assert engine.thread is sentinel
        assert _("已有任务在运行中，请等待完成") in log_messages(event_engine)

    def test_get_default_setting_and_strategy_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine, _event_engine = make_engine(monkeypatch)

        assert engine.get_strategy_class_names() == [BuyOnceStrategy.__name__]
        assert engine.get_default_setting(BuyOnceStrategy.__name__) == {"fixed_size": 1}
        assert Path(engine.get_strategy_class_file(BuyOnceStrategy.__name__)).resolve() == Path(__file__).resolve()

    def test_run_optimization_zero_workers_means_unlimited(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine, event_engine = make_engine(monkeypatch)
        seen: dict[str, object] = {}

        def run_bf(
            optimization_setting: object,
            output: bool = True,
            max_workers: int | None = None,
        ) -> list[dict[str, int]]:
            seen["setting"] = optimization_setting
            seen["output"] = output
            seen["max_workers"] = max_workers
            return [{"target": 1}]

        def run_ga(*args: object, **kwargs: object) -> list[object]:
            seen["ga"] = (args, kwargs)
            return []

        monkeypatch.setattr(engine.backtesting_engine, "run_bf_optimization", run_bf)
        monkeypatch.setattr(engine.backtesting_engine, "run_ga_optimization", run_ga)
        setting: object = object()
        engine.run_optimization(
            BuyOnceStrategy.__name__,
            VT_SYMBOL,
            Interval.MINUTE.value,
            START,
            END,
            0.0001,
            0.2,
            10,
            1.0,
            1_000_000,
            setting,  # type: ignore[arg-type]
            False,
            0,
        )

        assert seen["setting"] is setting
        assert seen["output"] is False
        assert seen["max_workers"] is None
        assert "ga" not in seen
        assert engine.get_result_values() == [{"target": 1}]
        assert engine.backtesting_engine.mode == BacktestingMode.BAR
        assert engine.backtesting_engine.rate == 0.0001
        assert engine.thread is None
        finished: list[Event] = [
            event
            for event in event_engine.events
            if event.type == EVENT_BACKTESTER_OPTIMIZATION_FINISHED
        ]
        assert len(finished) == 1

    def test_run_optimization_use_ga_keeps_worker_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine, _event_engine = make_engine(monkeypatch)
        seen: dict[str, object] = {}

        def run_ga(
            optimization_setting: object,
            output: bool = True,
            max_workers: int | None = None,
        ) -> list[str]:
            seen["output"] = output
            seen["max_workers"] = max_workers
            return ["ga"]

        def run_bf(*args: object, **kwargs: object) -> list[object]:
            seen["bf"] = (args, kwargs)
            return []

        monkeypatch.setattr(engine.backtesting_engine, "run_ga_optimization", run_ga)
        monkeypatch.setattr(engine.backtesting_engine, "run_bf_optimization", run_bf)
        engine.run_optimization(
            BuyOnceStrategy.__name__,
            VT_SYMBOL,
            Interval.HOUR.value,
            START,
            END,
            0.0,
            0.0,
            10,
            1.0,
            1_000_000,
            object(),  # type: ignore[arg-type]
            True,
            4,
        )

        assert seen["output"] is False
        assert seen["max_workers"] == 4
        assert "bf" not in seen
        assert engine.get_result_values() == ["ga"]
        assert engine.backtesting_engine.interval == Interval.HOUR

    def test_download_bars_from_datafeed_when_no_contract(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bar: BarData = make_bar(START, 100, 100, 100, 100)
        database: RecordingDatabase = RecordingDatabase()
        datafeed: RecordingDatafeed = RecordingDatafeed([bar])
        engine, event_engine = make_engine(
            monkeypatch,
            FakeMainEngine(),
            database,
            datafeed,
        )
        end: datetime = datetime(2024, 1, 3, 15, 0)

        started: bool = engine.start_downloading(VT_SYMBOL, Interval.MINUTE.value, START, end)

        assert started is True
        assert len(datafeed.bar_requests) == 1
        req: HistoryRequest = datafeed.bar_requests[0]
        assert req.symbol == "rb2501"
        assert req.exchange == Exchange.SHFE
        assert req.interval == Interval.MINUTE
        assert req.start == START
        assert req.end == end
        assert datafeed.outputs == [engine.write_log]
        assert database.saved_bars == [[bar]]
        assert _("{}-{}历史数据下载完成").format(VT_SYMBOL, "1m") in log_messages(event_engine)

    def test_download_bars_from_gateway_when_contract_has_history(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bar: BarData = make_bar(START, 100, 101, 99, 100)
        contract: ContractData = ContractData(
            symbol="rb2501",
            exchange=Exchange.SHFE,
            name="rb",
            product=Product.FUTURES,
            size=10,
            pricetick=1,
            history_data=True,
            gateway_name="FAKE",
        )
        database: RecordingDatabase = RecordingDatabase()
        datafeed: RecordingDatafeed = RecordingDatafeed([bar])
        main_engine: FakeMainEngine = FakeMainEngine(contract, [bar])
        engine, _event_engine = make_engine(monkeypatch, main_engine, database, datafeed)

        started: bool = engine.start_downloading(
            contract.vt_symbol,
            Interval.DAILY.value,
            START,
            END,
        )

        assert started is True
        assert len(main_engine.history_calls) == 1
        req: HistoryRequest
        gateway_name: str
        req, gateway_name = main_engine.history_calls[0]
        assert gateway_name == "FAKE"
        assert req.symbol == "rb2501"
        assert req.exchange == Exchange.SHFE
        assert req.interval == Interval.DAILY
        assert req.start == START
        assert req.end == END
        assert datafeed.bar_requests == []
        assert database.saved_bars == [[bar]]

    def test_download_rejects_symbol_without_exchange(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        database: RecordingDatabase = RecordingDatabase()
        datafeed: RecordingDatafeed = RecordingDatafeed()
        engine, event_engine = make_engine(
            monkeypatch,
            FakeMainEngine(),
            database,
            datafeed,
        )

        started: bool = engine.start_downloading("rb2501", Interval.MINUTE.value, START, END)

        assert started is True
        assert datafeed.bar_requests == []
        assert database.saved_bars == []
        assert engine.thread is None
        expected: str = _("{}解析失败，请检查交易所后缀").format("rb2501")
        assert expected in log_messages(event_engine)
