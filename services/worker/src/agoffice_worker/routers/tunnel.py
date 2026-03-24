from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agoffice_worker.services.vscode_tunnel import TunnelStartResult, start_tunnel

router = APIRouter()


class StartTunnelRequest(BaseModel):
    tunnel_name: str = Field(min_length=1)
    host_token: str = Field(min_length=1)


@router.post(
    "/start",
    response_model=TunnelStartResult,
)
async def start_tunnel_endpoint(req: StartTunnelRequest) -> TunnelStartResult:
    try:
        return await start_tunnel(
            tunnel_name=req.tunnel_name,
            host_token=req.host_token,
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
