"""Privacy-first turn binding, dynamic provider dispatch and content-free telemetry."""
from datetime import datetime, timezone
from time import perf_counter

from agent.llm import ModelFailure
from llm.cloud_client import CloudClient, public_projection
from llm.ollama_client import OllamaClient, OllamaUnavailable
from llm.policy import classify, cloud_configured, private_tool_context
from llm.schemas import RouteDecision


class HybridRouter:
    def __init__(self, settings, local=None, cloud=None):
        self.settings = settings
        self.local = local if local is not None else OllamaClient(settings)
        self.cloud = cloud if cloud is not None else CloudClient(settings)

    def initialize_state(self, request, state):
        if "routing" not in state:
            decision = classify(
                request.message, self.settings, request.routing_mode, request.public_market_context,
                history_private=state.get("history_private", bool(state["history"])),
                private_observations=private_tool_context(state["input"]),
            )
            state["routing"] = decision.model_dump(mode="json")
        state.setdefault("routing_capabilities", {
            "private_tools": state["routing"]["sensitive"],
            "market_tools": state["routing"]["market_access"],
        })
        state.setdefault("routing_events", [])

    def for_turn(self, request, state, registry, store):
        from agent.trading_agent import TradingAgent
        from llm.tools import RoutedToolsRegistry
        session = RoutingSession(self, state)
        return TradingAgent(session, self.settings,
                            registry=RoutedToolsRegistry(registry, session), store=store)


class RoutingSession:
    def __init__(self, router, state):
        self.router, self.state = router, state
        self.settings = router.settings
        self.decision = RouteDecision.model_validate(state["routing"])

    def localize(self, reason, private=False):
        self.decision.engine = "local"
        self.decision.provider = "ollama"
        self.decision.model = self.settings.llm_local_model or self.settings.agent_model
        self.decision.reason = reason
        self.decision.sensitive |= private
        self.decision.conversation_private |= private
        self.state["routing"] = self.decision.model_dump(mode="json")

    async def attempt(self, engine, messages, tools, instructions):
        started = perf_counter()
        event = {
            "engine": engine,
            "provider": "ollama" if engine == "local" else self.settings.llm_cloud_provider,
            "model": self.decision.model, "reason": self.decision.reason,
            "started_at": datetime.now(timezone.utc).isoformat(), "status": "error",
        }
        try:
            if engine == "local":
                reply = await self.router.local.complete(messages, tools, instructions)
                event["model"] = (getattr(reply, "model_name", None) or self.decision.model)
                self.decision.model = event["model"]
            else:
                # This is the only cloud dispatch boundary. Never forward messages/instructions.
                payload = public_projection(self.decision, messages)
                reply = await self.router.cloud.complete(payload, [
                    tool for tool in tools if tool["name"] == "get_stock_quote"])
            event["status"] = "ok"
            return reply
        finally:
            event["duration_ms"] = round((perf_counter() - started) * 1000, 3)
            self.state["routing_events"].append(event)
            self.state["routing"] = self.decision.model_dump(mode="json")

    async def complete(self, messages, tools, instructions):
        decision, settings = self.decision, self.settings
        if private_tool_context(messages):
            decision.sensitive = True
            decision.conversation_private = True
            if decision.engine == "cloud":
                if decision.mode == "cloud_only":
                    decision.reason = "private_tool_context_blocked"
                    self.state["routing"] = decision.model_dump(mode="json")
                    raise ModelFailure("Private tool context blocked cloud dispatch.")
                self.localize("private_tool_context", private=True)
            self.state["routing"] = decision.model_dump(mode="json")
        if decision.engine == "cloud":
            if settings.llm_offline_mode or not settings.llm_cloud_enabled:
                if decision.mode == "cloud_only":
                    raise ModelFailure("Cloud dispatch is disabled by the server privacy settings.")
                self.localize("cloud_disabled")
            elif decision.provider != settings.llm_cloud_provider or decision.model != settings.llm_cloud_model:
                raise ModelFailure("Cloud provider/model changed for an unfinished turn. Start a new request.")
        if decision.engine == "cloud":
            try:
                return await self.attempt("cloud", messages, tools, instructions)
            except ModelFailure:
                if decision.mode != "auto" or not settings.llm_cloud_fallback_local:
                    raise
                self.localize("cloud_failed_local_fallback")
                return await self.attempt("local", messages, tools, instructions)
        try:
            return await self.attempt("local", messages, tools, instructions)
        except OllamaUnavailable:
            can_fallback = (
                decision.mode == "auto" and not decision.conversation_private
                and not decision.sensitive and decision.public_context is not None
                and not private_tool_context(messages) and not settings.llm_offline_mode
                and settings.llm_cloud_enabled and settings.llm_public_cloud_fallback
                and cloud_configured(settings)
            )
            if not can_fallback:
                raise
            decision.engine, decision.provider = "cloud", settings.llm_cloud_provider
            decision.model, decision.reason = settings.llm_cloud_model, "public_local_unavailable"
            return await self.attempt("cloud", messages, tools, instructions)
