"""Assistant text boundaries. A model-supplied history frame is never metadata."""
import copy
import json

from .store import Fault


_FRAME_KEYS = {"source", "kind", "content", "evidence_ref", "work_id",
               "artifact_refs", "continuity_only"}


def _reply_frame(text):
    if not isinstance(text, str) or not text.lstrip().startswith("{"):
        return None
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or not _FRAME_KEYS <= value.keys():
        return None
    if value.keys() - (_FRAME_KEYS | {"authority"}):
        return None
    if (value["kind"] != "assistant" or not isinstance(value["content"], str)
            or not isinstance(value["source"], str) or not value["source"].startswith("bot-")
            or not isinstance(value["evidence_ref"], str) or not value["evidence_ref"].startswith("msg-")
            or not (value["work_id"] is None or isinstance(value["work_id"], str))
            or not isinstance(value["artifact_refs"], list)
            or not all(isinstance(ref, str) for ref in value["artifact_refs"])
            or not isinstance(value["continuity_only"], bool)):
        return None
    return value


def reply_text(text):
    """Unwrap only an entire canonical assistant frame, at most four layers.

    Prose, fenced examples and business JSON are unchanged. Discard EVERY wrapper
    field: provenance, artifacts and authority still come exclusively from Service.
    Do not write back legacy records; sanitize their model-facing projection instead.
    """
    for _ in range(4):
        frame = _reply_frame(text)
        if frame is None:
            return text
        text = frame["content"]
        if not text.strip():
            raise Fault("invalid_model_reply_frame")
    if _reply_frame(text) is not None:
        raise Fault("invalid_model_reply_frame")
    return text


def reply_delta_text(text):
    # A partial JSON object cannot yet be distinguished from a frame. Hold that
    # prefix until it parses; ordinary prose keeps streaming and business JSON is
    # released intact once complete. No partial frame metadata reaches the UI.
    if isinstance(text, str) and text.lstrip().startswith("{"):
        try:
            json.loads(text)
        except (ValueError, RecursionError):
            return ""
    try:
        return reply_text(text)
    except Fault:
        return ""


def history_reply_text(text):
    # A legacy bad reply must not poison every subsequent user turn. Keep its
    # original evidence row, but project a notice rather than the broken wrapper.
    try:
        return reply_text(text)
    except Fault:
        return "历史回复正文为空或格式异常；可按证据引用核查原记录。"


def reply_message(message):
    """Keep normalized text consistent with Pi's replayable native message."""
    content = reply_text(message.get("content"))
    if content == message.get("content"):
        return message
    result = copy.deepcopy(message)
    result["content"] = content
    native = result.get("_pi")
    if isinstance(native, dict):
        blocks = native.get("content", [])
        # fromPi joins text blocks. Replace the text as a whole, retaining thinking
        # and tool calls verbatim so transport normalization cannot alter actions.
        new_blocks, inserted = [], False
        for block in blocks:
            if block.get("type") == "text":
                if not inserted:
                    new_blocks.append({"type": "text", "text": content})
                    inserted = True
            else:
                new_blocks.append(block)
        native["content"] = new_blocks
    return result
