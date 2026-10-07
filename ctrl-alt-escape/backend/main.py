import os
import sys
import asyncio
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from db import init_db
from services.importer import load_participants
from services.game import check_timeouts
from routers.participant import router as participant_router
from routers.admin import router as admin_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    print("=> Database initialized")
    result = load_participants()
    print(f"=> Participants loaded: {result}")

    # Start background tasks
    task = asyncio.create_task(background_tasks())
    yield
    task.cancel()

async def background_tasks():
    """Background poller: expire requests, check timeouts every 3s."""
    from services.formation import expire_pending_requests
    while True:
        try:
            expire_pending_requests()
            check_timeouts()
        except Exception as e:
            print(f"Background task error: {e}")
        await asyncio.sleep(3)

app = FastAPI(title="CTRL+ALT+ESCAPE", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(participant_router, prefix="/api")
app.include_router(admin_router, prefix="/api")

# Mount frontend
frontend_path = os.path.join(os.path.dirname(__file__), '..', 'frontend')
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")

@app.get("/health")
async def health():
    return {"status": "ok", "app": "CTRL+ALT+ESCAPE"}
