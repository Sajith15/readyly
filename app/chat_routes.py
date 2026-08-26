"""Chat UI and the JSON endpoint the browser talks to."""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from time import perf_counter

from fastapi import APIRouter, Depends, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from app import conversations
from app.chat import ChatUnavailable, run_chat_turn, stream_chat_turn
from app.deps import require_user
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, user=Depends(require_user)):
    return templates.TemplateResponse(
        request,
        "chat.html",
        {"user": user, "history": conversations.history(str(user["id"]))},
    )


@router.post("/api/chat")
async def chat_api(payload: ChatRequest, user=Depends(require_user)):
    user_id = str(user["id"])
    message = payload.message.strip()
    if not message:
        return JSONResponse({"error": "Say something first."}, status_code=400)

    started = perf_counter()
    try:
        turn = await run_chat_turn(
            user_id=user_id,
            history=conversations.history(user_id),
            user_message=message,
        )
        logger.info(
            "chat turn took %.2fs (tools: %s)",
            perf_counter() - started,
            turn.tools_used or "none",
        )
    except ChatUnavailable as exc:
        logger.warning("Chat unavailable for user %s: %s", user_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception:
        # Unexpected: log the trace, hand the user something actionable.
        logger.exception("Unhandled error during chat turn for user %s", user_id)
        return JSONResponse(
            {"error": "Something went wrong handling that. Please try again."},
            status_code=500,
        )

    conversations.record(user_id, message, turn.reply)
    return {"reply": turn.reply, "tools_used": turn.tools_used}


@router.post("/api/chat/stream")
async def chat_stream(payload: ChatRequest, user=Depends(require_user)):
    """The same turn as /api/chat, delivered as server-sent events.

    The browser shows each step as it happens instead of watching a spinner, so
    a turn that spawns an MCP session and dispatches two tool calls reads as
    progress rather than as a stall.
    """
    user_id = str(user["id"])
    message = payload.message.strip()

    async def events() -> AsyncIterator[str]:
        def frame(event: dict) -> str:
            return f"data: {json.dumps(event)}\n\n"

        if not message:
            yield frame({"type": "error", "message": "Say something first."})
            return

        started = perf_counter()
        try:
            async for event in stream_chat_turn(
                user_id=user_id,
                history=conversations.history(user_id),
                user_message=message,
            ):
                if event["type"] == "done":
                    # Record only once the turn actually finished, so an
                    # abandoned request cannot poison the history.
                    conversations.record(user_id, message, event["reply"])
                    logger.info(
                        "chat turn took %.2fs (tools: %s)",
                        perf_counter() - started,
                        event["tools_used"] or "none",
                    )
                yield frame(event)
        except ChatUnavailable as exc:
            logger.warning("Chat unavailable for user %s: %s", user_id, exc)
            yield frame({"type": "error", "message": str(exc)})
        except Exception:
            logger.exception("Unhandled error during chat turn for user %s", user_id)
            yield frame(
                {
                    "type": "error",
                    "message": "Something went wrong handling that. Please try again.",
                }
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Render and most reverse proxies buffer responses by default,
            # which would hold every event back until the turn ended.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/clear")
def clear_chat(user=Depends(require_user)):
    conversations.clear(str(user["id"]))
    return RedirectResponse("/chat", status_code=303)
