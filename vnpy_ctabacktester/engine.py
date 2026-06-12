import importlib
import vnpy.trader.datafeed as datafeed_module
import traceback
from datetime import datetime
from threading import Thread
from pathlib import Path
from inspect import getfile
from glob import glob
from types import ModuleType
from typing import Any
from pandas import DataFrame
import requests

from vnpy.event import Event, EventEngine
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.trader.constant import Interval
from vnpy.trader.utility import extract_vt_symbol
from vnpy.trader.object import HistoryRequest, TickData, BarData, ContractData
from vnpy.trader.datafeed import BaseDatafeed, get_datafeed
from vnpy.trader.database import BaseDatabase, get_database
from vnpy.trader.setting import SETTINGS

import vnpy_ctastrategy
from vnpy_ctastrategy import CtaTemplate, TargetPosTemplate
from vnpy_ctastrategy.backtesting import (
    BacktestingEngine,
    OptimizationSetting,
    BacktestingMode
)
from .crypto import CryptoGatewayConfig
from .locale import _

APP_NAME = "CtaBacktester"

EVENT_BACKTESTER_LOG = "eBacktesterLog"
EVENT_BACKTESTER_BACKTESTING_FINISHED = "eBacktesterBacktestingFinished"
EVENT_BACKTESTER_OPTIMIZATION_FINISHED = "eBacktesterOptimizationFinished"


class BacktesterEngine(BaseEngine):
    """
    For running CTA strategy backtesting.
    """

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__(main_engine, event_engine, APP_NAME)

        self.classes: dict = {}
        self.backtesting_engine: BacktestingEngine = None
        self.thread: Thread | None = None

        self.datafeed: BaseDatafeed = get_datafeed()
        self.database: BaseDatabase = get_database()

        # Backtesting reuslt
        self.result_df: DataFrame | None = None
        self.result_statistics: dict | None = None

        # Optimization result
        self.result_values: list | None = None

    def init_engine(self) -> None:
        """"""
        self.write_log(_("初始化CTA回测引擎"))

        self.backtesting_engine = BacktestingEngine()
        # Redirect log from backtesting engine outside.
        self.backtesting_engine.output = self.write_log

        self.load_strategy_class()
        self.write_log(_("策略文件加载完成"))

        self.init_datafeed()

    def init_datafeed(self) -> None:
        """
        Init datafeed client.
        """
        result: bool = self.datafeed.init(self.write_log)
        if result:
            self.write_log(_("数据服务初始化成功"))

    def get_datafeed_by_name(self, datafeed_name: str) -> BaseDatafeed | None:
        """
        Create and initialize datafeed with selected source name.
        """
        datafeed_name = datafeed_name.strip()
        if not datafeed_name:
            self.write_log(_("行情数据源为空，请先选择或输入数据源"))
            return None

        SETTINGS["datafeed.name"] = datafeed_name
        datafeed_module.datafeed = None

        try:
            datafeed: BaseDatafeed = get_datafeed()
            self.datafeed = datafeed
            result: bool = datafeed.init(self.write_log)
        except Exception:
            self.write_log(
                _("数据服务{}初始化异常：\n{}").format(
                    datafeed_name,
                    traceback.format_exc()
                )
            )
            return None

        if not result:
            self.write_log(_("数据服务{}初始化失败").format(datafeed_name))
            return None

        self.write_log(_("使用行情数据源：{}").format(datafeed_name))
        return datafeed

    def get_crypto_gateway_name(self, datafeed_name: str) -> str:
        """
        Return crypto gateway name if selected source is a crypto source.
        """
        name: str = datafeed_name.strip()
        for prefix in ["crypto:", "crypto_gateway:"]:
            if name.startswith(prefix):
                return name.removeprefix(prefix).strip()

        try:
            from .crypto import CRYPTO_GATEWAYS
        except Exception:
            return ""

        if name in CRYPTO_GATEWAYS:
            return name

        return ""

    def normalize_vt_symbols(self, values: list[Any]) -> list[str]:
        """
        Convert common symbol list values to sorted vt_symbol strings.
        """
        symbols: set[str] = set()

        for value in values:
            if isinstance(value, str):
                if "." in value:
                    symbols.add(value)
                continue

            vt_symbol: str = getattr(value, "vt_symbol", "")
            if vt_symbol:
                symbols.add(vt_symbol)
                continue

            if isinstance(value, dict):
                vt_symbol = str(value.get("vt_symbol", ""))
                if vt_symbol:
                    symbols.add(vt_symbol)
                    continue

                symbol = value.get("symbol")
                exchange = value.get("exchange")
                if symbol and exchange:
                    if hasattr(exchange, "value"):
                        exchange = exchange.value
                    symbols.add(f"{symbol}.{exchange}")

        return sorted(symbols)

    def default_intervals(self, include_tick: bool = True) -> list[str]:
        """
        Return default vn.py interval values.
        """
        intervals: list[str] = []
        for interval in Interval:
            if not include_tick and interval == Interval.TICK:
                continue
            intervals.append(interval.value)
        return intervals

    def normalize_intervals(self, values: list[Any]) -> list[str]:
        """
        Convert common interval list values to sorted interval strings.
        """
        intervals: list[str] = []
        seen: set[str] = set()
        valid: set[str] = {interval.value for interval in Interval}

        for value in values:
            if isinstance(value, Interval):
                text = value.value
            else:
                text = str(getattr(value, "value", value))

            if text in valid and text not in seen:
                intervals.append(text)
                seen.add(text)

        return intervals

    def get_local_vt_symbols(self) -> list[str]:
        """
        Load vt_symbols from local contracts and database overviews.
        """
        symbols: set[str] = set()

        try:
            for contract in self.main_engine.get_all_contracts():
                symbols.add(contract.vt_symbol)
        except Exception:
            self.write_log(_("从当前合约列表加载本地代码失败"))

        try:
            for overview in self.database.get_bar_overview():
                if overview.symbol and overview.exchange:
                    symbols.add(f"{overview.symbol}.{overview.exchange.value}")
        except Exception:
            self.write_log(_("从本地K线数据加载本地代码失败"))

        try:
            for overview in self.database.get_tick_overview():
                if overview.symbol and overview.exchange:
                    symbols.add(f"{overview.symbol}.{overview.exchange.value}")
        except Exception:
            self.write_log(_("从本地Tick数据加载本地代码失败"))

        return sorted(symbols)

    def get_local_intervals(self, vt_symbol: str = "") -> list[str]:
        """
        Load intervals from local bar data overview.
        """
        intervals: set[str] = set()
        filter_symbol: str = ""
        filter_exchange = None

        if vt_symbol and "." in vt_symbol:
            try:
                filter_symbol, filter_exchange = extract_vt_symbol(vt_symbol)
            except ValueError:
                filter_symbol = ""
                filter_exchange = None

        try:
            for overview in self.database.get_bar_overview():
                if not overview.interval:
                    continue

                if filter_symbol and (
                    overview.symbol != filter_symbol
                    or overview.exchange != filter_exchange
                ):
                    continue

                intervals.add(overview.interval.value)
        except Exception:
            self.write_log(_("从本地K线数据加载周期失败"))

        if intervals:
            return self.normalize_intervals(sorted(intervals))

        return self.default_intervals()

    def get_source_vt_symbols(self, datafeed_name: str) -> list[str]:
        """
        Load vt_symbols from selected data source when supported.
        """
        crypto_gateway_name: str = self.get_crypto_gateway_name(datafeed_name)
        if crypto_gateway_name:
            return self.get_crypto_vt_symbols(crypto_gateway_name)

        datafeed: BaseDatafeed | None = self.get_datafeed_by_name(datafeed_name)
        if datafeed is None:
            return []

        method_names: list[str] = [
            "query_symbols",
            "get_symbols",
            "get_all_symbols",
            "query_contracts",
            "get_contracts",
            "query_contract",
        ]
        for method_name in method_names:
            method = getattr(datafeed, method_name, None)
            if not callable(method):
                continue

            try:
                values = method()
            except TypeError:
                try:
                    values = method(self.write_log)
                except TypeError:
                    continue
            except Exception:
                self.write_log(
                    _("数据源{}加载本地代码失败，触发异常：\n{}").format(
                        datafeed_name,
                        traceback.format_exc()
                    )
                )
                return []

            if values:
                symbols = self.normalize_vt_symbols(list(values))
                if symbols:
                    return symbols

        self.write_log(_("数据源{}不支持代码列表加载，请手动输入").format(datafeed_name))
        return []

    def get_source_intervals(self, datafeed_name: str) -> list[str]:
        """
        Load intervals from selected data source when supported.
        """
        crypto_gateway_name: str = self.get_crypto_gateway_name(datafeed_name)
        if crypto_gateway_name:
            return self.get_crypto_intervals(crypto_gateway_name)

        datafeed: BaseDatafeed | None = self.get_datafeed_by_name(datafeed_name)
        if datafeed is None:
            return self.default_intervals()

        method_names: list[str] = [
            "query_intervals",
            "get_intervals",
            "get_all_intervals",
            "query_history_intervals",
            "get_history_intervals",
        ]
        for method_name in method_names:
            method = getattr(datafeed, method_name, None)
            if not callable(method):
                continue

            try:
                values = method()
            except TypeError:
                try:
                    values = method(self.write_log)
                except TypeError:
                    continue
            except Exception:
                self.write_log(
                    _("数据源{}加载K线周期失败，触发异常：\n{}").format(
                        datafeed_name,
                        traceback.format_exc()
                    )
                )
                return self.default_intervals()

            if values:
                intervals = self.normalize_intervals(list(values))
                if intervals:
                    return intervals

        self.write_log(_("数据源{}不支持周期列表加载，使用默认周期").format(datafeed_name))
        return self.default_intervals()

    def get_crypto_intervals(self, gateway_name: str) -> list[str]:
        """
        Return currently supported intervals for selected crypto gateway.
        """
        if not self.get_crypto_gateway_name(gateway_name):
            return self.default_intervals(include_tick=False)

        return [
            Interval.MINUTE.value,
            Interval.HOUR.value,
            Interval.DAILY.value,
        ]

    def get_crypto_vt_symbols(self, gateway_name: str) -> list[str]:
        """
        Reload contract vt_symbols from selected crypto gateway.
        """
        try:
            from .crypto import get_crypto_gateway_spec, resolve_rest_host
        except Exception:
            self.write_log(_("虚拟币行情源加载失败，请确认crypto模块可用"))
            return []

        try:
            spec = get_crypto_gateway_spec(gateway_name)
        except ValueError as exc:
            self.write_log(str(exc))
            return []

        config = CryptoGatewayConfig()
        config.name = gateway_name
        config.server = str(SETTINGS.get("crypto_gateway.server", config.server))
        config.rest_host = str(SETTINGS.get("crypto_gateway.rest_host", config.rest_host))
        config.proxy_host = str(SETTINGS.get("crypto_gateway.proxy_host", config.proxy_host))
        config.proxy_port = int(SETTINGS.get("crypto_gateway.proxy_port", config.proxy_port) or 0)

        try:
            module = importlib.import_module(spec.module)
            rest_host: str = resolve_rest_host(module, spec, config).rstrip("/")
            timeout: float = float(SETTINGS.get("crypto_gateway.request_timeout", 10) or 10)
            proxies = self.get_crypto_proxies(config)

            symbols: list[str] = self.query_crypto_symbols_http(
                gateway_name,
                spec.exchange.value,
                rest_host,
                proxies,
                timeout
            )
        except Exception as exc:
            self.write_log(
                _("虚拟币行情源{}加载合约失败：{}").format(
                    gateway_name,
                    exc
                )
            )
            return []

        self.write_log(_("虚拟币行情源{}加载{}个合约").format(gateway_name, len(symbols)))
        return symbols

    def get_crypto_proxies(self, config: CryptoGatewayConfig) -> dict | None:
        """
        Build requests proxy settings for crypto HTTP calls.
        """
        use_env_proxy = SETTINGS.get(
            "crypto_gateway.use_env_proxy",
            config.use_env_proxy
        )
        if isinstance(use_env_proxy, str):
            use_env_proxy = use_env_proxy.lower() in {"1", "true", "yes", "y"}

        if use_env_proxy:
            return None

        if config.proxy_host and config.proxy_port:
            proxy = (
                f"http://{config.proxy_host}:"
                f"{config.proxy_port}"
            )
            return {"http": proxy, "https": proxy}

        return {"http": None, "https": None}

    def request_crypto_json(
        self,
        rest_host: str,
        path: str,
        proxies: dict | None,
        timeout: float,
        params: dict | None = None
    ) -> Any:
        """
        Request crypto REST JSON with timeout and without global excepthook.
        """
        response = requests.get(
            f"{rest_host}{path}",
            params=params,
            proxies=proxies,
            timeout=timeout
        )
        response.raise_for_status()
        return response.json()

    def query_crypto_symbols_http(
        self,
        gateway_name: str,
        exchange_value: str,
        rest_host: str,
        proxies: dict | None,
        timeout: float
    ) -> list[str]:
        """
        Query crypto contract symbols through safe synchronous HTTP APIs.
        """
        if gateway_name == "binance_spot":
            data = self.request_crypto_json(rest_host, "/api/v3/exchangeInfo", proxies, timeout)
            values = data.get("symbols", [])
            return sorted(
                f"{item['symbol']}.{exchange_value}"
                for item in values
                if item.get("symbol") and item.get("status") in {"TRADING", "BREAK"}
            )

        if gateway_name == "binance_usdt_futures":
            data = self.request_crypto_json(rest_host, "/fapi/v1/exchangeInfo", proxies, timeout)
            values = data.get("symbols", [])
            return sorted(
                f"{item['symbol']}.{exchange_value}"
                for item in values
                if item.get("symbol") and item.get("status") == "TRADING"
            )

        if gateway_name == "binance_coin_futures":
            data = self.request_crypto_json(rest_host, "/dapi/v1/exchangeInfo", proxies, timeout)
            values = data.get("symbols", [])
            return sorted(
                f"{item['symbol']}.{exchange_value}"
                for item in values
                if item.get("symbol") and item.get("contractStatus") == "TRADING"
            )

        if gateway_name == "huobi_spot":
            data = self.request_crypto_json(rest_host, "/v1/common/symbols", proxies, timeout)
            values = data.get("data", [])
            return sorted(
                f"{item['symbol']}.{exchange_value}"
                for item in values
                if item.get("symbol") and item.get("state") == "online"
            )

        if gateway_name == "gateio_futures":
            try:
                values = self.request_crypto_json(
                    rest_host,
                    "/api/v4/futures/usdt/contracts",
                    proxies,
                    timeout
                )
            except requests.RequestException:
                values = self.request_crypto_json(
                    rest_host,
                    "/api/v4/futures/contracts",
                    proxies,
                    timeout
                )
            return sorted(
                f"{item['name']}.{exchange_value}"
                for item in values
                if item.get("name")
            )

        if gateway_name == "bitmex":
            values = self.request_crypto_json(
                rest_host,
                "/instrument/active",
                proxies,
                timeout
            )
            return sorted(
                f"{item['symbol']}.{exchange_value}"
                for item in values
                if item.get("symbol") and item.get("tickSize") and item.get("lotSize")
            )

        if gateway_name == "bitfinex":
            values = self.request_crypto_json(rest_host, "/v1/symbols_details", proxies, timeout)
            return sorted(
                f"{item['pair'].upper()}.{exchange_value}"
                for item in values
                if item.get("pair")
            )

        if gateway_name == "bitstamp":
            values = self.request_crypto_json(rest_host, "/trading-pairs-info/", proxies, timeout)
            return sorted(
                f"{item['url_symbol']}.{exchange_value}"
                for item in values
                if item.get("url_symbol")
            )

        if gateway_name == "coinbase":
            values = self.request_crypto_json(rest_host, "/products", proxies, timeout)
            return sorted(
                f"{item['id']}.{exchange_value}"
                for item in values
                if item.get("id")
            )

        self.write_log(_("虚拟币行情源{}不支持合约列表加载").format(gateway_name))
        return []

    def query_crypto_bar_history(
        self,
        gateway_name: str,
        req: HistoryRequest,
        vt_symbol: str
    ) -> list[BarData]:
        """
        Query bar history directly from selected crypto gateway.
        """
        try:
            from .crypto import create_crypto_gateway, get_crypto_gateway_spec
        except Exception:
            self.write_log(_("虚拟币行情源加载失败，请确认crypto模块可用"))
            return []

        try:
            spec = get_crypto_gateway_spec(gateway_name)
        except ValueError as exc:
            self.write_log(str(exc))
            return []

        if req.exchange != spec.exchange:
            self.write_log(
                _("{}交易所后缀与行情源{}不匹配，应为{}").format(
                    vt_symbol,
                    gateway_name,
                    spec.exchange.value
                )
            )
            return []

        config = CryptoGatewayConfig()
        config.name = gateway_name
        config.server = str(SETTINGS.get("crypto_gateway.server", config.server))
        config.rest_host = str(SETTINGS.get("crypto_gateway.rest_host", config.rest_host))
        config.proxy_host = str(SETTINGS.get("crypto_gateway.proxy_host", config.proxy_host))
        config.proxy_port = int(SETTINGS.get("crypto_gateway.proxy_port", config.proxy_port) or 0)
        config.request_retries = int(
            SETTINGS.get("crypto_gateway.request_retries", config.request_retries) or 1
        )
        config.retry_delay = float(
            SETTINGS.get("crypto_gateway.retry_delay", config.retry_delay) or 0
        )

        use_env_proxy = SETTINGS.get(
            "crypto_gateway.use_env_proxy",
            config.use_env_proxy
        )
        if isinstance(use_env_proxy, str):
            config.use_env_proxy = use_env_proxy.lower() in {
                "1",
                "true",
                "yes",
                "y"
            }
        else:
            config.use_env_proxy = bool(use_env_proxy)

        gateway = create_crypto_gateway(spec, config)
        try:
            self.write_log(_("使用虚拟币行情源：{}").format(gateway_name))
            return gateway.query_history(req) or []
        except Exception:
            msg: str = _("虚拟币历史数据下载失败，触发异常：\n{}").format(
                traceback.format_exc()
            )
            self.write_log(msg)
            return []
        finally:
            gateway.close()

    def write_log(self, msg: str) -> None:
        """"""
        event: Event = Event(EVENT_BACKTESTER_LOG)
        event.data = msg
        self.event_engine.put(event)

    def load_strategy_class(self) -> None:
        """
        Load strategy class from source code.
        """
        app_path: Path = Path(vnpy_ctastrategy.__file__).parent
        path1: Path = app_path.joinpath("strategies")
        self.load_strategy_class_from_folder(path1, "vnpy_ctastrategy.strategies")

        path2: Path = Path.cwd().joinpath("strategies")
        self.load_strategy_class_from_folder(path2, "strategies")

    def load_strategy_class_from_folder(self, path: Path, module_name: str = "") -> None:
        """
        Load strategy class from certain folder.
        """
        for suffix in ["py", "pyd", "so"]:
            pathname: str = str(path.joinpath(f"*.{suffix}"))
            for filepath in glob(pathname):
                filename: str = Path(filepath).stem
                name: str = f"{module_name}.{filename}"
                self.load_strategy_class_from_module(name)

    def load_strategy_class_from_module(self, module_name: str) -> None:
        """
        Load strategy class from module file.
        """
        try:
            module: ModuleType = importlib.import_module(module_name)

            # 重载模块，确保如果策略文件中有任何修改，能够立即生效。
            importlib.reload(module)

            for name in dir(module):
                value = getattr(module, name)
                if (
                    isinstance(value, type)
                    and issubclass(value, CtaTemplate)
                    and value not in {CtaTemplate, TargetPosTemplate}
                ):
                    self.classes[value.__name__] = value
        except:  # noqa
            msg: str = _("策略文件{}加载失败，触发异常：\n{}").format(
                module_name, traceback.format_exc()
            )
            self.write_log(msg)

    def reload_strategy_class(self) -> None:
        """"""
        self.classes.clear()
        self.load_strategy_class()
        self.write_log(_("策略文件重载刷新完成"))

    def get_strategy_class_names(self) -> list:
        """"""
        return list(self.classes.keys())

    def run_backtesting(
        self,
        class_name: str,
        vt_symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        rate: float,
        slippage: float,
        size: int,
        pricetick: float,
        capital: int,
        setting: dict
    ) -> None:
        """"""
        self.result_df = None
        self.result_statistics = None

        engine: BacktestingEngine = self.backtesting_engine
        engine.clear_data()

        if interval == Interval.TICK.value:
            mode: BacktestingMode = BacktestingMode.TICK
        else:
            mode = BacktestingMode.BAR

        engine.set_parameters(
            vt_symbol=vt_symbol,
            interval=interval,
            start=start,
            end=end,
            rate=rate,
            slippage=slippage,
            size=size,
            pricetick=pricetick,
            capital=capital,
            mode=mode
        )

        strategy_class: type[CtaTemplate] = self.classes[class_name]
        engine.add_strategy(
            strategy_class,
            setting
        )

        engine.load_data()
        if not engine.history_data:
            self.write_log(_("策略回测失败，历史数据为空"))
            self.thread = None
            return

        try:
            engine.run_backtesting()
        except Exception:
            msg: str = _("策略回测失败，触发异常：\n{}").format(traceback.format_exc())
            self.write_log(msg)

            self.thread = None
            return

        self.result_df = engine.calculate_result()
        self.result_statistics = engine.calculate_statistics(output=False)

        # Clear thread object handler.
        self.thread = None

        # Put backtesting done event
        event: Event = Event(EVENT_BACKTESTER_BACKTESTING_FINISHED)
        self.event_engine.put(event)

    def start_backtesting(
        self,
        class_name: str,
        vt_symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        rate: float,
        slippage: float,
        size: float,
        pricetick: float,
        capital: float,
        setting: dict
    ) -> bool:
        if self.thread:
            self.write_log(_("已有任务在运行中，请等待完成"))
            return False

        self.write_log("-" * 40)
        self.thread = Thread(
            target=self.run_backtesting,
            args=(
                class_name,
                vt_symbol,
                interval,
                start,
                end,
                rate,
                slippage,
                size,
                pricetick,
                capital,
                setting
            )
        )
        self.thread.start()

        return True

    def get_result_df(self) -> DataFrame | None:
        """"""
        return self.result_df

    def get_result_statistics(self) -> dict | None:
        """"""
        return self.result_statistics

    def get_result_values(self) -> list | None:
        """"""
        return self.result_values

    def get_default_setting(self, class_name: str) -> dict:
        """"""
        strategy_class: type[CtaTemplate] = self.classes[class_name]
        setting: dict = strategy_class.get_class_parameters()
        return setting

    def run_optimization(
        self,
        class_name: str,
        vt_symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        rate: float,
        slippage: float,
        size: int,
        pricetick: float,
        capital: int,
        optimization_setting: OptimizationSetting,
        use_ga: bool,
        max_workers: int | None = None
    ) -> None:
        """"""
        self.result_values = None

        engine: BacktestingEngine = self.backtesting_engine
        engine.clear_data()

        if interval == Interval.TICK.value:
            mode: BacktestingMode = BacktestingMode.TICK
        else:
            mode = BacktestingMode.BAR

        engine.set_parameters(
            vt_symbol=vt_symbol,
            interval=interval,
            start=start,
            end=end,
            rate=rate,
            slippage=slippage,
            size=size,
            pricetick=pricetick,
            capital=capital,
            mode=mode
        )

        strategy_class: type[CtaTemplate] = self.classes[class_name]
        engine.add_strategy(
            strategy_class,
            {}
        )

        # 0则代表不限制
        if max_workers == 0:
            max_workers = None

        if use_ga:
            self.result_values = engine.run_ga_optimization(
                optimization_setting,
                output=False,
                max_workers=max_workers
            )
        else:
            self.result_values = engine.run_bf_optimization(
                optimization_setting,
                output=False,
                max_workers=max_workers
            )

        # Clear thread object handler.
        self.thread = None
        self.write_log(_("多进程参数优化完成"))

        # Put optimization done event
        event: Event = Event(EVENT_BACKTESTER_OPTIMIZATION_FINISHED)
        self.event_engine.put(event)

    def start_optimization(
        self,
        class_name: str,
        vt_symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        rate: float,
        slippage: float,
        size: float,
        pricetick: float,
        capital: float,
        optimization_setting: OptimizationSetting,
        use_ga: bool,
        max_workers: int
    ) -> bool:
        if self.thread:
            self.write_log(_("已有任务在运行中，请等待完成"))
            return False

        self.write_log("-" * 40)
        self.thread = Thread(
            target=self.run_optimization,
            args=(
                class_name,
                vt_symbol,
                interval,
                start,
                end,
                rate,
                slippage,
                size,
                pricetick,
                capital,
                optimization_setting,
                use_ga,
                max_workers
            )
        )
        self.thread.start()

        return True

    def run_downloading(
        self,
        datafeed_name: str,
        vt_symbol: str,
        interval: str,
        start: datetime,
        end: datetime
    ) -> None:
        """
        执行下载任务
        """
        self.write_log(_("{}-{}开始下载历史数据").format(vt_symbol, interval))

        try:
            symbol, exchange = extract_vt_symbol(vt_symbol)
        except ValueError:
            self.write_log(_("{}解析失败，请检查交易所后缀").format(vt_symbol))
            self.thread = None
            return

        req: HistoryRequest = HistoryRequest(
            symbol=symbol,
            exchange=exchange,
            interval=Interval(interval),
            start=start,
            end=end
        )

        crypto_gateway_name: str = self.get_crypto_gateway_name(datafeed_name)
        if crypto_gateway_name:
            if interval == "tick":
                self.write_log(
                    _("虚拟币行情源{}暂不支持Tick下载").format(crypto_gateway_name)
                )
                self.thread = None
                return

            bar_data: list[BarData] = self.query_crypto_bar_history(
                crypto_gateway_name,
                req,
                vt_symbol
            )
            if bar_data:
                self.database.save_bar_data(bar_data)
                self.write_log(_("{}-{}历史数据下载完成").format(vt_symbol, interval))
            else:
                self.write_log(_("数据下载失败，无法获取{}的历史数据").format(vt_symbol))

            self.thread = None
            return

        datafeed: BaseDatafeed | None = self.get_datafeed_by_name(datafeed_name)
        if datafeed is None:
            self.thread = None
            return

        try:
            if interval == "tick":
                tick_data: list[TickData] = datafeed.query_tick_history(req, self.write_log)
                if tick_data:
                    self.database.save_tick_data(tick_data)
                    self.write_log(_("{}-{}历史数据下载完成").format(vt_symbol, interval))
                else:
                    self.write_log(_("数据下载失败，无法获取{}的历史数据").format(vt_symbol))
            else:
                bar_data: list[BarData] = datafeed.query_bar_history(req, self.write_log)

                if bar_data:
                    self.database.save_bar_data(bar_data)
                    self.write_log(_("{}-{}历史数据下载完成").format(vt_symbol, interval))
                else:
                    self.write_log(_("数据下载失败，无法获取{}的历史数据").format(vt_symbol))
        except Exception:
            msg: str = _("数据下载失败，触发异常：\n{}").format(traceback.format_exc())
            self.write_log(msg)

        # Clear thread object handler.
        self.thread = None

    def start_downloading(
        self,
        datafeed_name: str,
        vt_symbol: str,
        interval: str,
        start: datetime,
        end: datetime
    ) -> bool:
        if self.thread:
            self.write_log(_("已有任务在运行中，请等待完成"))
            return False

        self.write_log("-" * 40)
        self.thread = Thread(
            target=self.run_downloading,
            args=(
                datafeed_name,
                vt_symbol,
                interval,
                start,
                end
            )
        )
        self.thread.start()

        return True

    def get_all_trades(self) -> list:
        """"""
        trades: list = self.backtesting_engine.get_all_trades()
        return trades

    def get_all_orders(self) -> list:
        """"""
        orders: list = self.backtesting_engine.get_all_orders()
        return orders

    def get_all_daily_results(self) -> list:
        """"""
        results: list = self.backtesting_engine.get_all_daily_results()
        return results

    def get_history_data(self) -> list:
        """"""
        history_data: list = self.backtesting_engine.history_data
        return history_data

    def get_strategy_class_file(self, class_name: str) -> str:
        """"""
        strategy_class: type[CtaTemplate] = self.classes[class_name]
        file_path: str = getfile(strategy_class)
        return file_path
