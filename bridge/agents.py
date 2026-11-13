"""Pluggable agent adapter registry for Herdr terminal coding agents."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

HERDR_BUILTIN_AGENT_KINDS: tuple[str, ...] = (
    "claude",
    "codex",
    "gemini",
    "agy",
    "opencode",
    "cursor",
    "pi",
    "devin",
    "cline",
    "omp",
    "mastracode",
    "copilot",
    "kimi",
    "kiro",
    "droid",
    "amp",
    "grok",
    "hermes",
    "kilo",
    "qodercli",
    "qwen",
    "maki",
)

AGENT_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class AgentSpec:
    """Adapter describing how to spawn, detect models for, and clean TUI chrome from an agent."""

    name: str
    kind: str
    spawn_args: tuple[str, ...] = ()
    model_patterns: tuple[str, ...] = ()
    prompt_prefixes: tuple[str, ...] = ()
    footer_patterns: tuple[str, ...] = ()
    default_model: str = ""

    def __post_init__(self) -> None:
        norm_name = self.name.strip().lower()
        norm_kind = (self.kind or norm_name).strip().lower()
        if not AGENT_ALIAS_RE.match(norm_name):
            raise ValueError(f"Invalid agent alias {self.name!r}; must match [a-z][a-z0-9_-]{{0,31}}.")
        if not AGENT_ALIAS_RE.match(norm_kind):
            raise ValueError(f"Invalid Herdr agent kind {self.kind!r}; must match [a-z][a-z0-9_-]{{0,31}}.")
        for pat in (*self.model_patterns, *self.footer_patterns):
            re.compile(pat)
        object.__setattr__(self, "name", norm_name)
        object.__setattr__(self, "kind", norm_kind)


_BUILTIN_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        name="claude",
        kind="claude",
        model_patterns=(r"^\s*🤖\s+([^|(\n]+)",),
        prompt_prefixes=("❯",),
    ),
    AgentSpec(
        name="codex",
        kind="codex",
        spawn_args=("--no-alt-screen",),
        model_patterns=(r"^\s*([a-z0-9][a-z0-9._-]+)\s+(?:low|medium|high|xhigh|max)\s*·\s*Context\b",),
        prompt_prefixes=("›",),
    ),
    AgentSpec(
        name="gemini",
        kind="gemini",
        model_patterns=(
            r"^\s*Ctx\s+[0-9.]+%\s*│\s*([^│\n]+)",
            r"(?:auto|plan|accept-edits|default|yolo)\s*·\s*([^·\n]+?)(?:\s*·\s*(?:low|medium|high|xhigh|max))?\s*$",
        ),
        prompt_prefixes=(">", "❯"),
    ),
    AgentSpec(
        name="agy",
        kind="agy",
        model_patterns=(
            r"^\s*Ctx\s+[0-9.]+%\s*│\s*([^│\n]+)",
            r"(?:auto|plan|accept-edits)\s*·\s*([^·\n]+?)(?:\s*·\s*(?:low|medium|high|xhigh|max))?\s*$",
        ),
        prompt_prefixes=(">", "❯"),
    ),
    AgentSpec(
        name="opencode",
        kind="opencode",
        model_patterns=(r"(?i)\bmodel[:\s]+([a-z0-9][a-z0-9._/-]+)",),
        prompt_prefixes=(">", "❯"),
    ),
    AgentSpec(
        name="cursor",
        kind="cursor",
        model_patterns=(r"(?i)\bmodel[:\s]+([a-z0-9][a-z0-9._/-]+)",),
        prompt_prefixes=(">", "❯"),
    ),
)


class AgentRegistry:
    """Immutable-style registry of built-in Herdr kinds plus user-configured [agents.<name>] specs."""

    def __init__(self, extra_specs: Iterable[AgentSpec] = ()) -> None:
        specs: dict[str, AgentSpec] = {}
        for kind in HERDR_BUILTIN_AGENT_KINDS:
            specs[kind] = AgentSpec(name=kind, kind=kind)
        for builtin in _BUILTIN_SPECS:
            specs[builtin.name] = builtin
        for custom in extra_specs:
            base = specs.get(custom.name) or specs.get(custom.kind)
            if base is not None:
                merged = replace(
                    base,
                    name=custom.name,
                    kind=custom.kind,
                    spawn_args=custom.spawn_args if custom.spawn_args else base.spawn_args,
                    model_patterns=custom.model_patterns + base.model_patterns,
                    prompt_prefixes=custom.prompt_prefixes if custom.prompt_prefixes else base.prompt_prefixes,
                    footer_patterns=custom.footer_patterns + base.footer_patterns,
                    default_model=custom.default_model or base.default_model,
                )
                specs[custom.name] = merged
            else:
                specs[custom.name] = custom
        self._specs = specs
        self._compiled_model: dict[str, tuple[re.Pattern[str], ...]] = {
            name: tuple(re.compile(p, re.IGNORECASE) for p in spec.model_patterns) for name, spec in specs.items()
        }
        all_model_pats: list[str] = []
        for spec in specs.values():
            for p in spec.model_patterns:
                if p not in all_model_pats:
                    all_model_pats.append(p)
        self._fallback_model_regexes: tuple[re.Pattern[str], ...] = tuple(
            re.compile(p, re.IGNORECASE) for p in all_model_pats
        )
        self._compiled_footers: dict[str, tuple[re.Pattern[str], ...]] = {
            name: tuple(re.compile(p, re.IGNORECASE) for p in spec.footer_patterns)
            for name, spec in specs.items()
            if spec.footer_patterns
        }

    @property
    def specs(self) -> Mapping[str, AgentSpec]:
        return self._specs

    @property
    def allowed_names(self) -> tuple[str, ...]:
        primary = [k for k in ("claude", "codex", "gemini", "agy") if k in self._specs]
        rest = sorted(k for k in self._specs if k not in primary)
        return tuple(primary + rest)

    def is_supported(self, name: str) -> bool:
        return name.strip().lower() in self._specs

    def get(self, name: str) -> AgentSpec | None:
        return self._specs.get(name.strip().lower())

    def infer_alias(self, agent_kind: str, agent_name: str | None) -> str:
        """Recover a custom [agents.<alias>] profile from Herdr's (kind, agent_name) pair across restarts."""
        kind_key = agent_kind.strip().lower()
        raw_name = (agent_name or "").strip().lower()
        if not raw_name:
            return kind_key
        custom_matches = sorted(
            (spec.name for spec in self._specs.values() if spec.name != spec.kind and spec.kind == kind_key),
            key=len,
            reverse=True,
        )
        for alias in custom_matches:
            if raw_name == alias or raw_name.startswith(f"{alias}-") or raw_name.startswith(f"{alias}_"):
                return alias
        return kind_key

    def with_overrides(self, extra_specs: Sequence[AgentSpec]) -> AgentRegistry:
        if not extra_specs:
            return self
        return AgentRegistry(extra_specs=extra_specs)

    def model_regexes_for(self, agent_kind: str) -> tuple[re.Pattern[str], ...]:
        key = agent_kind.strip().lower()
        specific = self._compiled_model.get(key, ())
        if not specific:
            return self._fallback_model_regexes
        seen = {r.pattern for r in specific}
        combined = list(specific) + [r for r in self._fallback_model_regexes if r.pattern not in seen]
        return tuple(combined)

    def matches_custom_footer(self, line: str, agent_kind: str = "") -> bool:
        key = agent_kind.strip().lower()
        if key and key in self._compiled_footers:
            return any(rx.search(line) for rx in self._compiled_footers[key])
        return any(rx.search(line) for rx_group in self._compiled_footers.values() for rx in rx_group)

    def prompt_prefixes_for(self, agent_kind: str = "") -> tuple[str, ...]:
        spec = self.get(agent_kind)
        if spec and spec.prompt_prefixes:
            return spec.prompt_prefixes
        return ("❯", "›", ">")


DEFAULT_AGENT_REGISTRY = AgentRegistry()
