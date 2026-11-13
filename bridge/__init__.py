"""Modular agent registry, routing rules, and TOML/env configuration for herdr-telegram."""

from __future__ import annotations

__version__ = "0.2.0"

from bridge.agents import (
    DEFAULT_AGENT_REGISTRY,
    HERDR_BUILTIN_AGENT_KINDS,
    AgentRegistry,
    AgentSpec,
)
from bridge.config import (
    BridgeConfig,
    default_config_dir,
    default_config_toml_path,
    default_env_path,
    load_bridge_config,
    parse_env_file,
)
from bridge.media import (
    HEIC_HINT_MESSAGE,
    TELEGRAM_CAPTION_MAX_CHARS,
    ImageMetadata,
    InboundImageCandidate,
    MediaValidationError,
    PreparedOutboundMedia,
    encode_multipart_formdata,
    ensure_git_info_exclude,
    format_image_prompt,
    inspect_inbound_image_message,
    is_heic_bytes,
    prune_media_dir,
    read_outbound_image,
    resolve_media_dir,
    save_inbound_image,
    sniff_image_bytes,
)
from bridge.rules import (
    BotChannelSpec,
    ResolvedRoute,
    RouteDefaults,
    RouteRule,
    RuleEngine,
    match_glob_token,
    normalize_match_key,
)

__all__ = [
    "__version__",
    "AgentRegistry",
    "AgentSpec",
    "BotChannelSpec",
    "BridgeConfig",
    "DEFAULT_AGENT_REGISTRY",
    "HEIC_HINT_MESSAGE",
    "HERDR_BUILTIN_AGENT_KINDS",
    "ImageMetadata",
    "InboundImageCandidate",
    "MediaValidationError",
    "PreparedOutboundMedia",
    "ResolvedRoute",
    "RouteDefaults",
    "RouteRule",
    "RuleEngine",
    "TELEGRAM_CAPTION_MAX_CHARS",
    "default_config_dir",
    "default_config_toml_path",
    "default_env_path",
    "encode_multipart_formdata",
    "ensure_git_info_exclude",
    "format_image_prompt",
    "inspect_inbound_image_message",
    "is_heic_bytes",
    "load_bridge_config",
    "match_glob_token",
    "normalize_match_key",
    "parse_env_file",
    "prune_media_dir",
    "read_outbound_image",
    "resolve_media_dir",
    "save_inbound_image",
    "sniff_image_bytes",
]
