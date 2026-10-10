"""Small configuration boundary; Pi owns model protocols and thinking mapping."""
import copy
import json
from functools import lru_cache
from pathlib import Path
from .store import Fault

APIS = {"openai-completions", "openai-responses", "anthropic-messages", "google-generative-ai"}
LEVELS = {"default", "off", "minimal", "low", "medium", "high", "xhigh", "max"}
FORMATS = {"auto", "openai", "qwen", "qwen-chat-template", "deepseek", "zai", "openrouter", "together"}
BUDGET_FIELDS = {"", "thinking_token_budget", "thinking_budget", "thinking_budget_tokens"}
FIELDS = ("provider", "api", "reasoning_level", "thinking_format", "thinking_budget_field",
          "thinking_budget", "max_tokens_field")
# Default and minimum output cap. A planner turn that writes a tool batch
# needs room; a too-small cap truncates it and blocks the Work.
DEFAULT_OUTPUT_TOKENS = 32768
MAX_OUTPUT_TOKENS = 1048576


@lru_cache(maxsize=1)
def catalog():
    return json.loads(Path(__file__).with_name("model_catalog.json").read_text(encoding="utf-8"))


def output_floor(profile):
    """Smallest output cap a profile may hold: DEFAULT_OUTPUT_TOKENS, or the model's
    own maximum when the Pi catalog lists a lower one (the provider rejects more)."""
    known = catalog()["providers"].get(profile.get("provider", "custom"), {}).get(profile.get("model", ""))
    return min(DEFAULT_OUTPUT_TOKENS, known["maxTokens"]) if known else DEFAULT_OUTPUT_TOKENS


def validate_profile(profile):
    provider = profile.get("provider", "custom")
    api = profile.get("api", "openai-completions")
    level = profile.get("reasoning_level", "default")
    fmt = profile.get("thinking_format", "auto")
    if not all(isinstance(x, str) for x in (provider, api, level, fmt)):
        raise Fault("invalid_model_provider_config", 400)
    if api not in APIS or level not in LEVELS or fmt not in FORMATS:
        raise Fault("invalid_model_provider_config", 400)
    if provider != "custom":
        known = catalog()["providers"].get(provider, {}).get(profile.get("model", ""))
        if not known:
            raise Fault("model_not_in_pi_catalog_use_custom", 400)
        if api != known["api"]:
            raise Fault("model_provider_api_mismatch", 400)
        if level not in {"default", "off"} and not known.get("reasoning"):
            raise Fault("model_does_not_support_thinking", 400)
        if level != "default" and known.get("thinkingLevelMap", {}).get(level, True) is None:
            raise Fault("model_thinking_level_unsupported", 400)
    if api != "openai-completions" and (fmt != "auto" or profile.get("thinking_budget_field")):
        raise Fault("thinking_format_requires_chat_completions", 400)
    if not isinstance(profile.get("thinking_budget_field", ""), str) or profile.get("thinking_budget_field", "") not in BUDGET_FIELDS:
        raise Fault("invalid_thinking_budget_field", 400)
    budget = profile.get("thinking_budget")
    if budget is not None and (type(budget) is not int or not 1 <= budget <= 1048576):
        raise Fault("invalid_thinking_budget", 400)
    if not isinstance(profile.get("max_tokens_field", "auto"), str) or profile.get("max_tokens_field", "auto") not in {"auto", "max_tokens", "max_completion_tokens"}:
        raise Fault("invalid_max_tokens_field", 400)
    gen = profile.get("generation_settings", {})
    allowed = {"temperature", "top_p", "frequency_penalty", "presence_penalty", "seed",
               "reasoning_effort", "chat_template_kwargs"} if api == "openai-completions" else {"temperature"}
    if not isinstance(gen, dict) or set(gen) - allowed:
        raise Fault("invalid_generation_settings", 400)
    # Legacy raw thinking remains supported, but cannot silently override a new selection.
    if level != "default" and {"reasoning_effort", "chat_template_kwargs"} & set(gen):
        raise Fault("conflicting_thinking_settings", 400)
    return profile


def setup_profile(old, body):
    """Resolve a Pi catalog choice or explicit custom API, without guessing a model name."""
    p = copy.deepcopy(old)
    provider = body.get("provider", "custom")
    name = str(body.get("model", "")).strip()[:200]
    if not isinstance(provider, str):
        raise Fault("invalid_model_provider_config", 400)
    known = catalog()["providers"].get(provider, {}).get(name) if provider != "custom" else None
    if provider != "custom" and not known:
        raise Fault("model_not_in_pi_catalog_use_custom", 400)
    api = known["api"] if known else body.get("api", "openai-completions")
    address = str(body.get("endpoint", "")).strip() or (known["baseUrl"] if known else "")
    same = (provider, name, api, address.rstrip("/")) == (
        old.get("provider", "custom"), old.get("model"), old.get("api", "openai-completions"),
        old.get("endpoint", "").rstrip("/"))
    for key in FIELDS:
        p.pop(key, None)
    # Preserve explicitly stored legacy settings for an unchanged profile. Changing
    # provider/model/endpoint or selecting a new thinking mode clears stale parameters.
    if body.get("keep_generation_settings") is False or not same or body.get("reasoning_level", "default") != "default" or body.get("thinking_format", "auto") != old.get("thinking_format", "auto"):
        p.pop("generation_settings", None)
    p.update(provider=provider, api=api, model=name, endpoint=address,
             vision=body.get("vision") is True)
    # The form sends the cap it shows; without one, a different model starts from the default.
    cap = body.get("output_tokens")
    if cap is not None:
        if type(cap) is not int or not output_floor(p) <= cap <= MAX_OUTPUT_TOKENS:
            raise Fault("invalid_output_tokens", 400)
        p["output_tokens"] = cap
    elif not same:
        p["output_tokens"] = DEFAULT_OUTPUT_TOKENS
    if known:
        p["context_window_tokens"] = min(p.get("context_window_tokens", known["contextWindow"]), known["contextWindow"])
        p["output_tokens"] = min(p.get("output_tokens", known["maxTokens"]), known["maxTokens"])
    for key in FIELDS[2:]:
        if key in body and body[key] is not None:
            p[key] = body[key]
    return validate_profile(p)
