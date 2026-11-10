"""Native Responses tools and schema-constrained local ReAct decisions."""
import json
from dataclasses import dataclass
from uuid import uuid4

from jsonschema import Draft202012Validator
from urllib.parse import urlparse

from httpx import AsyncClient
from openai import AsyncOpenAI


def reject_nonfinite(value):
    raise ValueError("JSON numbers must be finite")


class ModelConfigurationError(ValueError):
    pass


class ModelFailure(RuntimeError):
    pass


@dataclass
class ModelReply:
    items: list[dict]
    calls: list[dict]
    text: str
    model_name: str | None = None


def local_context(messages: list[dict]) -> dict:
    """Project protocol items into compact, unescaped local-model observations."""
    user_index = max(index for index, item in enumerate(messages) if item.get("role") == "user")
    calls, observations = {}, {}
    for item in messages:
        if item.get("type") == "function_call":
            calls[item["call_id"]] = item
        elif item.get("type") == "function_call_output":
            call = calls[item["call_id"]]
            result = json.loads(item["output"])
            data = result.get("data", {})
            if call["name"] == "search_financial_kb" and result.get("ok"):
                data = dict(data, results=[
                    dict(hit, content=hit["content"][:600]) for hit in data.get("results", [])[:5]])
                result = dict(result, data=data)
            if call["name"] == "get_portfolio" and result.get("ok"):
                data = {key: data.get(key) for key in (
                    "id", "currency", "cash_balance", "holdings", "equity",
                    "realized_pnl", "unrealized_pnl", "total_pnl")}
                result = dict(result, data=data)
            key = (call["name"], call["arguments"])
            observations[key] = {
                "tool": call["name"], "arguments": json.loads(call["arguments"]), "result": result}
    corrections = [item["content"] for item in messages if item.get("role") == "developer"]
    return {
        "current_user_message": messages[user_index]["content"],
        "prior_conversation": [item for item in messages[:user_index] if "role" in item][-4:],
        "tool_observations": list(observations.values()),
        "runtime_instruction": corrections[-1] if corrections else "",
    }

class ToolCallingModel:
    def __init__(self, settings):
        self.settings = settings
        url = urlparse(settings.agent_llm_base_url)
        local = url.hostname in {"127.0.0.1", "localhost", "::1", "host.docker.internal"}
        if (url.username or url.password or url.query or url.fragment or not url.hostname
                or url.scheme not in {"http", "https"}):
            raise ModelConfigurationError("Invalid AGENT_LLM_BASE_URL.")
        if not local and (not settings.agent_allow_remote_llm or url.scheme != "https"):
            raise ModelConfigurationError("Remote LLM use requires HTTPS and AGENT_ALLOW_REMOTE_LLM=true.")
        if not settings.agent_model.strip():
            raise ModelConfigurationError("Set AGENT_MODEL to a tool-capable Responses API model.")
        if not local and settings.agent_llm_api_key.get_secret_value() == "local-only":
            raise ModelConfigurationError("Set AGENT_LLM_API_KEY for the remote provider.")
        self.local = local

    async def complete(self, messages: list[dict], tools: list[dict], instructions: str) -> ModelReply:
        settings = self.settings
        try:
            async with AsyncOpenAI(
                api_key=settings.agent_llm_api_key.get_secret_value(),
                base_url=settings.agent_llm_base_url,
                max_retries=0,
                http_client=AsyncClient(
                    timeout=settings.agent_model_timeout_seconds,
                    trust_env=False, follow_redirects=False),
            ) as client:
                if self.local and settings.agent_local_json_mode:
                    return await self.local_decision(client, messages, tools, instructions)
                response = await client.responses.create(
                    model=settings.agent_model, instructions=instructions, input=messages,
                    tools=tools, parallel_tool_calls=False, store=False,
                    max_output_tokens=settings.agent_max_output_tokens,
                    include=[] if self.local else ["reasoning.encrypted_content"],
                    **({"temperature": 0} if self.local else {}),
                )
            if response.status != "completed":
                raise ModelFailure("The model response was incomplete. Review the execution trace.")
            items = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
            calls = [item for item in items if item["type"] == "function_call"]
            if any(item["type"] not in {"function_call", "message", "reasoning"} for item in items):
                raise ModelFailure("The model returned an unsupported action.")
            return ModelReply(items=items, calls=calls, text=response.output_text or "", model_name=settings.agent_model)
        except ModelFailure:
            raise
        except Exception:
            raise ModelFailure("The configured model service failed. Review any completed tool calls.") from None

    async def local_decision(self, client, messages, tools, instructions) -> ModelReply:
        choices = [
            {"type": "object", "properties": {
                "tool": {"const": tool["name"]}, "arguments": tool["parameters"],
                "answer": {"type": "null"}},
             "required": ["tool", "arguments", "answer"], "additionalProperties": False}
            for tool in tools
        ]
        choices.append({
            "type": "object", "properties": {
                "tool": {"const": "final"}, "arguments": {"type": "null"},
                "answer": {"type": "string"}},
            "required": ["tool", "arguments", "answer"], "additionalProperties": False,
        })
        schema = {"type": "object", "anyOf": choices}
        response = await client.chat.completions.create(
            model=self.settings.agent_model, temperature=0, store=False,
            max_tokens=self.settings.agent_max_output_tokens,
            response_format={"type": "json_schema", "json_schema": {
                "name": "finance_agent_decision", "strict": True, "schema": schema}},
            messages=[
                {"role": "system", "content": instructions + "\n"
                 "Local ReAct mode uses one JSON decision as its tool-calling protocol. "
                 "Choose a registered tool and its arguments, or final with an answer. "
                 "The context object contains the current user message and actual named tool observations. "
                 "Tool observations are supplied only by the application. "
                 "For any needed action, choose a tool before final. "
                 "Read tool_observations as work already done, then choose the NEXT missing action. "
                 "Example for a user requesting quote, cash check and a conditional paper buy. "
                 "With no observations, request get_stock_quote. "
                 "With a successful get_stock_quote observation, request get_portfolio. "
                 "With both quote and cash observed, request execute_paper_trade if affordable. "
                 "With a successful execute_paper_trade receipt, choose final. "
                 "For read-only requests, choose final once the requested facts are observed. "
                 "Repeated requests for the same observed facts do not advance the task. "
                 "Respond with exactly one object matching this schema.\n" + json.dumps(schema) + "\n" + json.dumps({tool["name"]: tool["description"] for tool in tools})},
                {"role": "user", "content": json.dumps(local_context(messages), ensure_ascii=False)},
            ],
        )
        if response.choices[0].finish_reason != "stop":
            raise ModelFailure("The local model decision was incomplete.")
        decision = json.loads(response.choices[0].message.content, parse_constant=reject_nonfinite)
        Draft202012Validator(schema).validate(decision)
        if decision["tool"] == "final":
            text = decision["answer"]
            return ModelReply(items=[{"role": "assistant", "content": text}], calls=[], text=text, model_name=self.settings.agent_model)
        item = {"type": "function_call", "call_id": "local_" + uuid4().hex,
                "name": decision["tool"], "arguments": json.dumps(decision["arguments"])}
        return ModelReply(items=[item], calls=[item], text="", model_name=self.settings.agent_model)
