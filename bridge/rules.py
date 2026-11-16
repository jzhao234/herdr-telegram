"""Configurable per-workspace / per-agent routing and notification rule engine."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


class SnapshotLike(Protocol):
    @property
    def pane_id(self) -> str: ...

    @property
    def workspace_id(self) -> str: ...

    @property
    def workspace_label(self) -> str: ...

    @property
    def agent(self) -> str: ...

    @property
    def name(self) -> str | None: ...

    @property
    def title(self) -> str: ...


def normalize_match_key(value: str) -> str:
    """Normalize labels for resilient glob matching ('My Project' -> 'my-project', keeping '*' and '?')."""
    return re.sub(r"[^a-z0-9*?]+", "-", value.strip().lower()).strip("-")


def match_glob_token(value: str, patterns: Sequence[str]) -> bool:
    """Match a value against normalized glob patterns ('ops-*', 'prod-support', etc.)."""
    if not patterns:
        return False
    norm_val = normalize_match_key(value).replace("*", "").replace("?", "")
    raw_low = value.strip().lower()
    if not norm_val and not raw_low:
        return False
    for raw_pat in patterns:
        pat_stripped = raw_pat.strip().lower()
        if not pat_stripped:
            continue
        norm_pat = normalize_match_key(pat_stripped)
        if norm_pat and norm_val:
            if norm_pat.endswith("*") and "*" not in norm_pat[:-1] and "?" not in norm_pat:
                prefix = norm_pat[:-1].rstrip("-")
                if not prefix or norm_val == prefix or norm_val.startswith(f"{prefix}-"):
                    return True
            if fnmatch.fnmatchcase(norm_val, norm_pat):
                return True
        if fnmatch.fnmatchcase(raw_low, pat_stripped):
            return True
    return False


@dataclass(frozen=True)
class BotChannelSpec:
    """A named Telegram bot destination (e.g. 'default', 'prod', 'ops', 'nightly')."""

    name: str
    token: str
    chat_id: str = ""
    title: str = ""

    @property
    def display_title(self) -> str:
        if self.title.strip():
            return self.title.strip()
        if self.name == "default":
            return "Herdr Telegram Bridge"
        return f"Herdr Bridge ({self.name})"


@dataclass(frozen=True)
class RouteDefaults:
    """Fallback notification and summary rules ('the rest') when no [[routes]] entry matches."""

    bot: str = "default"
    idle_delay_seconds: float = 90.0
    blocked_delay_seconds: float = 30.0
    fast_track_replies: bool = True
    silent_when_green: bool = False
    hold_on_short_timers: bool = True
    suppress_support_done: bool = True
    include_output: bool = True
    summary_provider: str = "auto"
    summary_model: str = ""
    summary_style: str = ""


@dataclass(frozen=True)
class RouteRule:
    """A single ordered rule matching workspaces/agents/names/titles and overriding defaults."""

    name: str
    match_workspaces: tuple[str, ...] = ()
    match_agents: tuple[str, ...] = ()
    match_names: tuple[str, ...] = ()
    match_titles: tuple[str, ...] = ()
    bot: str | None = None
    idle_delay_seconds: float | None = None
    blocked_delay_seconds: float | None = None
    fast_track_replies: bool | None = None
    silent_when_green: bool | None = None
    hold_on_short_timers: bool | None = None
    suppress_support_done: bool | None = None
    include_output: bool | None = None
    summary_provider: str | None = None
    summary_model: str | None = None
    summary_style: str = ""

    def has_matchers(self) -> bool:
        return bool(self.match_workspaces or self.match_agents or self.match_names or self.match_titles)

    def matches(self, snap: SnapshotLike) -> bool:
        """All non-empty match_* lists are ANDed; patterns within each list are ORed."""
        if not self.has_matchers():
            return True
        if self.match_workspaces:
            ws = snap.workspace_label or snap.workspace_id
            if not match_glob_token(ws, self.match_workspaces):
                return False
        if self.match_agents and not match_glob_token(snap.agent, self.match_agents):
            return False
        if self.match_names and not match_glob_token(snap.name or "", self.match_names):
            return False
        if self.match_titles and not match_glob_token(snap.title or "", self.match_titles):
            return False
        return True


@dataclass(frozen=True)
class ResolvedRoute:
    """Effective policy resolved for a pane snapshot."""

    rule_name: str
    bot: str
    idle_delay_seconds: float
    blocked_delay_seconds: float
    fast_track_replies: bool
    silent_when_green: bool
    hold_on_short_timers: bool
    suppress_support_done: bool
    include_output: bool
    summary_provider: str
    summary_model: str
    summary_style: str


class RuleEngine:
    """Evaluates ordered RouteRules with a RouteDefaults fallback."""

    def __init__(
        self,
        defaults: RouteDefaults | None = None,
        routes: Sequence[RouteRule] = (),
    ) -> None:
        self.defaults = defaults if defaults is not None else RouteDefaults()
        self.routes = tuple(routes)

    def resolve(
        self,
        snap: SnapshotLike,
        available_bots: Collection[str] | None = None,
    ) -> ResolvedRoute:
        matched: RouteRule | None = None
        for rule in self.routes:
            if rule.matches(snap):
                matched = rule
                break

        d = self.defaults
        raw_bot = (matched.bot if (matched and matched.bot) else d.bot).strip() or "default"
        effective_bot = raw_bot if (available_bots is None or raw_bot in available_bots) else "default"

        combined_style_parts = [
            p.strip() for p in (d.summary_style, matched.summary_style if matched else "") if p.strip()
        ]

        return ResolvedRoute(
            rule_name=matched.name if matched else "defaults",
            bot=effective_bot,
            idle_delay_seconds=(
                matched.idle_delay_seconds
                if (matched and matched.idle_delay_seconds is not None)
                else d.idle_delay_seconds
            ),
            blocked_delay_seconds=(
                matched.blocked_delay_seconds
                if (matched and matched.blocked_delay_seconds is not None)
                else d.blocked_delay_seconds
            ),
            fast_track_replies=(
                matched.fast_track_replies
                if (matched and matched.fast_track_replies is not None)
                else d.fast_track_replies
            ),
            silent_when_green=(
                matched.silent_when_green
                if (matched and matched.silent_when_green is not None)
                else d.silent_when_green
            ),
            hold_on_short_timers=(
                matched.hold_on_short_timers
                if (matched and matched.hold_on_short_timers is not None)
                else d.hold_on_short_timers
            ),
            suppress_support_done=(
                matched.suppress_support_done
                if (matched and matched.suppress_support_done is not None)
                else d.suppress_support_done
            ),
            include_output=(
                matched.include_output if (matched and matched.include_output is not None) else d.include_output
            ),
            summary_provider=(
                matched.summary_provider if (matched and matched.summary_provider is not None) else d.summary_provider
            ),
            summary_model=(
                matched.summary_model if (matched and matched.summary_model is not None) else d.summary_model
            ),
            summary_style="\n".join(combined_style_parts),
        )

    def partition_by_bot(
        self,
        snapshots: Mapping[str, SnapshotLike],
        available_bots: Collection[str] | None = None,
    ) -> dict[str, dict[str, SnapshotLike]]:
        grouped: dict[str, dict[str, SnapshotLike]] = {}
        for pane_id, snap in snapshots.items():
            bot_name = self.resolve(snap, available_bots=available_bots).bot
            grouped.setdefault(bot_name, {})[pane_id] = snap
        return grouped
