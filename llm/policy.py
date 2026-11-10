"""Deterministic classification. Only a public projection can cross a cloud boundary."""
import re

from llm.schemas import PublicMarketContext, RouteDecision

PRIVATE = re.compile(
    r"\b(my|mine|our|personal|private|sensitive|confidential|bank|statement|transactions?|"
    r"expenses?|spending|grocer\w*|salary|income|rent|merchant|budget|audit|credit|debit|"
    r"account|balance|holdings?|positions?|paper|virtual|ledger|p&l|passport|ssn|"
    r"address|phone|email|password|secret|iban|upload|document|notes?)\b"
    r"|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b\d{8,}\b", re.I)
MARKET = re.compile(
    r"\b(stock|stocks|equity|equities|market|ticker|quote|earnings|valuation|"
    r"fundamentals|macro|diversification|correlation|portfolio|etf|compare|risk|scenario|trend)\b", re.I)
COMPLEX = re.compile(
    r"\b(compare|comparison|risk|scenario|trend|fundamentals|valuation|correlation|"
    r"macro|analy[sz]e|analysis|diversification|multi-hop)\b", re.I)
DRIVERS = {
    "interest_rates": r"\b(rates?|interest|yields?)\b",
    "inflation": r"\binflation\b", "earnings": r"\b(earnings|margins?|profits?)\b",
    "valuation": r"\b(valuation|multiples?)\b",
    "diversification": r"\b(diversification|correlation)\b",
    "recession": r"\brecession\b", "ai_demand": r"\b(ai|artificial intelligence)\b",
    "trade_policy": r"\b(tariffs?|trade policy)\b", "regulation": r"\bregulation\b",
    "liquidity": r"\bliquidity\b",
}
EXECUTION = re.compile(r"\b(buy|sell|execute|order|trade)\b", re.I)
ALIASES = {"apple": "AAPL", "microsoft": "MSFT", "tesla": "TSLA", "nvidia": "NVDA",
           "amazon": "AMZN", "alphabet": "GOOGL", "google": "GOOGL", "meta": "META"}


class RoutingPolicyError(ValueError):
    pass


def cloud_configured(settings) -> bool:
    key = settings.llm_cloud_api_key.get_secret_value().strip()
    return bool(settings.llm_cloud_model.strip() and key and key != "local-only")


def private_tool_context(messages) -> bool:
    public_ids = {item["call_id"] for item in messages
                  if item.get("type") == "function_call" and item.get("name") == "get_stock_quote"}
    return any(
        (item.get("type") == "function_call" and item.get("name") != "get_stock_quote")
        or (item.get("type") == "function_call_output" and item.get("call_id") not in public_ids)
        for item in messages)


def classify(message, settings, mode=None, public_context=None, history_private=False,
             private_observations=False) -> RouteDecision:
    mode = mode or settings.llm_routing_mode
    approved = set(settings.llm_public_tickers)
    inferred = [symbol for symbol in settings.llm_public_tickers
                if re.search(r"(?<![A-Za-z0-9])\$?" + re.escape(symbol) + r"(?![A-Za-z0-9])",
                             message, re.I)]
    inferred += [symbol for name, symbol in ALIASES.items()
                 if symbol in approved and re.search(r"\b" + name + r"\b", message, re.I)]
    sensitive = bool(PRIVATE.search(message) or EXECUTION.search(message)
                     or history_private or private_observations)
    market_intent = bool(MARKET.search(message) or inferred or public_context)
    context = public_context
    if context is not None and not set(context.tickers).issubset(approved):
        raise RoutingPolicyError("Public-market tickers must belong to LLM_PUBLIC_TICKERS.")
    if context is None and inferred and market_intent:
        analysis = "scenario" if re.search(r"\bscenario\b", message, re.I) else (
            "risk" if re.search(r"\brisk\b", message, re.I) else (
                "comparison" if len(set(inferred)) > 1 else "overview"))
        context = PublicMarketContext(
            tickers=list(dict.fromkeys(inferred))[:6], analysis=analysis,
            drivers=[name for name, pattern in DRIVERS.items() if re.search(pattern, message, re.I)])
    # Unrecognized free text remains private. Complexity alone cannot release it.
    sensitive = sensitive or context is None
    score = len({word.lower() for word in COMPLEX.findall(message)})
    score += int(context is not None and len(context.tickers) > 1)
    score += int(len(message.split()) >= 40)
    if context and context.analysis in {"risk", "scenario", "portfolio_analysis"}:
        score = max(score, settings.llm_complexity_threshold)
    engine, reason = "local", "routine_or_cloud_disabled"
    if sensitive:
        reason = "private_context"
    elif settings.llm_offline_mode:
        reason = "offline_mode"
    elif mode == "local_only":
        reason = "local_only"
    elif settings.llm_cloud_enabled and cloud_configured(settings):
        if mode == "cloud_only" or score >= settings.llm_complexity_threshold:
            engine, reason = "cloud", "public_complexity" if mode == "auto" else "cloud_only"
    if mode == "cloud_only":
        if sensitive:
            raise RoutingPolicyError("Private or unclassified context must use local inference.")
        if settings.llm_offline_mode or not settings.llm_cloud_enabled:
            raise RoutingPolicyError("Cloud inference is disabled by the server privacy settings.")
        if not cloud_configured(settings):
            raise RoutingPolicyError("Configure LLM_CLOUD_MODEL and LLM_CLOUD_API_KEY before using cloud.")
    return RouteDecision(
        mode=mode, engine=engine,
        provider=settings.llm_cloud_provider if engine == "cloud" else "ollama",
        model=settings.llm_cloud_model if engine == "cloud" else
              (settings.llm_local_model or settings.agent_model),
        sensitive=sensitive, conversation_private=sensitive,
        complexity_score=score, reason=reason,
        market_access=market_intent and not settings.llm_offline_mode,
        public_context=context,
    )
