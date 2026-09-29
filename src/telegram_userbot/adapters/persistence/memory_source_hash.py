"""Memory evidence hash for a canonical revision without a textual body.

Ingest keeps its body hash NULL for image-only messages. Memory manifests hash
the explicit empty envelope; image bytes have a separate sealed media digest.
Never reinterpret a redacted text/caption as an empty image message.
"""

import hashlib
from typing import Any

from sqlalchemy import and_, case, exists, select

from telegram_userbot.adapters.persistence.schema import message_media
from telegram_userbot.adapters.persistence.schema import message_revisions as revisions

EMPTY_MESSAGE_CONTENT = '{"entities":[],"kind":"none","text":null}'
EMPTY_MESSAGE_SHA256 = hashlib.sha256(EMPTY_MESSAGE_CONTENT.encode()).digest()


def evidence_hash_expression() -> Any:
    return case(
        (
            and_(
                revisions.c.body_kind == "none",
                revisions.c.text_content.is_(None),
                revisions.c.caption.is_(None),
                revisions.c.content_sha256.is_(None),
                revisions.c.entities == [],
                revisions.c.redacted_at.is_(None),
                exists(
                    select(message_media.c.id).where(
                        message_media.c.message_revision_id == revisions.c.id,
                        message_media.c.account_id == revisions.c.account_id,
                        message_media.c.media_kind.in_(("photo", "image_document")),
                    )
                ),
            ),
            EMPTY_MESSAGE_SHA256,
        ),
        else_=revisions.c.content_sha256,
    )
