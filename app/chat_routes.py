"""Chat UI and the JSON endpoint the browser talks to."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app import conversations
from app.chat import ChatUnavailable, run_chat_turn
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

    try:
        turn = await run_chat_turn(
            user_id=user_id,
            history=conversations.history(user_id),
            user_message=message,
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


@router.post("/chat/clear")
def clear_chat(user=Depends(require_user)):
    conversations.clear(str(user["id"]))
    return RedirectResponse("/chat", status_code=303)
