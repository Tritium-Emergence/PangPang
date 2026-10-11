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
# Default output cap when the context window can hold it. A planner turn that
# writes a tool batch needs room; caps below MIN_OUTPUT_TOKENS truncate it and
# block the Work (the old 1200 stall). 32768 is not a minimum: an unknown model
# whose window is 32K cannot reserve 32768 output tokens and still take a prompt.
DEFAULT_OUTPUT_TOKENS = 32768
MIN_OUTPUT_TOKENS = 4096
INPUT_RESERVE_TOKENS = 1024
WINDOW_FIT_OUTPUT_TOKENS = 8192
MAX_OUTPUT_TOKENS = 1048576
MIN_CONTEXT_WINDOW_TOKENS = MIN_OUTPUT_TOKENS + INPUT_RESERVE_TOKENS


@lru_cache(maxsize=1)
def catalog():
    return json.loads(Path(__file__).with_name("model_catalog.json").read_text(encoding="utf-8"))


def _known_model(profile):
    provider = profile.get("provider", "custom")
    if provider == "custom":
        return None
    return catalog()["providers"].get(provider, {}).get(profile.get("model", ""))


def output_ceiling(profile):
    """Largest output cap this profile can reserve and still leave prompt room."""
    caps = [MAX_OUTPUT_TOKENS]
    known = _known_model(profile)
    if known:
        caps.append(int(known["maxTokens"]))
    window = profile.get("context_window_tokens")
    if type(window) is int and window > 0:
        caps.append(window - INPUT_RESERVE_TOKENS)
    return min(caps)


def output_floor(profile):
    """Smallest output cap the operator may set. Catalog models stay at
    min(DEFAULT, maxTokens). Unknown models may go down to MIN_OUTPUT_TOKENS so a
    32K window can use a cap the model actually accepts."""
    known = _known_model(profile)
    floor = min(DEFAULT_OUTPUT_TOKENS, int(known["maxTokens"])) if known else MIN_OUTPUT_TOKENS
    return min(floor, output_ceiling(profile))


def parse_context_window(raw):
    """A context window an operator named. Anything else is absent."""
    if type(raw) is int:
        value = raw
    elif isinstance(raw, str) and raw.isdigit():
        value = int(raw)
    else:
        return None
    if MIN_CONTEXT_WINDOW_TOKENS <= value <= 2_000_000:
        return value
    return None


def operator_catalog(stored_window=None):
    """Catalog plus the output bounds a save will enforce.

    A catalog choice keeps min(stored window, that model's window). The numbers
    are computed here so the settings form does not keep a second copy of the rule.
    """
    raw = catalog()
    window = stored_window if type(stored_window) is int and stored_window > 0 else None
    providers = {}
    for provider, models in raw["providers"].items():
        copied = {}
        for name, model in models.items():
            entry = dict(model)
            model_window = int(model["contextWindow"])
            chosen = min(window, model_window) if window is not None else model_window
            prof = {"provider": provider, "model": name, "context_window_tokens": chosen}
            entry["output_floor"] = output_floor(prof)
            entry["output_ceiling"] = output_ceiling(prof)
            copied[name] = entry
        providers[provider] = copied
    custom = {"provider": "custom", "model": "custom"}
    if window is not None:
        custom["context_window_tokens"] = window
    return {"pi_version": raw.get("pi_version"), "providers": providers,
            "custom": {"output_floor": output_floor(custom), "output_ceiling": output_ceiling(custom),
                       "fitted_output": fitted_output(custom)}}


def fitted_output(profile):
    """Replacement cap when the stored value is missing, below the floor, or
    larger than the window. A tight window gets WINDOW_FIT_OUTPUT_TOKENS, not
    the historical 32768 default."""
    ceiling = output_ceiling(profile)
    if _known_model(profile):
        return min(output_floor(profile), ceiling)
    if ceiling >= DEFAULT_OUTPUT_TOKENS:
        return DEFAULT_OUTPUT_TOKENS
    return min(WINDOW_FIT_OUTPUT_TOKENS, ceiling)


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
    # Context before the output cap: the cap has to fit the window the operator named.
    requested_window = body.get("context_window_tokens", None)
    if requested_window is not None:
        if type(requested_window) is not int or not MIN_CONTEXT_WINDOW_TOKENS <= requested_window <= 2_000_000:
            raise Fault("invalid_context_window", 400)
        if known and requested_window > int(known["contextWindow"]):
            raise Fault("invalid_context_window", 400)
        p["context_window_tokens"] = requested_window
    elif known:
        current = p.get("context_window_tokens", known["contextWindow"])
        if type(current) is not int:
            current = int(known["contextWindow"])
        p["context_window_tokens"] = min(current, int(known["contextWindow"]))
    # The form sends the cap it shows. A catalog maximum above the provider's
    # hard max is clamped (the provider rejects more). A cap that consumes the
    # context window is refused with a specific code, not stored as "unavailable".
    cap = body.get("output_tokens")
    if cap is None:
        if not same or type(p.get("output_tokens")) is not int or not output_floor(p) <= p.get("output_tokens") <= output_ceiling(p):
            p["output_tokens"] = fitted_output(p)
    else:
        if type(cap) is not int or cap < output_floor(p) or cap > MAX_OUTPUT_TOKENS:
            raise Fault("invalid_output_tokens", 400)
        if known and cap > int(known["maxTokens"]):
            cap = int(known["maxTokens"])
        if cap > output_ceiling(p):
            raise Fault("output_exceeds_context_window", 400)
        p["output_tokens"] = cap
    for key in FIELDS[2:]:
        if key in body and body[key] is not None:
            p[key] = body[key]
    return validate_profile(p)
