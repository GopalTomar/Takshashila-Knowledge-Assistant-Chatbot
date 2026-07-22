"""
groq_client.py — Groq LLM client wrapper
Never logs or exposes the API key.
"""

from typing import Iterator, Optional

from src import config
from src.utils import get_logger

logger = get_logger("groq_client")

_CLIENT = None


def _get_client():
    global _CLIENT
    if _CLIENT is None:
        if not config.GROQ_API_KEY:
            raise ValueError(
                "GROQ_API_KEY is not set. "
                "Add it to your .env file or Streamlit secrets."
            )
        from groq import Groq
        _CLIENT = Groq(api_key=config.GROQ_API_KEY)
    return _CLIENT


def generate(
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    temperature: float = config.DEFAULT_TEMP,
    max_tokens: int = 1500,
) -> str:
    """Single-turn generation; returns assistant text."""
    client = _get_client()
    mdl = model or config.GROQ_MODEL
    response = client.chat.completions.create(
        model=mdl,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""


def stream(
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    temperature: float = config.DEFAULT_TEMP,
    max_tokens: int = 1500,
) -> Iterator[str]:
    """Streaming generation; yields text delta strings."""
    client = _get_client()
    mdl = model or config.GROQ_MODEL
    with client.chat.completions.stream(
        model=mdl,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    ) as stream_ctx:
        for chunk in stream_ctx:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
