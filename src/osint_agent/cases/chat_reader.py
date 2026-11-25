"""A reader that asks an OpenAI-compatible chat endpoint, such as a local model.

Each batch is a fresh conversation: the instructions, the batch, one answer.
Keeping the reading on a local model means the posts never leave the machine.
"""

import json
import os
import time
from collections.abc import Callable, Mapping

import httpx

from osint_agent.cases.reading import SUBMIT_TOOL, ReaderError

MAX_WAITS = 30  # Times to wait out a busy endpoint before giving up
LONGEST_WAIT = 120.0


def _seconds(value: str | None) -> float:
    try:
        return max(1.0, float(value or 20))
    except ValueError:
        return 20.0


def extract_flagged(data: object) -> tuple[list | None, str]:
    """The submitted list from a completion, or why there is none."""
    choices = data.get("choices") if isinstance(data, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        return None, "the reply held no message"
    for call in message.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or function.get("name") != "submit_review":
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return None, "the tool arguments were not JSON"
        if isinstance(arguments, dict) and isinstance(arguments.get("flagged"), list):
            return arguments["flagged"], ""
        return None, "the tool arguments held no list"
    content = message.get("content")
    content = content if isinstance(content, str) else ""
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            return None, "the text was not JSON"
        if isinstance(parsed, dict) and isinstance(parsed.get("flagged"), list):
            return parsed["flagged"], ""
    return None, "there was no tool call and no JSON"


class ChatReader:
    """Reads one batch of posts through a chat-completions endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        effort: str | None = "none",
        temperature: float = 0.2,
        max_tokens: int = 4000,
        pause: float = 0.0,
        client: httpx.Client | None = None,
        wait: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.name = model
        self.effort = effort
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.pause = pause  # Seconds to rest after each batch, to spare the hardware
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=600.0)
        self._wait = wait

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ChatReader | None":
        """A reader from LLM_BASE_URL, LLM_MODEL and LLM_API_KEY, if the first two are set."""
        values = os.environ if environ is None else environ
        base, model = values.get("LLM_BASE_URL", ""), values.get("LLM_MODEL", "")
        if not base or not model:
            return None
        return cls(
            base,
            model,
            values.get("LLM_API_KEY", ""),
            effort=values.get("LLM_REASONING_EFFORT", "none") or None,
            pause=(
                _seconds(values["LLM_PAUSE_SECONDS"])
                if values.get("LLM_PAUSE_SECONDS")
                else 0.0
            ),
        )

    def read(self, instructions: str, listing: str) -> list[dict]:
        payload = {
            "model": self.name,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": f"Review these posts.\n\n{listing}"},
            ],
            "tools": [SUBMIT_TOOL],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if self.effort:
            payload["reasoning_effort"] = self.effort
        problem = ""
        for _ in range(2):
            flagged, problem = extract_flagged(self._complete(payload))
            if flagged is not None:
                if self.pause:
                    self._wait(self.pause)
                return flagged
        raise ReaderError(f"the reader's answer could not be understood: {problem}")

    def _complete(self, payload: dict) -> object:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        for _ in range(MAX_WAITS):
            try:
                reply = self._client.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
            except httpx.HTTPError as error:
                raise ReaderError(
                    f"the reader could not be reached ({type(error).__name__})"
                ) from error
            if reply.status_code == 503:  # Busy or cooling down: wait as asked
                self._wait(
                    min(_seconds(reply.headers.get("Retry-After")), LONGEST_WAIT)
                )
                continue
            if reply.status_code != 200:
                raise ReaderError(f"the reader answered HTTP {reply.status_code}")
            try:
                return reply.json()
            except ValueError as error:
                raise ReaderError("the reader's reply was not JSON") from error
        raise ReaderError("the reader stayed unavailable")
