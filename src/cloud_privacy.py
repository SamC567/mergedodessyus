"""Keep the user's private data off cloud (non-local) model endpoints.

When a turn can reach a cloud endpoint -- the selected model or any configured
foreground fallback -- saved memories, personal-document RAG and the tools
that read private data (memory, past chats, files, mail, notes, calendar,
contacts, vault, app/admin APIs) are withheld. Only what the user types (and a
document they explicitly open in the editor) is sent.

"Local" is decided by ``model_context.is_local_endpoint``: loopback, private
LAN and Tailscale addresses, or an endpoint explicitly configured as local.
Anything that cannot be classified is treated as cloud (fail closed).

On by default. Set ODYSSEUS_CLOUD_PRIVACY_GUARD=0 to turn it off (the test
suite does, so upstream tests that drive tools through cloud URLs still run).
"""

import logging
import os
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


def guard_enabled() -> bool:
    return os.getenv("ODYSSEUS_CLOUD_PRIVACY_GUARD", "1").strip() != "0"


PRIVATE_DATA_TOOL_NAMES = frozenset({
    # memory, chat history and sessions
    "manage_memory", "search_chats", "list_sessions", "manage_session",
    "create_session", "send_to_session", "chat_with_model",
    # personal documents, notes, calendar, contacts, tasks
    "manage_documents", "manage_notes", "manage_calendar", "manage_tasks",
    "manage_contact", "resolve_contact",
    # life plans, goals and progress history
    "manage_quests",
    # email
    "read_email", "list_emails", "search_emails", "list_email_accounts",
    "download_attachment", "draft_email", "draft_email_reply",
    "ai_draft_email_reply", "reply_to_email", "send_email", "bulk_email",
    "archive_email", "delete_email", "mark_email_read",
    "scan_email_unsubscribes", "unsubscribe_email",
    # local files and execution
    "read_file", "write_file", "edit_file", "apply_patch", "ls", "glob",
    "grep", "get_workspace", "bash", "python", "pipeline",
    # secrets and app internals
    "vault_get", "vault_search", "vault_unlock", "manage_settings",
    "manage_endpoints", "manage_tokens", "manage_webhooks", "app_api",
    "api_call",
})


def is_cloud_url(url: Optional[str]) -> bool:
    """True when ``url`` is not a local endpoint. Empty URLs reach nothing."""
    if not url or not guard_enabled():
        return False
    try:
        from src.model_context import is_local_endpoint
        return not is_local_endpoint(url)
    except Exception:
        logger.warning("[cloud-privacy] could not classify endpoint; treating as cloud")
        return True


def route_is_cloud(endpoint_url: Optional[str], fallbacks: Optional[Iterable] = None) -> bool:
    """True when the primary endpoint or any ``(url, model, headers)`` fallback is cloud."""
    urls = [endpoint_url] + [fb[0] for fb in (fallbacks or []) if fb]
    return any(is_cloud_url(u) for u in urls)


def foreground_route_is_cloud(endpoint_url: Optional[str], owner: Optional[str] = None) -> bool:
    """``route_is_cloud`` including the owner's configured foreground fallbacks."""
    try:
        from src.foreground_model_routing import resolve_foreground_fallback_candidates
        fallbacks = resolve_foreground_fallback_candidates(owner)
    except Exception:
        fallbacks = []
    return route_is_cloud(endpoint_url, fallbacks)
