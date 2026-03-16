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


def _to_response(
    result: TunnelStartResult,
) -> TunnelStartedResponse | TunnelAlreadyRunningResponse:
    if result.status == "ok":
        if result.url is None:
            raise HTTPException(status_code=500, detail="tunnel started without URL")
        return TunnelStartedResponse(status="ok", pid=result.pid, url=result.url)
    return TunnelAlreadyRunningResponse(
        status="already_running",
        pid=result.pid,
        url=result.url,
    )


@router.post(
    "/start",
    response_model=TunnelStartedResponse | TunnelAlreadyRunningResponse,
)
async def start_tunnel_endpoint(
    req: StartTunnelRequest,
) -> TunnelStartedResponse | TunnelAlreadyRunningResponse:
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
