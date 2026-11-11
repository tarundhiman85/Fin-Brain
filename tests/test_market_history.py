"""Historical candle validation, daily session dates, scope and HTTP contracts."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from config import get_settings
from main import app
from market_history import fetch_candle_history
from trading_service import TradingError
import main_ui_bridge as bridge


@pytest.fixture
def provider(monkeypatch):
    import market_history
    settings = SimpleNamespace(llm_offline_mode=False, llm_public_tickers=["AAPL"])
    monkeypatch.setattr(market_history, "get_settings", lambda: settings)
    stock = Mock()
    stock.history.return_value = pd.DataFrame({"Open": [100, 101], "High": [103, 104],
        "Low": [99, 100], "Close": [102, 103], "Volume": [1000, 2000]},
        index=pd.to_datetime(["2025-01-02", "2025-01-03"]).tz_localize("Pacific/Kiritimati"), dtype=float)
    stock.get_history_metadata.return_value = {"symbol": "AAPL", "currency": "USD"}
    factory = Mock(return_value=stock)
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=factory))
    return settings, stock, factory


def test_history_preserves_exchange_date_and_bounds(provider):
    _, stock, _ = provider
    history = fetch_candle_history("aapl", "2y")
    assert [point.time for point in history.candles] == ["2025-01-02", "2025-01-03"]
    assert history.candles[0].volume == 1000 and history.currency == "USD"
    assert history.dropped_rows == 0
    stock.history.assert_called_once_with(period="2y", interval="1d", auto_adjust=False,
        actions=False, prepost=False, timeout=15, raise_errors=True)


def test_history_validates_before_network(provider):
    settings, _, factory = provider
    settings.llm_offline_mode = True
    with pytest.raises(TradingError):
        fetch_candle_history("AAPL")
    settings.llm_offline_mode = False
    with pytest.raises(TradingError):
        fetch_candle_history("SECRET")
    with pytest.raises(TradingError):
        fetch_candle_history("AAPL", "max")
    factory.assert_not_called()


@pytest.mark.parametrize("column,value", [("Open", float("nan")), ("High", 1),
    ("Low", 1000), ("Volume", -1), ("Volume", 1.5), ("Close", float("inf"))])
def test_history_reports_excluded_invalid_rows(provider, column, value):
    _, stock, _ = provider
    stock.history.return_value.loc[stock.history.return_value.index[0], column] = value
    history = fetch_candle_history("AAPL")
    assert history.dropped_rows == 1 and len(history.candles) == 1


def test_history_rejects_empty_wrong_identity_and_provider_errors(provider):
    _, stock, _ = provider
    stock.get_history_metadata.return_value["symbol"] = "MSFT"
    with pytest.raises(TradingError, match="different symbol"):
        fetch_candle_history("AAPL")
    stock.history.side_effect = RuntimeError("SECRET_PROVIDER_COOKIES")
    with pytest.raises(TradingError, match="Market history unavailable") as error:
        fetch_candle_history("AAPL")
    assert "SECRET" not in str(error.value)


def test_history_http_auth_policy_and_period(provider, monkeypatch):
    settings, _, _ = provider
    monkeypatch.setattr(bridge, "get_settings", lambda: settings)
    with TestClient(app) as client:
        path = "/api/v1/trading/quotes/AAPL/history"
        assert client.get(path).status_code == 401
        client.headers["X-API-Key"] = get_settings().api_key.get_secret_value()
        assert client.get(path + "?period=max").status_code == 422
        assert client.get("/api/v1/trading/quotes/SECRET/history").status_code == 403
        settings.llm_offline_mode = True
        assert client.get(path).status_code == 403
        settings.llm_offline_mode = False
        response = client.get(path)
        assert response.status_code == 200 and response.json()["candles"][0]["time"] == "2025-01-02"
