"""PolicyAI FastAPI Backend."""
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import discovery, qa, claim, chat


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: seed all PDFs from policies/ folder into Supabase pgvector."""
    print("[Startup] Checking policy embeddings...")
    try:
        from scripts.startup_seeder import seed_all_policies
        seed_all_policies()
    except Exception as e:
        print(f"[Startup] Seeder warning: {e}")
    yield
    print("[Shutdown] PolicyAI backend stopping.")


app = FastAPI(
    title="PolicyAI",
    description="Health Insurance Intelligence Platform — Hybrid RAG + Hidden Conditions Detector",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(discovery.router)
app.include_router(qa.router)
app.include_router(claim.router)
app.include_router(chat.router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "PolicyAI Backend"}


@app.get("/")
async def root():
    return {
        "service": "PolicyAI",
        "docs": "/docs",
        "health": "/api/health",
        "endpoints": [
            "POST /api/discover",
            "POST /api/discover/chat",
            "POST /api/compare",
            "GET  /api/policies",
            "POST /api/upload",
            "POST /api/ask",
            "POST /api/claim-check",
            "POST /api/extract-conditions",
            "POST /api/extract-conditions-file",
            "POST /api/match-conditions",
            "GET  /api/gap-analysis/{policy_id}",
            "POST /api/chat/sessions",
            "GET  /api/chat/sessions",
            "GET  /api/chat/sessions/{id}",
            "POST /api/chat/sessions/{id}/messages",
            "DEL  /api/chat/sessions/{id}",
        ],
    }
