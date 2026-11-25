"""Native Ollama chat adapter for the scoped Controller workflow."""

import json
import re
import time

import httpx

from .agent_defs import RECOMMENDATION_SCHEMA
from .model import ModelFailure

OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
LOCAL_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?::[A-Za-z0-9][A-Za-z0-9._-]{0,127})?")


def is_local_model_name(name):
    return isinstance(name, str) and LOCAL_MODEL.fullmatch(name) is not None and not any(
        part == "cloud" or part.endswith(("-cloud", ".cloud", "_cloud"))
        for part in name.casefold().split(":")
    )


def _tagged_name(model):
    return model if ":" in model else model + ":latest"


def _invalid_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def _structured_json(message):
    try:
        return json.loads(message["content"], parse_constant=_invalid_constant)
    except (ValueError, TypeError, KeyError) as exc:
        raise ModelFailure("invalid_model_output") from exc


class Ollama:
    def __init__(self, timeout, transport=None):
        self.timeout = timeout
        self.transport = transport

    def inventory(self):
        """Return local model metadata, or None while the daemon is unreachable."""
        try:
            with httpx.Client(transport=self.transport, trust_env=False) as client:
                response = client.get(OLLAMA_TAGS_URL, timeout=1)
                response.raise_for_status()
                models = response.json()["models"]
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return None
        if not isinstance(models, list):
            return None
        result = {}
        for item in models:
            if not isinstance(item, dict) or item.get("remote_host") or item.get("remote_model"):
                continue
            name = item.get("name") or item.get("model")
            if not is_local_model_name(name):
                continue
            digest, details = item.get("digest"), item.get("details")
            result[name] = {
                "digest": digest if isinstance(digest, str) else None,
                "details": details if isinstance(details, dict) else {},
            }
        return result

    @staticmethod
    def _complete(client, payload, deadline, trace, expected, protocol, pending_tools=()):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ModelFailure("timeout")
        trace.append({"event": "model_request", "provider": "ollama", "expected_tool": expected,
                      "protocol": protocol})
        try:
            response = client.post(OLLAMA_CHAT_URL, json=payload, timeout=remaining)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ModelFailure("timeout") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                raise ModelFailure("local_model_not_installed") from exc
            raise ModelFailure(f"local_api_http_{status}") from exc
        except httpx.HTTPError as exc:
            raise ModelFailure("local_api_unavailable") from exc
        trace.extend({"event": "tool_output_returned", "provider": "ollama",
                      "tool": name, "protocol": "structured_tool"} for name in pending_tools)
        try:
            completion = response.json()
            message = completion["message"]
            done = completion["done"]
        except (ValueError, TypeError, KeyError) as exc:
            raise ModelFailure("invalid_model_output") from exc
        reason = completion.get("done_reason")
        observed_model = completion.get("model")
        complete = done is True and reason != "length"
        trace.append({
            "event": "model_response", "provider": "ollama",
            "status": "completed" if complete else "incomplete",
            "done_reason": reason, "model": observed_model, "protocol": protocol,
        })
        if completion.get("remote_host") or completion.get("remote_model"):
            raise ModelFailure("local_model_required")
        if not is_local_model_name(observed_model) or _tagged_name(observed_model) != _tagged_name(payload["model"]):
            raise ModelFailure("local_model_mismatch")
        if not complete:
            raise ModelFailure("incomplete_response")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ModelFailure("invalid_model_output")
        if message.get("tool_calls"):
            raise ModelFailure("unexpected_tool_call")
        return _structured_json(message)

    @staticmethod
    def _payload(model, system, prompt, output_tokens):
        payload = {
            "model": model, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": output_tokens},
        }
        normalized_model = model.casefold()
        if normalized_model.split(":", 1)[0] == "qwen3" and "instruct" not in normalized_model:
            payload["think"] = False
        return payload

    def propose_pi_gains(self, model, instructions, prompt, schema, trace):
        """Request one schema-constrained proposal from the verified local runtime."""
        if not is_local_model_name(model):
            raise ModelFailure("local_model_required")
        deadline = time.monotonic() + self.timeout
        payload = self._payload(model, instructions, prompt, output_tokens=320)
        payload["format"] = schema
        with httpx.Client(transport=self.transport, trust_env=False) as client:
            return self._complete(client, payload, deadline, trace, None, "structured_pi_proposal")

    def _select_tool(self, client, model, name, trace, deadline, expected):
        payload = self._payload(
            model, "You are requesting an approved, read-only Controller tool. The server supplies its inputs. "
            "Return the request as one JSON object.",
            f'Call the approved Controller tool by returning {{"tool":"{name}"}}.', output_tokens=96,
        )
        payload["format"] = {
            "type": "object",
            "properties": {"tool": {"type": "string", "enum": [name]}},
            "required": ["tool"],
            "additionalProperties": False,
        }
        if self._complete(client, payload, deadline, trace, expected, "structured_tool") != {"tool": name}:
            raise ModelFailure("unexpected_tool_call")

    def run(self, model, instructions, question, controller, trace):
        if not is_local_model_name(model):
            raise ModelFailure("local_model_required")
        deadline = time.monotonic() + self.timeout
        evidence = []
        with httpx.Client(transport=self.transport, trust_env=False) as client:
            for expected in controller.workflow:
                name = controller.spec(expected)["name"]
                self._select_tool(client, model, name, trace, deadline, expected)
                try:
                    result = controller.execute(expected, controller.args(expected))
                except (TypeError, ValueError, KeyError) as exc:
                    raise ModelFailure("invalid_tool_arguments") from exc
                evidence.append({"tool": expected, "output": result})
            prompt = (
                "Question:\n" + question
                + "\n\nWORKBENCH CONTROLLER EVIDENCE (server-generated JSON):\n"
                + json.dumps(evidence, allow_nan=False, separators=(",", ":"))
                + "\n\nReturn only the recommendation JSON and copy evidence IDs exactly. JSON schema:\n"
                + json.dumps(RECOMMENDATION_SCHEMA, separators=(",", ":"))
            )
            payload = self._payload(
                model, instructions + "\nAll Controller requests are complete. Use their supplied evidence.",
                prompt, output_tokens=512,
            )
            payload["format"] = RECOMMENDATION_SCHEMA
            return self._complete(
                client, payload, deadline, trace, None, "structured_recommendation",
                pending_tools=controller.workflow,
            )
