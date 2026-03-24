import logging
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from agoffice_api.realtime.socketio_proxy import SOCKETIO_PATH, sio
from agoffice_api.routers.mission import router as mission_router
from agoffice_api.routers.session import router as session_router
from agoffice_infra.db.database import init_database


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    yield

fastapi_app = FastAPI(
    title="agoffice",
    lifespan=lifespan,
)
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
fastapi_app.include_router(session_router, prefix="/session", tags=["session"])
fastapi_app.include_router(mission_router, prefix="/mission", tags=["mission"])

logging.basicConfig(level=logging.DEBUG, force=True)

@fastapi_app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    logging.error(f"caught exception: {exc.detail}", exc_info=True)
    return exc


@fastapi_app.get("/health")
async def health():
    return {"status": "ok"}


app = socketio.ASGIApp(
    sio,
    other_asgi_app=fastapi_app,
    socketio_path=SOCKETIO_PATH.lstrip("/"),
)
