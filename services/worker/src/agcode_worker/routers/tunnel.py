from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agcode_worker.services.vscode_tunnel import TunnelStartResult, start_tunnel

router = APIRouter()


class StartTunnelRequest(BaseModel):
    tunnel_name: str = Field(min_length=1)
    host_token: str = Field(min_length=1)


class TunnelStartedResponse(BaseModel):
    status: Literal["ok"]
    pid: int
    url: str


class TunnelAlreadyRunningResponse(BaseModel):
    status: Literal["already_running"]
    pid: int
    url: str | None = None


class TunnelManualAuthRequiredResponse(BaseModel):
    status: Literal["manual_auth_required"]
    pid: int
    redirect_url: str
    code: str


def _to_response(
    result: TunnelStartResult,
) -> TunnelStartedResponse | TunnelAlreadyRunningResponse | TunnelManualAuthRequiredResponse:
    if result.status == "ok":
        if result.url is None:
            raise HTTPException(status_code=500, detail="tunnel started without URL")
        return TunnelStartedResponse(status="ok", pid=result.pid, url=result.url)
    if result.status == "manual_auth_required":
        if result.redirect_url is None or result.code is None:
            raise HTTPException(status_code=500, detail="manual auth required without prompt details")
        return TunnelManualAuthRequiredResponse(
            status="manual_auth_required",
            pid=result.pid,
            redirect_url=result.redirect_url,
            code=result.code,
        )
    return TunnelAlreadyRunningResponse(
        status="already_running",
        pid=result.pid,
        url=result.url,
    )


@router.post(
    "/start",
    response_model=TunnelStartedResponse | TunnelAlreadyRunningResponse | TunnelManualAuthRequiredResponse,
)
async def start_tunnel_endpoint(
    req: StartTunnelRequest,
) -> TunnelStartedResponse | TunnelAlreadyRunningResponse | TunnelManualAuthRequiredResponse:
    try:
        result = await start_tunnel(
            tunnel_name=req.tunnel_name,
            host_token=req.host_token,
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return _to_response(result)
