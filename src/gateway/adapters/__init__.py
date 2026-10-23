"""Provider adapters: Ollama, any OpenAI-compatible endpoint, and Anthropic's Messages API,
each a ``Backend`` that speaks the chat format of ``gateway.chat``. See
docs/31-chat-and-adapters.md."""

from __future__ import annotations

from gateway.adapters.anthropic import AnthropicAdapter
from gateway.adapters.base import HttpAdapter
from gateway.adapters.ollama import OllamaAdapter
from gateway.adapters.openai_compatible import OpenAICompatibleAdapter

__all__ = ["AnthropicAdapter", "HttpAdapter", "OllamaAdapter", "OpenAICompatibleAdapter"]
