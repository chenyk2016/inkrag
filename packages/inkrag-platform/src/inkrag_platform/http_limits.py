"""HTTP 传输边界，负责正文预算、大文件背压及统一错误响应。"""

import asyncio
import re
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import JsonValue
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_UPLOAD = 20 * 1024 * 1024


class BodyBudget:
    """在ASGI接收层约束实际字节与正文时间，不信任Content-Length。"""

    def __init__(self, app: ASGIApp) -> None:
        """借入ASGI应用，不执行外部初始化。"""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """限制分块正文，不给已经进入业务提交的线程施加硬取消。"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        maximum = (
            MAX_UPLOAD
            if (
                scope["method"] == "POST"
                and scope["path"].endswith(("/documents", "/imports/staged"))
            )
            or (scope["method"] == "PUT" and scope["path"].endswith("/source"))
            else 4 * 1024 * 1024
        )
        total = 0
        deadline: float | None = None
        complete = False

        async def limited() -> Message:
            """正文读完后不再将断开监听计入上传时限。"""
            nonlocal total, deadline, complete
            if complete:
                return await receive()
            loop = asyncio.get_running_loop()
            if deadline is None:
                deadline = loop.time() + 60
            budget = min(30, deadline - loop.time())
            try:
                if budget <= 0:
                    raise TimeoutError()
                message = await asyncio.wait_for(receive(), budget)
            except TimeoutError:
                raise HTTPException(408, "请求正文读取超时") from None
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > maximum:
                    raise HTTPException(413, "请求正文超过预算")
                complete = not message.get("more_body", False)
            else:
                complete = True
            return message

        await self.app(scope, limited, send)


class CapacityGate:
    """大请求和大响应独立背压，槽位持有至ASGI响应实际发送完成。"""

    def __init__(self, app: ASGIApp) -> None:
        """每个API进程仅允许两个上传和两个资产/完整产物操作。"""
        self.app = app
        self.uploads = asyncio.Semaphore(2)
        self.responses = asyncio.Semaphore(2)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """排队前直接拒绝，避免积压上传体、线程和20MiB响应副本。"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path, method = scope["path"], scope["method"]
        gate = (
            self.uploads
            if (
                method == "POST"
                and re.fullmatch(
                    r"/api/v2/knowledge-bases/[^/]+/(?:documents|imports/staged)", path
                )
            )
            or (method == "PUT" and path.endswith("/source"))
            else self.responses
            if (
                method == "GET"
                and (
                    path.startswith("/api/v2/assets/")
                    or re.fullmatch(r"/api/v2/documents/[^/]+(?:/export|/bundle|/preview)?", path)
                    or re.fullmatch(r"/api/v2/knowledge-bases/[^/]+/imports/previews/[^/]+", path)
                )
            )
            or (method == "PATCH" and re.fullmatch(r"/api/v2/documents/[^/]+(?:/structure)?", path))
            else None
        )
        if gate is None:
            await self.app(scope, receive, send)
            return
        if gate.locked():
            response = failure("capacity_exceeded", "文件操作繁忙，请稍后重试", 429, uuid4().hex)
            response.headers["Retry-After"] = "2"
            await response(scope, receive, send)
            return
        await gate.acquire()

        async def bounded_send(message: Message) -> None:
            """分块触发服务器原生drain，避免单次大write绕过慢客户端背压。"""
            if message["type"] != "http.response.body":
                await send(message)
                return
            data = message.get("body", b"")
            if len(data) <= 65536:
                await send(message)
                return
            for offset in range(0, len(data), 65536):
                await send(
                    {
                        "type": "http.response.body",
                        "body": data[offset : offset + 65536],
                        "more_body": offset + 65536 < len(data) or message.get("more_body", False),
                    }
                )

        try:
            await self.app(scope, receive, bounded_send)
        finally:
            gate.release()


def failure(
    code: str,
    message: str,
    status: int,
    request_id: str,
    *,
    details: dict[str, JsonValue] | None = None,
) -> JSONResponse:
    """只返回安全错误与关联ID，不暴露提供方响应/SQL/凭据。"""
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                **({"details": details} if details is not None else {}),
            }
        },
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )
