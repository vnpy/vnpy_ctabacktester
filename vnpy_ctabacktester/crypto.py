from dataclasses import dataclass
from importlib import import_module
from time import sleep
from typing import Any

from vnpy.event import EventEngine
from vnpy.trader.constant import Exchange


@dataclass(frozen=True)
class CryptoGatewaySpec:
    """Built-in cryptocurrency history gateway metadata."""

    name: str
    module: str
    class_name: str
    exchange: Exchange
    rest_host_attr: str
    testnet_rest_host_attr: str = ""
    usdt_rest_host_attr: str = ""
    coin_rest_host_attr: str = ""
    usdt_testnet_rest_host_attr: str = ""
    coin_testnet_rest_host_attr: str = ""
    description: str = ""


CRYPTO_GATEWAYS: dict[str, CryptoGatewaySpec] = {
    "binance_spot": CryptoGatewaySpec(
        name="binance_spot",
        module="vnpy.gateway.binance.binance_gateway",
        class_name="BinanceGateway",
        exchange=Exchange.BINANCE,
        rest_host_attr="REST_HOST",
        description="Binance spot public kline API",
    ),
    "binance_usdt_futures": CryptoGatewaySpec(
        name="binance_usdt_futures",
        module="vnpy.gateway.binances.binances_gateway",
        class_name="BinancesGateway",
        exchange=Exchange.BINANCE,
        rest_host_attr="F_REST_HOST",
        usdt_rest_host_attr="F_REST_HOST",
        coin_rest_host_attr="D_REST_HOST",
        usdt_testnet_rest_host_attr="F_TESTNET_RESTT_HOST",
        coin_testnet_rest_host_attr="D_TESTNET_RESTT_HOST",
        description="Binance USDT-margined futures public kline API",
    ),
    "binance_coin_futures": CryptoGatewaySpec(
        name="binance_coin_futures",
        module="vnpy.gateway.binances.binances_gateway",
        class_name="BinancesGateway",
        exchange=Exchange.BINANCE,
        rest_host_attr="D_REST_HOST",
        usdt_rest_host_attr="F_REST_HOST",
        coin_rest_host_attr="D_REST_HOST",
        usdt_testnet_rest_host_attr="F_TESTNET_RESTT_HOST",
        coin_testnet_rest_host_attr="D_TESTNET_RESTT_HOST",
        description="Binance coin-margined futures public kline API",
    ),
    "huobi_spot": CryptoGatewaySpec(
        name="huobi_spot",
        module="vnpy.gateway.huobi.huobi_gateway",
        class_name="HuobiGateway",
        exchange=Exchange.HUOBI,
        rest_host_attr="REST_HOST",
        description="Huobi spot public kline API",
    ),
    "gateio_futures": CryptoGatewaySpec(
        name="gateio_futures",
        module="vnpy.gateway.gateios.gateios_gateway",
        class_name="GateiosGateway",
        exchange=Exchange.GATEIO,
        rest_host_attr="REST_HOST",
        testnet_rest_host_attr="TESTNET_REST_HOST",
        description="Gate.io futures public kline API",
    ),
    "bitmex": CryptoGatewaySpec(
        name="bitmex",
        module="vnpy.gateway.bitmex.bitmex_gateway",
        class_name="BitmexGateway",
        exchange=Exchange.BITMEX,
        rest_host_attr="REST_HOST",
        testnet_rest_host_attr="TESTNET_REST_HOST",
        description="BitMEX public kline API",
    ),
    "bitfinex": CryptoGatewaySpec(
        name="bitfinex",
        module="vnpy.gateway.bitfinex.bitfinex_gateway",
        class_name="BitfinexGateway",
        exchange=Exchange.BITFINEX,
        rest_host_attr="REST_HOST",
        description="Bitfinex public kline API",
    ),
    "bitstamp": CryptoGatewaySpec(
        name="bitstamp",
        module="vnpy.gateway.bitstamp.bitstamp_gateway",
        class_name="BitstampGateway",
        exchange=Exchange.BITSTAMP,
        rest_host_attr="REST_HOST",
        description="Bitstamp public kline API",
    ),
    "coinbase": CryptoGatewaySpec(
        name="coinbase",
        module="vnpy.gateway.coinbase.coinbase_gateway",
        class_name="CoinbaseGateway",
        exchange=Exchange.COINBASE,
        rest_host_attr="REST_HOST",
        testnet_rest_host_attr="SANDBOX_REST_HOST",
        description="Coinbase public kline API",
    ),
}


@dataclass
class CryptoGatewayConfig:
    """Lightweight crypto gateway settings."""

    name: str = "binance_spot"
    server: str = "REAL"
    usdt_base: bool = True
    rest_host: str = ""
    proxy_host: str = ""
    proxy_port: int = 0
    use_env_proxy: bool = False
    request_retries: int = 3
    retry_delay: float = 1.0


def available_crypto_gateways() -> dict[str, str]:
    """Return built-in cryptocurrency gateway choices."""
    return {name: spec.description for name, spec in CRYPTO_GATEWAYS.items()}


def get_crypto_gateway_spec(name: str) -> CryptoGatewaySpec:
    """Return metadata for a configured cryptocurrency gateway."""
    try:
        return CRYPTO_GATEWAYS[name]
    except KeyError as exc:
        available = ", ".join(sorted(CRYPTO_GATEWAYS))
        raise ValueError(f"unknown crypto gateway {name!r}; available: {available}") from exc


def create_crypto_gateway(spec: CryptoGatewaySpec, config: CryptoGatewayConfig) -> Any:
    """Create a gateway instance initialized for synchronous public history queries."""
    module = import_module(spec.module)
    gateway_class = getattr(module, spec.class_name)
    gateway = gateway_class(EventEngine())

    rest_host = resolve_rest_host(module, spec, config)
    rest_api = gateway.rest_api
    rest_api.init(rest_host, config.proxy_host, config.proxy_port)
    if not config.proxy_host and not config.use_env_proxy:
        rest_api.proxies = {"http": None, "https": None}
    install_request_retry(rest_api, config)

    if spec.class_name == "BinancesGateway":
        rest_api.usdt_base = config.usdt_base
        rest_api.server = config.server
        if spec.name == "binance_coin_futures":
            rest_api.usdt_base = False

    return gateway


def install_request_retry(rest_api: Any, config: CryptoGatewayConfig) -> None:
    """Install lightweight retry around legacy synchronous REST requests."""
    if not hasattr(rest_api, "request"):
        return

    original_request = rest_api.request
    retries = max(1, config.request_retries)
    retry_delay = max(0, config.retry_delay)

    def request_with_retry(*args: Any, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                return original_request(*args, **kwargs)
            except Exception as exc:
                last_error = exc
                if attempt + 1 < retries and retry_delay:
                    sleep(retry_delay)

        if last_error:
            raise last_error

        return original_request(*args, **kwargs)

    rest_api.request = request_with_retry


def resolve_rest_host(module: Any, spec: CryptoGatewaySpec, config: CryptoGatewayConfig) -> str:
    """Resolve REST host constant for a built-in crypto gateway."""
    if config.rest_host:
        return config.rest_host

    server = config.server.upper()

    if spec.class_name == "BinancesGateway":
        usdt_base = config.usdt_base
        if spec.name == "binance_coin_futures":
            usdt_base = False

        if server == "TESTNET":
            attr = spec.usdt_testnet_rest_host_attr if usdt_base else spec.coin_testnet_rest_host_attr
        else:
            attr = spec.usdt_rest_host_attr if usdt_base else spec.coin_rest_host_attr

        return str(getattr(module, attr))

    if server == "TESTNET" and spec.testnet_rest_host_attr:
        return str(getattr(module, spec.testnet_rest_host_attr))

    return str(getattr(module, spec.rest_host_attr))
