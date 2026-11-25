"""Stateless OpenAI Responses function calling with server-executed tools."""

import json
import time

import httpx

from .agent_defs import RECOMMENDATION_SCHEMA

RESPONSES_URL = "https://api.openai.com/v1/responses"


class ModelFailure(Exception):
    pass


class Responses:
    def __init__(self, api_key, timeout, transport=None):
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport

    def run(self, model, instructions, question, controller, trace):
        deadline = time.monotonic() + self.timeout
        items = [{"role": "user", "content": question}]
        with httpx.Client(headers={"Authorization": "Bearer " + self.api_key}, transport=self.transport) as client:
            for expected in (*controller.workflow, None):
                tool = controller.spec(expected) if expected else None
                payload = {
                    "model": model,
                    "store": False,
                    "include": ["reasoning.encrypted_content"],
                    "instructions": instructions,
                    "input": items,
                    "tools": [tool] if tool else [],
                    "tool_choice": "required" if tool else "none",
                    "parallel_tool_calls": False,
                    "max_output_tokens": 1800,
                }
                if tool is None:
                    payload["text"] = {"format": {
                        "type": "json_schema", "name": "controller_recommendation",
                        "strict": True, "schema": RECOMMENDATION_SCHEMA,
                    }}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelFailure("timeout")
                trace.append({"event": "model_request", "expected_tool": expected})
                try:
                    response = client.post(RESPONSES_URL, json=payload, timeout=remaining)
                    response.raise_for_status()
                    document = response.json()
                except httpx.TimeoutException as exc:
                    raise ModelFailure("timeout") from exc
                except httpx.HTTPStatusError as exc:
                    raise ModelFailure(f"provider_http_{exc.response.status_code}") from exc
                except (httpx.HTTPError, ValueError) as exc:
                    raise ModelFailure("provider_unavailable") from exc
                trace.append({"event": "model_response", "response_id": document.get("id"), "status": document.get("status")})
                if document.get("status") != "completed":
                    raise ModelFailure("incomplete_response")
                output = document.get("output", [])
                calls = [item for item in output if item.get("type") == "function_call"]
                if tool is None:
                    if calls:
                        raise ModelFailure("unexpected_tool_call")
                    try:
                        message = [part["text"] for item in output if item.get("type") == "message"
                                   for part in item.get("content", []) if part.get("type") == "output_text"]
                        return json.loads("".join(message))
                    except (KeyError, ValueError) as exc:
                        raise ModelFailure("invalid_model_output") from exc
                if len(calls) != 1 or calls[0].get("name") != tool["name"] or not calls[0].get("call_id"):
                    raise ModelFailure("required_tool_call_missing")
                call = calls[0]
                try:
                    arguments = json.loads(call["arguments"])
                    result = controller.execute(expected, arguments)
                except (ValueError, KeyError) as exc:
                    raise ModelFailure("invalid_tool_arguments") from exc
                items.extend(output)
                items.append({"type": "function_call_output", "call_id": call["call_id"],
                              "output": json.dumps(result, allow_nan=False)})
                trace.append({"event": "tool_output_returned", "tool": expected, "call_id": call["call_id"]})
