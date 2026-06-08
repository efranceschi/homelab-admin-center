"""Web console: mint a one-time token, serve the xterm page, bridge the PTY.

Three endpoints:

* ``POST /console/token`` — admin-only, CSRF-checked. Resolves the target's
  interactive command, stages key material, and returns a single-use token.
* ``GET  /console/{token}`` — serves the standalone xterm page the popup loads.
* ``WS   /console/ws/{token}`` — claims the token, spawns the PTY, and pumps
  bytes both ways until either side closes.

Auth on the WebSocket is enforced inline (the HTTP ``Depends`` guards raise HTTP
responses / redirects that don't apply to a socket upgrade): the ``hac_session``
cookie rides the same-origin handshake and ``SessionMiddleware`` populates
``websocket.session``.
"""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, Depends, Form, Request, WebSocket
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..auth import require_admin, verify_csrf
from ..console import (
    ConsoleError,
    ConsoleSession,
    manager as console_manager,
    reap,
    set_winsize,
    spawn_pty,
)
from ..db import db_dependency
from ..models import AuditLog, User
from ..templating import render

router = APIRouter(prefix="/console")

# WebSocket close codes (application range): not authorized / token unusable.
_WS_UNAUTHORIZED = 4401
_WS_BAD_TOKEN = 4404


@router.post("/token", dependencies=[Depends(verify_csrf)])
async def console_token(
    request: Request,
    target_kind: str = Form(...),
    target_id: str = Form(...),
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    """Mint a one-time console token for a host (Server) or docker container."""
    if target_kind not in ("server", "docker"):
        return JSONResponse({"detail": "unknown target kind"}, status_code=400)
    try:
        session = console_manager.issue(
            db, user_id=user.id, kind=target_kind, target_id=target_id,
        )
    except ConsoleError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    db.add(AuditLog(
        user_id=user.id, action="console.open",
        target=f"{target_kind}:{target_id}",
        detail_json=json.dumps({"label": session.label}),
    ))
    return JSONResponse({
        "token": session.token,
        "url": f"/console/{session.token}",
        "title": session.label,
    })


@router.get("/{token}")
def console_page(
    token: str,
    request: Request,
    user: User = Depends(require_admin),
):
    """Serve the standalone xterm page the popup window loads.

    Auth is the session cookie (same-origin popup inherits it); the token itself
    is validated when the page opens its WebSocket."""
    session = console_manager.peek(token)
    title = session.label if session is not None else "Console"
    return render(request, "console.html", token=token, title=title)


@router.websocket("/ws/{token}")
async def console_ws(websocket: WebSocket, token: str):
    """Claim the token, spawn the PTY, and bridge it to the WebSocket."""
    user_id = websocket.session.get("user_id")
    role = websocket.session.get("role")
    if not user_id or role != "admin":
        await websocket.close(code=_WS_UNAUTHORIZED)
        return
    session = console_manager.claim(token, int(user_id))
    if session is None:
        await websocket.close(code=_WS_BAD_TOKEN)
        return
    await websocket.accept()
    await _bridge(websocket, session)


def _apply_control(master_fd: int, text: str) -> None:
    """Handle a JSON control frame from the browser (terminal resize)."""
    try:
        ctl = json.loads(text)
    except ValueError:
        return
    if ctl.get("type") == "resize":
        try:
            set_winsize(master_fd, int(ctl.get("rows", 24)), int(ctl.get("cols", 80)))
        except (OSError, ValueError, TypeError):
            pass


async def _safe_close(websocket: WebSocket) -> None:
    try:
        await websocket.close()
    except Exception:
        pass


async def _bridge(websocket: WebSocket, session: ConsoleSession) -> None:
    """Pump bytes between the WebSocket and the session's PTY until either ends.

    Downstream (PTY -> browser) is raw binary; upstream is binary for keystrokes
    and a JSON text frame for ``resize``. On exit the child's process group is
    killed/reaped and the per-session key material is removed.
    """
    loop = asyncio.get_running_loop()
    try:
        # Neutral cwd: the SSH key (if any) is referenced by absolute path, and a
        # root shell starting in the ephemeral run dir would read oddly.
        master_fd, proc = spawn_pty(session.argv, cwd="/")
    except Exception as exc:  # spawn failure (bad binary, perms, …)
        try:
            await websocket.send_bytes(
                f"\r\n[panel] failed to start console: {exc}\r\n".encode()
            )
        except Exception:
            pass
        console_manager.close(session.token)
        await _safe_close(websocket)
        return
    session.master_fd = master_fd
    session.proc = proc

    out_q: asyncio.Queue = asyncio.Queue()

    def on_readable() -> None:
        try:
            data = os.read(master_fd, 65536)
        except OSError:
            data = b""
        if data:
            out_q.put_nowait(data)
        else:  # EOF: child exited / closed its side
            try:
                loop.remove_reader(master_fd)
            except (OSError, ValueError):
                pass
            out_q.put_nowait(None)

    loop.add_reader(master_fd, on_readable)

    async def pump_out() -> None:
        while True:
            data = await out_q.get()
            if data is None:
                return
            await websocket.send_bytes(data)

    async def pump_in() -> None:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data is not None:
                try:
                    os.write(master_fd, data)
                except OSError:
                    return
                continue
            text = msg.get("text")
            if text is not None:
                _apply_control(master_fd, text)

    out_task = asyncio.create_task(pump_out())
    in_task = asyncio.create_task(pump_in())
    # Also wake on a SIGHUP drain request (begin_drain sets this event).
    drain_task = asyncio.create_task(session.close_requested.wait())
    try:
        await asyncio.wait(
            {out_task, in_task, drain_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        try:
            loop.remove_reader(master_fd)
        except (OSError, ValueError):
            pass
        for t in (out_task, in_task, drain_task):
            t.cancel()
        # Reap off the loop: closes the master fd (HUP) then escalates killpg.
        await loop.run_in_executor(None, reap, session)
        console_manager.close(session.token)
        await _safe_close(websocket)
