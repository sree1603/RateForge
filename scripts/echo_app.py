"""A tiny upstream service for trying the gateway. It reports back what it received.

Run by Docker Compose as the `echo` service (http://echo:9000 inside the network).

    GET  /anything/you/like     -> JSON describing the request the upstream received
    GET  /fast                  -> a tiny fixed response (used by the load tests)
    GET  /slow?seconds=3        -> responds after a delay (to see the 504 timeout)
    GET  /status/503            -> responds with that status code
    GET  /stream?chunks=5       -> sends 5 chunks 0.3s apart (to see streaming)
    GET  /redirect              -> 302 to an internal address (the gateway must not follow it)
"""

import asyncio
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

app = FastAPI(title="echo upstream", docs_url=None, redoc_url=None, openapi_url=None)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def echo(path: str, request: Request) -> Response:
    if path == "fast":
        return Response(b"ok", media_type="text/plain")
    if path == "slow":
        await asyncio.sleep(float(request.query_params.get("seconds", "1")))
    if path.startswith("status/"):
        return Response(status_code=int(path.split("/", 1)[1]))
    if path == "redirect":
        return Response(status_code=302, headers={"location": "http://169.254.169.254/latest/"})
    if path == "stream":
        chunks = int(request.query_params.get("chunks", "5"))

        async def generate() -> AsyncIterator[bytes]:
            for i in range(chunks):
                yield f"chunk {i}\n".encode()
                await asyncio.sleep(0.3)

        return StreamingResponse(generate(), media_type="text/plain")

    body = await request.body()
    return JSONResponse(
        {
            "method": request.method,
            "path": request.url.path,
            "query": request.url.query,
            "headers": dict(request.headers),
            "body": body.decode(errors="replace"),
            "body_bytes": len(body),
        },
        headers={"X-Upstream-Header": "hello from echo", "Set-Cookie": "a=1"},
    )
