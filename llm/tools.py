"""Per-turn capabilities. Cloud sessions cannot access private or trading tools."""
from agent.portfolio import get_portfolio_metrics


class RoutedToolsRegistry:
    def __init__(self, base, session):
        self.base, self.session = base, session
        self.capabilities = session.state.setdefault("routing_capabilities", {
            "private_tools": session.decision.sensitive,
            "market_tools": session.decision.market_access,
        })

    def allowed(self):
        decision, settings = self.session.decision, self.session.settings
        if not self.capabilities["private_tools"]:
            return {"get_stock_quote"} if self.capabilities["market_tools"] and not settings.llm_offline_mode else set()
        names = {"search_financial_kb", "get_portfolio"}
        if self.capabilities["market_tools"] and not settings.llm_offline_mode:
            names |= {"get_stock_quote", "execute_paper_trade"}
        return names

    async def definitions(self):
        allowed = self.allowed()
        return [tool for tool in await self.base.definitions() if tool["name"] in allowed]

    async def invoke(self, name, arguments, turn_id):
        decision, settings = self.session.decision, self.session.settings
        if name == "execute_paper_trade" and settings.llm_offline_mode and self.capabilities["private_tools"]:
            # execute_trade can return a saved receipt before its shared offline quote guard.
            return await self.base.invoke(name, arguments, turn_id)
        if name not in self.allowed():
            return {"ok": False, "error": "This tool is blocked by the routing privacy policy."}
        if name in {"get_stock_quote", "execute_paper_trade"}:
            ticker = arguments.get("ticker")
            if not isinstance(ticker, str):
                return {"ok": False, "error": "A public ticker is required."}
            allowed_tickers = set(settings.llm_public_tickers) if self.capabilities["private_tools"] else (
                set(decision.public_context.tickers) & set(settings.llm_public_tickers))
            if ticker.strip().upper() not in allowed_tickers:
                return {"ok": False, "error": "Ticker is outside this request's approved public market scope."}
        if name == "get_portfolio":
            try:
                return {"ok": True, "data": await self.metrics()}
            except Exception:
                return {"ok": False, "error": "Local portfolio snapshot unavailable."}
        return await self.base.invoke(name, arguments, turn_id)

    async def metrics(self):
        decision, settings = self.session.decision, self.session.settings
        include_quotes = self.capabilities["private_tools"] and self.capabilities["market_tools"] and not settings.llm_offline_mode
        return await get_portfolio_metrics(include_quotes=include_quotes, approved_tickers=set(settings.llm_public_tickers))
