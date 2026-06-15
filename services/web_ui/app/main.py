from html import escape as _esc
from typing import Annotated

import httpx
from fastapi import File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from platform_common.app_factory import create_service_app
from platform_common.settings import get_settings
from services.api.app.answer_trace import AnswerTraceRepository
from services.api.app.backups import BackupRepository
from services.api.app.hitl import HitlTicketRepository
from services.api.app.operators import OperatorRepository
from services.web_ui.app.admin import router as admin_router
from services.web_ui.app.auth import _api_get, _resolve_principal
from services.web_ui.app.usage_dashboard import router as usage_router

app = create_service_app("web_ui")
app.include_router(admin_router)
app.include_router(usage_router)
_settings = get_settings()
backup_repository = BackupRepository(
    db_path=_settings.backup_db_path,
    archive_dir=_settings.backup_archive_dir,
    source_paths=_settings.backup_source_path_list(),
)
answer_trace_repository = AnswerTraceRepository(
    db_path=_settings.answer_trace_db_path,
    snippet_max_chars=_settings.answer_trace_snippet_max_chars,
)
hitl_ticket_repository = HitlTicketRepository(_settings.hitl_ticket_db_path)
operator_repository = OperatorRepository(_settings.operators_db_path)


def _default_operator_username() -> str:
    operators = operator_repository.list_active()
    return operators[0].username if operators else ""


async def _forward_upload_to_api(
    *,
    operator_username: str,
    is_confidential: bool,
    inline_text: str | None,
    upload_filename: str | None,
    upload_bytes: bytes | None,
    upload_content_type: str | None,
) -> tuple[int, dict]:
    url = f"{_settings.api_internal_base_url.rstrip('/')}/knowledge/operator_upload_multipart"
    data: dict[str, str] = {
        "operator_username": operator_username,
        "is_confidential": "true" if is_confidential else "false",
    }
    if inline_text is not None:
        data["inline_text"] = inline_text
    files = None
    if upload_bytes is not None:
        files = {
            "upload": (
                upload_filename or "upload.bin",
                upload_bytes,
                upload_content_type or "application/octet-stream",
            )
        }
    async with httpx.AsyncClient(
        timeout=_settings.operator_upload_api_timeout_seconds
    ) as client:
        response = await client.post(url, data=data, files=files)
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text or "api_returned_non_json"}
    return response.status_code, body


@app.get("/", response_class=HTMLResponse)
def admin_shell() -> str:
    return """
    <!doctype html>
    <html>
      <head><title>Semantaix Admin</title></head>
      <body>
        <h1>Semantaix Admin</h1>
        <p>Bootstrap admin shell is running.</p>
        <ul>
          <li><a href='/admin/login'>Admin panel (login required)</a></li>
          <li><a href='/admin/usage'>Usage dashboard</a></li>
          <li><a href='/files'>Files (inspect extracted text)</a></li>
          <li><a href='/knowledge-upload'>Upload to knowledge base</a></li>
          <li><a href='/answer-traces'>Answer traces</a></li>
          <li><a href='/backups'>Backups</a></li>
          <li><a href='/alerts'>Alerts</a></li>
        </ul>
      </body>
    </html>
    """


@app.get("/knowledge-upload", response_class=HTMLResponse)
def knowledge_upload_form() -> str:
    operator = _esc(_default_operator_username())
    return f"""
    <!doctype html>
    <html>
      <head><title>Upload to knowledge base</title></head>
      <body>
        <h1>Upload to knowledge base</h1>
        <p>Upload a file (PDF, DOCX, PPTX, TXT, image, audio, video) <em>or</em>
           paste raw text. The submission is auto-approved and indexed into RAG
           immediately.</p>
        <form action='/knowledge-upload' method='post'
              enctype='multipart/form-data'>
          <p>
            <label>Operator username
              <input type='text' name='operator_username'
                     value='{operator}' required />
            </label>
          </p>
          <p>
            <label>File
              <input type='file' name='upload' />
            </label>
          </p>
          <p>
            <label>Or paste text
              <br />
              <textarea name='inline_text' rows='8' cols='60'></textarea>
            </label>
          </p>
          <p>
            <label>
              <input type='checkbox' name='is_confidential' value='true' />
              Confidential (mark chunks as confidential in RAG)
            </label>
          </p>
          <p><button type='submit'>Upload</button></p>
        </form>
      </body>
    </html>
    """


def _render_upload_result(status: int, body: dict) -> str:
    if status == 200:
        candidate_id = _esc(str(body.get("candidate_id", "—")))
        source_id = _esc(str(body.get("source_id", "—")))
        inserted = _esc(str(body.get("inserted_chunks", "—")))
        chars = _esc(str(body.get("extracted_chars", "—")))
        deduplicated = "yes" if body.get("deduplicated") else "no"
        confidential = "yes" if body.get("is_confidential") else "no"
        return f"""
        <!doctype html>
        <html>
          <head><title>Upload complete</title></head>
          <body>
            <h1>Upload complete</h1>
            <dl>
              <dt>Candidate id</dt><dd>{candidate_id}</dd>
              <dt>RAG source id</dt><dd>{source_id}</dd>
              <dt>Inserted chunks</dt><dd>{inserted}</dd>
              <dt>Extracted characters</dt><dd>{chars}</dd>
              <dt>Deduplicated</dt><dd>{deduplicated}</dd>
              <dt>Confidential</dt><dd>{confidential}</dd>
            </dl>
            <p><a href='/knowledge-upload'>Upload another</a></p>
          </body>
        </html>
        """
    detail = _esc(str(body.get("detail", "unknown_error")))
    return f"""
    <!doctype html>
    <html>
      <head><title>Upload failed</title></head>
      <body>
        <h1>Upload failed</h1>
        <p>The API rejected the upload with status <code>{status}</code>:
           <code>{detail}</code>.</p>
        <p><a href='/knowledge-upload'>Back to form</a></p>
      </body>
    </html>
    """


@app.post("/knowledge-upload", response_class=HTMLResponse)
async def knowledge_upload_submit(
    operator_username: Annotated[str, Form()],
    is_confidential: Annotated[bool, Form()] = False,
    inline_text: Annotated[str | None, Form()] = None,
    upload: Annotated[UploadFile | None, File()] = None,
) -> str:
    upload_filename: str | None = None
    upload_bytes: bytes | None = None
    upload_content_type: str | None = None
    if upload is not None and (upload.filename or "").strip() != "":
        upload_filename = upload.filename
        upload_bytes = await upload.read()
        upload_content_type = upload.content_type
    forwarded_inline = inline_text if inline_text and inline_text.strip() else None
    status, body = await _forward_upload_to_api(
        operator_username=operator_username,
        is_confidential=is_confidential,
        inline_text=forwarded_inline,
        upload_filename=upload_filename,
        upload_bytes=upload_bytes,
        upload_content_type=upload_content_type,
    )
    return _render_upload_result(status, body)


@app.get("/alerts", response_class=HTMLResponse)
def alerts_shell() -> str:
    return """
    <!doctype html>
    <html>
      <head><title>Semantaix Alerts</title></head>
      <body>
        <h1>Semantaix Alerts</h1>
        <p>Use API endpoints for read/unread + ack/resolve timeline in Epic 02.</p>
      </body>
    </html>
    """


def _render_trace_list_row(trace) -> str:
    grounded = "yes" if trace.grounded else "no"
    return (
        f"<tr><td><a href='/answer-traces/{_esc(trace.trace_id)}'>{_esc(trace.trace_id)}</a></td>"
        f"<td>{_esc(trace.created_at)}</td>"
        f"<td>{_esc(trace.response_mode)}</td>"
        f"<td>{_esc(trace.guardrail_outcome)}</td>"
        f"<td>{grounded}</td></tr>"
    )


@app.get("/answer-traces", response_class=HTMLResponse)
def answer_traces_list(limit: int = 50) -> str:
    traces = answer_trace_repository.list_traces(limit=limit)
    if not traces:
        body = "<p>No answer traces persisted yet.</p>"
    else:
        rows = "".join(_render_trace_list_row(trace) for trace in traces)
        body = (
            "<table border='1' cellpadding='6'>"
            "<thead><tr><th>Trace</th><th>Created</th><th>Mode</th>"
            "<th>Guardrail</th><th>Grounded</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    return f"""
    <!doctype html>
    <html>
      <head><title>Semantaix Answer Traces</title></head>
      <body>
        <h1>Answer Traces</h1>
        <p>Read-only transparency view (Epic 08 Story 02).</p>
        {body}
      </body>
    </html>
    """


def _render_sources_section(retrieval: list[dict[str, object]]) -> str:
    if not retrieval:
        return "<p><em>No retrieval hit.</em></p>"
    rows = "".join(
        "<tr>"
        f"<td>{_esc(str(item.get('chunk_id', '')))}</td>"
        f"<td>{_esc(str(item.get('source_ref', '')))}</td>"
        f"<td>{float(item.get('score', 0.0)):.3f}</td>"
        f"<td>{_esc(str(item.get('text_snippet', '')))}</td>"
        "</tr>"
        for item in retrieval
    )
    return (
        "<table border='1' cellpadding='6'>"
        "<thead><tr><th>Chunk</th><th>Source</th><th>Score</th><th>Snippet</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


@app.get("/answer-traces/{trace_id}", response_class=HTMLResponse)
def answer_trace_detail(trace_id: str) -> str:
    try:
        trace = answer_trace_repository.get_by_trace_id(trace_id)
    except LookupError:
        return f"""
        <!doctype html>
        <html>
          <head><title>Answer trace not found</title></head>
          <body>
            <h1>Answer trace not found</h1>
            <p>No record exists for trace id <code>{_esc(trace_id)}</code>. The
               suggestion may have failed before persistence — check the
               <code>answer_trace_persistence_failures</code> incident feed.</p>
            <p><a href='/answer-traces'>Back to list</a></p>
          </body>
        </html>
        """
    reasons = ", ".join(_esc(reason) for reason in trace.guardrail_reasons) or "—"
    limitations = ", ".join(_esc(item) for item in trace.limitations) or "—"
    confidence = f"{trace.confidence:.3f}" if trace.confidence is not None else "—"
    latency = f"{trace.latency_ms} ms" if trace.latency_ms is not None else "—"
    return f"""
    <!doctype html>
    <html>
      <head><title>Why this answer — {_esc(trace.trace_id)}</title></head>
      <body>
        <h1>Why this answer</h1>
        <p><strong>Trace</strong> {_esc(trace.trace_id)} —
           created {_esc(trace.created_at)}</p>
        <p><strong>Request</strong>: {_esc(trace.request_text)}</p>

        <h2>Sources</h2>
        {_render_sources_section(trace.retrieval)}

        <h2>Policy / guardrails</h2>
        <ul>
          <li><strong>Outcome</strong>: {_esc(trace.guardrail_outcome)}</li>
          <li><strong>Reasons</strong>: {reasons}</li>
          <li><strong>Response mode</strong>: {_esc(trace.response_mode)}</li>
          <li><strong>Guardrails applied</strong>: {trace.guardrails_applied}</li>
        </ul>

        <h2>Model routing</h2>
        <ul>
          <li><strong>Model</strong>: {_esc(trace.model_id or '—')}</li>
          <li><strong>Provider</strong>: {_esc(trace.model_provider or '—')}</li>
          <li><strong>Latency</strong>: {latency}</li>
        </ul>

        <h2>Confidence / limitations</h2>
        <ul>
          <li><strong>Grounded</strong>: {trace.grounded}</li>
          <li><strong>No retrieval hit</strong>: {trace.no_retrieval_hit}</li>
          <li><strong>Confidence</strong>: {confidence}</li>
          <li><strong>Limitations</strong>: {limitations}</li>
        </ul>
        <p><a href='/answer-traces'>Back to list</a></p>
      </body>
    </html>
    """




@app.get("/login", response_class=HTMLResponse)
def login_form(error: str | None = None) -> str:
    note = ""
    if error:
        note = f"<p style='color:#a00'>{_esc(error)}</p>"
    return f"""
    <!doctype html>
    <html>
      <head><title>Semantaix · Sign in</title></head>
      <body>
        <h1>Sign in to Semantaix</h1>
        <p>Enter your Telegram username and we will DM you a one-time code.</p>
        {note}
        <form action='/login/request_code' method='post'>
          <p>
            <label>Telegram username
              <input type='text' name='username' placeholder='@alice' required />
            </label>
          </p>
          <p><button type='submit'>Send code</button></p>
        </form>
      </body>
    </html>
    """


def _render_verify_form(*, username: str, error: str | None = None) -> str:
    note = ""
    if error:
        note = f"<p style='color:#a00'>{_esc(error)}</p>"
    return f"""
    <!doctype html>
    <html>
      <head><title>Semantaix · Enter code</title></head>
      <body>
        <h1>Enter the code from Telegram</h1>
        <p>A 6-digit code was sent to <code>{_esc(username)}</code>.</p>
        {note}
        <form action='/login/verify' method='post'>
          <input type='hidden' name='username' value='{_esc(username)}' />
          <p>
            <label>Code
              <input type='text' name='code' inputmode='numeric'
                     pattern='\\d{{6}}' maxlength='6' required />
            </label>
          </p>
          <p><button type='submit'>Sign in</button></p>
        </form>
        <p><a href='/login'>Send a new code</a></p>
      </body>
    </html>
    """


@app.post("/login/request_code", response_class=HTMLResponse)
async def login_request_code(username: Annotated[str, Form()]) -> HTMLResponse:
    url = f"{_settings.api_internal_base_url.rstrip('/')}/admin/auth/request_code"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, json={"username": username})
    if response.status_code == 404:
        return HTMLResponse(
            login_form(error=f"Этот @username не зарегистрирован: {username}")
        )
    if response.status_code != 200:
        return HTMLResponse(
            login_form(error=f"Не удалось отправить код (HTTP {response.status_code})")
        )
    return HTMLResponse(_render_verify_form(username=username))


@app.post("/login/verify")
async def login_verify(
    username: Annotated[str, Form()], code: Annotated[str, Form()]
) -> Response:
    url = f"{_settings.api_internal_base_url.rstrip('/')}/admin/auth/verify"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            url, json={"username": username, "code": code}
        )
    if response.status_code == 200:
        redirect = RedirectResponse(url="/files", status_code=303)
        set_cookie = response.headers.get("set-cookie")
        if set_cookie:
            redirect.raw_headers.append(
                (b"set-cookie", set_cookie.encode("latin-1"))
            )
        return redirect
    if response.status_code == 410:
        return HTMLResponse(
            _render_verify_form(
                username=username,
                error="Код устарел. Запросите новый.",
            )
        )
    if response.status_code == 429:
        return HTMLResponse(
            _render_verify_form(
                username=username,
                error="Слишком много неверных попыток. Запросите новый код.",
            )
        )
    return HTMLResponse(
        _render_verify_form(username=username, error="Неверный код.")
    )


@app.post("/logout")
async def logout_submit(request: Request) -> Response:
    cookie = request.cookies.get(_settings.web_session_cookie_name)
    if cookie:
        headers = {"Cookie": f"{_settings.web_session_cookie_name}={cookie}"}
        url = f"{_settings.api_internal_base_url.rstrip('/')}/admin/auth/logout"
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, headers=headers)
    redirect = RedirectResponse(url="/login", status_code=303)
    redirect.delete_cookie(
        key=_settings.web_session_cookie_name, path="/"
    )
    return redirect


def _format_file_size(byte_count: int | None) -> str:
    if byte_count is None:
        return "—"
    if byte_count >= 1024 * 1024:
        return f"{byte_count // (1024 * 1024)} MB"
    if byte_count >= 1024:
        return f"{byte_count // 1024} KB"
    return f"{byte_count} B"


def _render_file_list_row(item: dict) -> str:
    short_id = _esc(str(item.get("short_id", "")))
    name = _esc(str(item.get("source_file_name") or ""))
    uploader = _esc(str(item.get("uploaded_by") or ""))
    uploaded_at = _esc(str(item.get("uploaded_at") or ""))
    size = _format_file_size(item.get("file_size_bytes"))
    confidential = "🔒" if item.get("is_confidential") else ""
    kb_status = _esc(str(item.get("kb_ingest_status") or ""))
    chunks = item.get("kb_inserted_chunks")
    chunks_str = str(chunks) if chunks is not None else "—"
    chars = _esc(str(item.get("extracted_chars", 0)))
    return (
        f"<tr>"
        f"<td><a href='/files/{short_id}'>{short_id}</a></td>"
        f"<td>{name}</td>"
        f"<td>{uploader}</td>"
        f"<td>{uploaded_at}</td>"
        f"<td>{size}</td>"
        f"<td>{confidential}</td>"
        f"<td>{kb_status} · {chunks_str}</td>"
        f"<td>{chars}</td>"
        f"</tr>"
    )


def _render_search_hit_row(item: dict) -> str:
    short_id = _esc(str(item.get("short_id", "")))
    name = _esc(str(item.get("source_file_name") or ""))
    uploader = _esc(str(item.get("uploaded_by") or ""))
    uploaded_at = _esc(str(item.get("uploaded_at") or ""))
    snippet = _esc(str(item.get("snippet") or ""))
    return (
        f"<tr>"
        f"<td><a href='/files/{short_id}'>{short_id}</a></td>"
        f"<td>{name}</td>"
        f"<td>{uploader}</td>"
        f"<td>{uploaded_at}</td>"
        f"<td>{snippet}</td>"
        f"</tr>"
    )


@app.get("/files", response_class=HTMLResponse)
async def files_list(
    request: Request, q: str | None = None, owner: str | None = None
) -> Response:
    principal = await _resolve_principal(request)
    if principal is None:
        return RedirectResponse(url="/login", status_code=303)
    if q and len(q.strip()) >= 2:
        status, body = await _api_get(
            request, "/admin/files/search", params={"q": q.strip()}
        )
        rows = "".join(
            _render_search_hit_row(item) for item in body.get("items", [])
        )
        table = (
            "<table border='1' cellpadding='6'>"
            "<thead><tr><th>ID</th><th>File</th><th>Uploader</th>"
            "<th>Uploaded</th><th>Snippet</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            if rows
            else "<p>Ничего не найдено.</p>"
        )
        section_title = f"Search results for «{_esc(q.strip())}»"
    else:
        params: dict = {}
        if owner and principal.get("role") == "admin":
            params["owner"] = owner
        status, body = await _api_get(request, "/admin/files", params=params)
        rows = "".join(
            _render_file_list_row(item) for item in body.get("items", [])
        )
        table = (
            "<table border='1' cellpadding='6'>"
            "<thead><tr><th>ID</th><th>File</th><th>Uploader</th>"
            "<th>Uploaded</th><th>Size</th><th>🔒</th>"
            "<th>KB status</th><th>Chars</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            if rows
            else "<p>Файлов пока нет.</p>"
        )
        section_title = "All files" if principal.get("role") == "admin" else "Your files"
    username = _esc(str(principal.get("username", "")))
    role = _esc(str(principal.get("role", "")))
    owner_form = ""
    if principal.get("role") == "admin":
        owner_form = (
            "<input type='text' name='owner' placeholder='filter by @owner' "
            f"value='{_esc(owner or '')}' />"
        )
    return HTMLResponse(
        f"""
        <!doctype html>
        <html>
          <head><title>Semantaix · Files</title></head>
          <body>
            <p style='float:right'>
              Signed in as <strong>{username}</strong> ({role}) ·
              <form action='/logout' method='post' style='display:inline'>
                <button type='submit'>Logout</button>
              </form>
            </p>
            <h1>Files</h1>
            <form action='/files' method='get'>
              <input type='text' name='q' placeholder='Search extracted text'
                     value='{_esc(q or "")}' />
              {owner_form}
              <button type='submit'>Search</button>
            </form>
            <h2>{section_title}</h2>
            {table}
          </body>
        </html>
        """
    )


@app.get("/files/{short_id}", response_class=HTMLResponse)
async def files_detail(request: Request, short_id: str) -> Response:
    principal = await _resolve_principal(request)
    if principal is None:
        return RedirectResponse(url="/login", status_code=303)
    status, body = await _api_get(request, f"/admin/files/{short_id}")
    if status == 404:
        return HTMLResponse(
            f"""
            <!doctype html>
            <html>
              <head><title>Файл не найден</title></head>
              <body>
                <h1>Файл #{_esc(short_id)} не найден</h1>
                <p><a href='/files'>Назад к списку</a></p>
              </body>
            </html>
            """
        )
    candidate = body.get("candidate_text")
    text_block = (
        f"<pre style='white-space:pre-wrap'>{_esc(candidate)}</pre>"
        if candidate
        else (
            "<p><em>Extracted text not available for this upload.</em></p>"
            f"<p>KB status: <code>{_esc(str(body.get('kb_ingest_status', '—')))}</code></p>"
        )
    )
    confidential_badge = "🔒 confidential" if body.get("is_confidential") else ""
    chunks = body.get("kb_inserted_chunks")
    kb_status = body.get("kb_ingest_status") or "—"
    kb_line = f"{kb_status}"
    if chunks is not None:
        kb_line = f"{kb_status} · {chunks} chunks"
    return HTMLResponse(
        f"""
        <!doctype html>
        <html>
          <head><title>{_esc(str(body.get('source_file_name', short_id)))}</title></head>
          <body>
            <p><a href='/files'>← Files</a></p>
            <h1>📄 {_esc(str(body.get('source_file_name') or short_id))}</h1>
            <dl>
              <dt>Short ID</dt><dd><code>{_esc(short_id)}</code></dd>
              <dt>Uploaded by</dt><dd>{_esc(str(body.get('uploaded_by', '—')))}</dd>
              <dt>Uploaded at</dt><dd>{_esc(str(body.get('uploaded_at', '—')))}</dd>
              <dt>Size</dt><dd>{_format_file_size(body.get('file_size_bytes'))}</dd>
              <dt>Type</dt><dd>{_esc(str(body.get('source_file_type') or '—'))}</dd>
              <dt>Confidential</dt><dd>{confidential_badge or 'no'}</dd>
              <dt>KB</dt><dd>{_esc(kb_line)}</dd>
            </dl>
            <h2>Extracted text</h2>
            {text_block}
          </body>
        </html>
        """
    )


@app.get("/backups", response_class=HTMLResponse)
def backups_shell() -> str:
    latest = backup_repository.latest_successful()
    if latest is None:
        last_block = "<p>No successful backup recorded yet.</p>"
    else:
        last_block = (
            "<dl>"
            f"<dt>Backup id</dt><dd>{latest.id}</dd>"
            f"<dt>Completed at</dt><dd>{latest.completed_at}</dd>"
            f"<dt>Archive path</dt><dd>{latest.archive_path}</dd>"
            f"<dt>Size (bytes)</dt><dd>{latest.size_bytes}</dd>"
            "</dl>"
        )
    return f"""
    <!doctype html>
    <html>
      <head><title>Semantaix Backups</title></head>
      <body>
        <h1>Semantaix Backups</h1>
        <h2>Last successful backup</h2>
        {last_block}
        <h2>Restore</h2>
        <p>POST <code>/api/backups/&lt;id&gt;/restore</code> with
           <code>confirm_token=restore-&lt;id&gt;</code> and a writable
           <code>target_root</code>. Restores are blocked unless the token matches.</p>
      </body>
    </html>
    """
