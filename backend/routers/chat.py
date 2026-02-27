"""
Chat Memory System — persistent conversational sessions stored in Supabase.

Endpoints:
  POST   /api/chat/sessions                      — create new session
  GET    /api/chat/sessions                      — list recent sessions
  GET    /api/chat/sessions/{session_id}         — get session + all messages
  POST   /api/chat/sessions/{session_id}/messages — send message + get AI response
  DELETE /api/chat/sessions/{session_id}         — delete session (cascades messages)

AI response logic:
  - Retrieve last 10 messages from DB for context
  - If context > 6000 chars: summarize oldest portion with LLM
  - Route through discover_chat logic (follow-up questions or ranked results)
  - Persist both user message and assistant response
  - Update session.updated_at and session.context with any extracted state
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from services import llm
from services.vector_store import get_client
from services.skills import PolicyRanker, hard_filter
from services.vector_store import list_catalog_policies

router = APIRouter(prefix="/api/chat", tags=["chat"])
ranker = PolicyRanker()

CONTEXT_SUMMARY_SYSTEM = """Summarize this insurance advisor conversation in 3-4 sentences.
Capture: what coverage the user needs, their budget, family size, and any pre-existing conditions mentioned.
Return ONLY the summary text, no JSON."""

CHAT_SYSTEM = """You are a friendly health insurance advisor for Indian health insurance policies.
Analyze the conversation history and decide if you have enough information to recommend policies.
Return ONLY valid JSON:
{
  "ready": true or false,
  "follow_up": "ONE specific follow-up question if not ready, null if ready",
  "extracted": {
    "needs": ["maternity", "opd", "mental_health", "ayush", "dental", "critical_illness"],
    "budget_max": null or annual premium in INR as a number,
    "members": null or number of family members,
    "preexisting_conditions": ["diabetes", "hypertension"],
    "preferred_type": null or "individual" or "family_floater" or "senior_citizen"
  }
}
Rules:
- Set ready=true if user mentioned ANY of: a health coverage need, a medical condition, a budget, or a plan type
- Set ready=false ONLY for completely vague messages like "hi", "help me", "insurance", "hello"
- If ready=false, ask ONE warm and specific follow-up question about what they need
- Extract whatever partial info you can even from incomplete queries"""

CHAT_INTRO_SYSTEM = """You are a warm health insurance advisor. Write a friendly 1-2 sentence response acknowledging what the user asked for, right before showing their policy recommendations. Be specific about what you understood. Do not say "Great!" or "Sure!" — be natural.
Return ONLY valid JSON: {"message": "your response here"}"""

NO_RESULTS_MESSAGE = (
    "No policies in our catalog match all your hard requirements. "
    "Try relaxing your budget, removing a specific coverage requirement, "
    "or changing the plan type."
)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db():
    return get_client()


def _create_session(user_id: str = "anonymous", session_name: Optional[str] = None) -> dict:
    data = {"user_id": user_id, "context": {}}
    if session_name:
        data["session_name"] = session_name
    res = _db().table("chat_sessions").insert(data).execute()
    return res.data[0]


def _get_session(session_id: str) -> dict | None:
    res = _db().table("chat_sessions").select("*").eq("id", session_id).execute()
    return res.data[0] if res.data else None


def _list_sessions(limit: int = 20) -> list[dict]:
    res = (
        _db().table("chat_sessions")
        .select("id, user_id, session_name, context, created_at, updated_at")
        .order("updated_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def _get_messages(session_id: str, limit: int = 100) -> list[dict]:
    res = (
        _db().table("chat_messages")
        .select("id, role, content, metadata, created_at")
        .eq("session_id", session_id)
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    )
    return res.data or []


def _insert_message(session_id: str, role: str, content: str, metadata: dict | None = None) -> dict:
    row = {
        "session_id": session_id,
        "role": role,
        "content": content,
        "metadata": metadata or {},
    }
    res = _db().table("chat_messages").insert(row).execute()
    return res.data[0]


def _update_session(session_id: str, context: dict):
    from datetime import datetime, timezone
    _db().table("chat_sessions").update({
        "context": context,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", session_id).execute()


def _delete_session(session_id: str):
    _db().table("chat_sessions").delete().eq("id", session_id).execute()


# ── Context management ────────────────────────────────────────────────────────

def _build_context_string(messages: list[dict]) -> str:
    """Build conversation string from last 10 messages."""
    recent = messages[-10:] if len(messages) > 10 else messages
    return "\n".join([f"{m['role'].upper()}: {m['content']}" for m in recent])


def _maybe_summarize(full_context: str) -> str:
    """If context is too long, summarize the older half to keep token count manageable."""
    if len(full_context) <= 6000:
        return full_context
    midpoint = len(full_context) // 2
    old_half = full_context[:midpoint]
    recent_half = full_context[midpoint:]
    summary = llm.chat_text(CONTEXT_SUMMARY_SYSTEM, old_half, temperature=0.1)
    return f"[EARLIER CONVERSATION SUMMARY]\n{summary}\n\n[RECENT MESSAGES]\n{recent_half}"


def _process_message(content: str, db_messages: list[dict], session_context: dict) -> dict:
    """
    Core AI response logic.
    Returns structured response dict with type, message, optional policies.
    """
    context_str = _build_context_string(db_messages)
    context_str = _maybe_summarize(context_str)

    result = llm.chat_json(CHAT_SYSTEM, f"Conversation:\n{context_str}")

    if not result.get("ready", False):
        follow_up = (
            result.get("follow_up")
            or "Could you tell me your health coverage needs, annual budget, and how many family members need coverage?"
        )
        return {"type": "question", "message": follow_up}

    requirements = result.get("extracted") or {}
    requirements["needs"] = requirements.get("needs") or []
    requirements["preexisting_conditions"] = requirements.get("preexisting_conditions") or []

    all_policies = list_catalog_policies()
    filtered = hard_filter(all_policies, requirements)

    if not filtered:
        return {
            "type": "no_results",
            "message": NO_RESULTS_MESSAGE,
            "extracted_requirements": requirements,
            "policies": [],
            "total_found": 0,
        }

    ranked = ranker.rank(requirements, filtered)

    last_user = next(
        (m["content"] for m in reversed(db_messages) if m["role"] == "user"), content
    )
    intro_result = llm.chat_json(
        CHAT_INTRO_SYSTEM,
        f"User asked: {last_user}\nExtracted needs: {requirements}",
    )
    message = intro_result.get("message") or "Here are the best policies matching your needs:"

    return {
        "type": "results",
        "message": message,
        "extracted_requirements": requirements,
        "policies": ranked[:6],
        "total_found": len(ranked),
    }


# ── Request models ────────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    user_id: Optional[str] = "anonymous"
    session_name: Optional[str] = None


class SendMessageRequest(BaseModel):
    content: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/sessions")
async def create_session(req: CreateSessionRequest):
    """Create a new chat session. Returns session_id for client to store."""
    session = _create_session(req.user_id or "anonymous", req.session_name)
    return {
        "session_id": session["id"],
        "created_at": session["created_at"],
    }


@router.get("/sessions")
async def list_sessions():
    """List the 20 most recent sessions ordered by last activity."""
    sessions = _list_sessions(limit=20)
    return {"sessions": sessions}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """Get session metadata + full message history."""
    session = _get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    messages = _get_messages(session_id)
    return {
        "session": session,
        "messages": messages,
    }


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, req: SendMessageRequest):
    """
    Process a user message:
    1. Persist user message
    2. Load last 10 messages for context
    3. Generate AI response (follow-up or ranked policies)
    4. Persist assistant response
    5. Update session context with extracted state
    6. Return AI response
    """
    session = _get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    # Persist user message
    _insert_message(session_id, "user", req.content)

    # Get all messages for context (last 10 used internally)
    db_messages = _get_messages(session_id)

    # Generate AI response
    ai_response = _process_message(req.content, db_messages, session.get("context", {}))

    # Persist assistant message with metadata
    metadata = {
        "type": ai_response.get("type"),
        "policies": ai_response.get("policies", []),
        "extracted_requirements": ai_response.get("extracted_requirements", {}),
    }
    persisted = _insert_message(session_id, "assistant", ai_response["message"], metadata)

    # Update session context with any extracted state
    extracted = ai_response.get("extracted_requirements", {})
    if extracted:
        updated_context = {**session.get("context", {})}
        if extracted.get("budget_max"):
            updated_context["budget"] = extracted["budget_max"]
        if extracted.get("preexisting_conditions"):
            updated_context["diseases"] = extracted["preexisting_conditions"]
        if extracted.get("members"):
            updated_context["family_size"] = extracted["members"]
        _update_session(session_id, updated_context)

    return {
        **ai_response,
        "message_id": persisted["id"],
        "session_id": session_id,
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete session and all its messages (cascade)."""
    session = _get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    _delete_session(session_id)
    return {"deleted": True, "session_id": session_id}
