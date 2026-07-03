"""Provider registry: config-driven registration of providers by name.

The built-in `stub` provider is always registered and remains the default.
Real providers (Anthropic/OpenAI/OpenAI-compatible/Ollama) are added by
`/connect` (see reidcli.provider.store) or by env-var auto-registration for
Anthropic, but never auto-promoted to default — the user picks with `/use`.
"""
from __future__ import annotations

from reidcli.config.models import Config
from reidcli.diagnostics.logger import get_logger
from reidcli.provider.anthropic import AnthropicProvider
from reidcli.provider.base import BaseProvider
from reidcli.provider.gemini import GeminiProvider
from reidcli.provider.stub import StubProvider

log = get_logger("reidcli.provider")


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}

    def register(self, name: str, provider: BaseProvider) -> None:
        self._providers[name] = provider
        log.debug("registered provider: %s", name)

    def unregister(self, name: str) -> bool:
        if name in self._providers:
            del self._providers[name]
            return True
        return False

    def get(self, name: str) -> BaseProvider:
        if name not in self._providers:
            raise KeyError(f"provider '{name}' not registered")
        return self._providers[name]

    def has(self, name: str) -> bool:
        return name in self._providers

    def names(self) -> list[str]:
        return list(self._providers)


def default_registry(config: Config) -> ProviderRegistry:
    """Build the default registry. Stub is always registered and stays the
    default; Anthropic auto-registers under its own name if ANTHROPIC_* env
    vars are set (available via `/use anthropic`), but never overrides the
    default. Providers persisted by `/connect` are layered on top by
    `reidcli.provider.store.load_into`.
    """
    reg = ProviderRegistry()
    reg.register("stub", StubProvider())

    anthropic = AnthropicProvider.from_env()
    if anthropic is not None:
        reg.register("anthropic", anthropic)
        log.debug("auto-registered anthropic provider from env vars")

    gemini = GeminiProvider.from_env()
    if gemini is not None:
        reg.register("gemini", gemini)
        log.debug("auto-registered gemini provider from env vars")

    for name in config.providers:
        if name in ("stub", "anthropic", "gemini"):
            continue
        log.warning("provider '%s' configured but no client implementation yet (TODO)", name)
    return reg
