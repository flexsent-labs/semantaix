from __future__ import annotations

import httpx


class ApiError(httpx.HTTPStatusError):
    """HTTPStatusError that also carries the API's structured `detail`.

    Subclasses HTTPStatusError so existing `except httpx.HTTPStatusError`
    sites keep working unchanged; new callers can read `.detail` for the
    API-level reason (e.g. ``"empty_text"``, ``"unsupported_source_file_type"``).
    """

    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request,
        response: httpx.Response,
        detail: str | None,
    ) -> None:
        super().__init__(message, request=request, response=response)
        self.detail = detail


def _extract_detail(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        raw = body.get("detail")
        if isinstance(raw, str):
            return raw
        if raw is not None:
            return str(raw)
    return None


def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ApiError(
            str(exc),
            request=exc.request,
            response=exc.response,
            detail=_extract_detail(exc.response),
        ) from exc


class ApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: int = 10,
        internal_token: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._internal_token = internal_token

    def _internal_headers(self) -> dict[str, str]:
        if not self._internal_token:
            return {}
        return {"X-Internal-Token": self._internal_token}

    async def forward_inbound(
        self,
        *,
        text: str,
        chat_id: int,
        customer_username: str | None,
        trace_id: str,
    ) -> dict:
        payload = {
            "text": text,
            "chat_id": chat_id,
            "customer_username": customer_username,
            "trace_id": trace_id,
        }
        return await self._post("/conversations/inbound", payload)

    async def deliver_operator_reply(
        self, *, ticket_id: int, operator_username: str, reply_text: str
    ) -> dict:
        payload = {"operator_username": operator_username, "reply_text": reply_text}
        return await self._post(f"/hitl/tickets/{ticket_id}/reply", payload)

    async def submit_operator_upload(
        self,
        *,
        operator_username: str,
        source_file_type: str,
        source_file_name: str | None,
        stored_binary_path: str | None,
        is_confidential: bool,
        inline_text: str | None = None,
        operator_short_id: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict:
        payload = {
            "operator_username": operator_username,
            "source_file_type": source_file_type,
            "source_file_name": source_file_name,
            "stored_binary_path": stored_binary_path,
            "is_confidential": is_confidential,
            "inline_text": inline_text,
            "operator_short_id": operator_short_id,
        }
        return await self._post(
            "/knowledge/operator_upload",
            payload,
            timeout_override=timeout_seconds,
        )

    async def add_sales_service(
        self,
        *,
        project_id: int,
        name: str,
        description_md: str | None,
        tags: list[str] | None,
        internal_token: str,
    ) -> dict:
        """Story 12.02: create a row on the sales services catalog.

        Returns ``{"id": int}`` on success. Raises :class:`ApiError` with
        ``.detail == "service_already_exists"`` on a 409 duplicate so the
        slash-command dispatcher can DM a one-line "уже есть" message.
        """
        response = await self._bearer_post(
            "/sales/services",
            internal_token=internal_token,
            json={
                "project_id": project_id,
                "name": name,
                "description_md": description_md,
                "tags": tags,
            },
        )
        _raise_for_status(response)
        return response.json()

    async def list_sales_services(
        self,
        *,
        project_id: int,
        internal_token: str,
    ) -> dict:
        """Story 12.02: list active sales services for a project."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/sales/services",
                params={"project_id": project_id},
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        _raise_for_status(response)
        return response.json()

    async def delete_sales_service(
        self,
        *,
        service_id: int,
        internal_token: str,
    ) -> dict:
        """Story 12.02: soft-delete a sales service row."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.delete(
                f"{self._base_url}/sales/services/{service_id}",
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        _raise_for_status(response)
        return response.json()

    async def get_sales_state(
        self,
        *,
        project_id: int,
        chat_id: int | None,
        internal_token: str,
    ) -> dict:
        """Story 12.02: read the sales-conversation state for a project.

        Optional ``chat_id`` filters server-side so the ``/sales_state @name``
        command doesn't have to fetch + scan the whole project.
        """
        params: dict[str, object] = {"project_id": project_id}
        if chat_id is not None:
            params["chat_id"] = chat_id
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/sales/state",
                params=params,
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        _raise_for_status(response)
        return response.json()

    async def add_sales_material(
        self,
        *,
        project_id: int,
        kind: str,
        local_path: str,
        byte_size: int,
        internal_token: str,
        duration_seconds: int | None = None,
        caption: str | None = None,
        tags: list[str] | None = None,
        telegram_file_id: str | None = None,
        source_operator_file_id: str | None = None,
    ) -> dict:
        """Story 12.05: register a client material row from the `/material` bot command."""
        response = await self._bearer_post(
            "/sales/materials",
            internal_token=internal_token,
            json={
                "project_id": project_id,
                "kind": kind,
                "local_path": local_path,
                "byte_size": byte_size,
                "duration_seconds": duration_seconds,
                "caption": caption,
                "tags": tags,
                "telegram_file_id": telegram_file_id,
                "source_operator_file_id": source_operator_file_id,
            },
        )
        _raise_for_status(response)
        return response.json()

    async def list_sales_materials(
        self,
        *,
        project_id: int,
        internal_token: str,
    ) -> dict:
        """Story 12.05: list active client materials for a project."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/sales/materials",
                params={"project_id": project_id},
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        _raise_for_status(response)
        return response.json()

    async def delete_sales_material(
        self,
        *,
        material_id: int,
        internal_token: str,
    ) -> dict:
        """Story 12.05: soft-delete a client material row."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.delete(
                f"{self._base_url}/sales/materials/{material_id}",
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        _raise_for_status(response)
        return response.json()

    async def dispatch_sales_material(
        self,
        *,
        chat_id: int,
        material_id: int,
        internal_token: str,
        caption_override: str | None = None,
        trace_id: str | None = None,
    ) -> dict:
        """Story 12.05: send a registered material to a customer chat."""
        response = await self._bearer_post(
            "/sales/dispatch/material",
            internal_token=internal_token,
            json={
                "chat_id": chat_id,
                "material_id": material_id,
                "caption_override": caption_override,
                "trace_id": trace_id,
            },
        )
        _raise_for_status(response)
        return response.json()

    async def analyze_kb_material(
        self,
        *,
        project_id: int,
        operator_file_short_id: str,
        internal_token: str,
    ) -> dict:
        """Call the 12.05b client-materials analyzer endpoint.

        Returns the api body ``{registered: bool, material_id: int|None,
        reason: str}``. Raises ``ApiError`` on a non-2xx response so the
        caller can surface a redacted error in the operator-facing DM (or,
        per the story, swallow it and keep the KB ack visible).
        """
        response = await self._bearer_post(
            "/sales/materials/analyze-kb-file",
            internal_token=internal_token,
            json={
                "project_id": project_id,
                "operator_file_short_id": operator_file_short_id,
            },
        )
        _raise_for_status(response)
        return response.json()

    async def extract_kb_services(
        self,
        *,
        project_id: int,
        operator_file_short_id: str,
        internal_token: str,
    ) -> dict:
        """Call the 12.05c services extractor endpoint.

        Returns the api body ``{added: list, skipped_existing: list, reason:
        str}``. Raises ``ApiError`` on a non-2xx response — the caller in the
        bot KB-upload hook swallows the exception, logs it, and keeps the
        KB ack visible (services-extract failure must never block the
        materials hook or the bare KB ack).
        """
        response = await self._bearer_post(
            "/sales/services/extract-from-kb-file",
            internal_token=internal_token,
            json={
                "project_id": project_id,
                "operator_file_short_id": operator_file_short_id,
            },
        )
        _raise_for_status(response)
        return response.json()

    async def initiate_calendar_connect(
        self,
        *,
        project_id: int,
        operator: str,
        internal_token: str,
    ) -> dict:
        """Mint a Google consent URL for the project's calendar operator.

        Calls the api `POST /calendar/connect/initiate` (story 11.02) with the
        internal service token. The returned ``consent_url`` carries a single-use
        ``state`` — callers must never log it.
        """
        response = await self._bearer_post(
            "/calendar/connect/initiate",
            internal_token=internal_token,
            json={"project_id": project_id, "operator": operator},
        )
        _raise_for_status(response)
        return response.json()

    async def disconnect_calendar(
        self,
        *,
        project_id: int,
        operator: str,
        internal_token: str,
    ) -> dict:
        """Revoke + delete the operator's stored calendar token.

        Calls the api `POST /calendar/disconnect` (story 11.02) with the
        internal service token.
        """
        response = await self._bearer_post(
            "/calendar/disconnect",
            internal_token=internal_token,
            json={"project_id": project_id, "operator": operator},
        )
        _raise_for_status(response)
        return response.json()

    async def calendar_disable(
        self,
        *,
        project_id: int,
        actor: str,
        actor_role: str,
        internal_token: str,
    ) -> dict:
        """Disable a project's calendar (story 11.08); keeps the stored token."""
        response = await self._bearer_post(
            f"/calendar/projects/{project_id}/disable",
            internal_token=internal_token,
            json={"actor": actor, "actor_role": actor_role},
        )
        _raise_for_status(response)
        return response.json()

    async def calendar_get_settings(
        self,
        *,
        project_id: int,
        internal_token: str,
    ) -> dict:
        """Fetch enablement + service rules for a project (story 11.08)."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/calendar/projects/{project_id}/settings",
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        _raise_for_status(response)
        return response.json()

    async def calendar_upsert_service(
        self,
        *,
        project_id: int,
        actor: str,
        actor_role: str,
        internal_token: str,
        rule_id: int | None = None,
        name: str | None = None,
        duration_minutes: int | None = None,
        working_hours: dict | None = None,
        service_days: list | None = None,
        date_exceptions: list | None = None,
    ) -> dict:
        """Create or update a service rule (story 11.08)."""
        response = await self._bearer_post(
            f"/calendar/projects/{project_id}/services",
            internal_token=internal_token,
            json={
                "actor": actor,
                "actor_role": actor_role,
                "rule_id": rule_id,
                "name": name,
                "duration_minutes": duration_minutes,
                "working_hours": working_hours,
                "service_days": service_days,
                "date_exceptions": date_exceptions,
            },
        )
        _raise_for_status(response)
        return response.json()

    async def calendar_delete_service(
        self,
        *,
        project_id: int,
        rule_id: int,
        actor: str,
        actor_role: str,
        internal_token: str,
    ) -> dict:
        """Delete a service rule (story 11.08)."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(
                "DELETE",
                f"{self._base_url}/calendar/projects/{project_id}/services/{rule_id}",
                json={"actor": actor, "actor_role": actor_role},
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        _raise_for_status(response)
        return response.json()

    async def upsert_project_service(
        self,
        *,
        project_id: int,
        payload: dict,
        actor: str,
        actor_role: str,
        internal_token: str,
    ) -> dict:
        """Create or update a service row on the canonical Epic-13 surface.

        Calls ``POST /api/projects/{project_id}/services`` (story 13.02). The
        request body is the ``payload`` dict (must contain ``name`` and may
        carry any of ``description``/``price_text``/``tags``/``duration_minutes``/
        ``working_hours``/``service_days``/``date_exceptions``) extended with
        ``actor`` + ``actor_role``. Raises ``ApiError`` with ``detail`` set for
        non-2xx responses so callers can surface the reason in Russian DMs.
        """
        body: dict[str, object] = {
            "actor": actor,
            "actor_role": actor_role,
        }
        body.update(payload)
        response = await self._bearer_post(
            f"/api/projects/{project_id}/services",
            internal_token=internal_token,
            json=body,
        )
        _raise_for_status(response)
        return response.json()

    async def list_project_services(
        self,
        *,
        project_id: int,
        internal_token: str,
    ) -> dict:
        """List all service rows for a project on the canonical Epic-13 surface.

        Calls ``GET /api/projects/{project_id}/services`` (story 13.02).
        Returns the raw JSON body (``{"project_id": int, "services": [...]}``).
        """
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/api/projects/{project_id}/services",
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        _raise_for_status(response)
        return response.json()

    async def delete_project_service(
        self,
        *,
        project_id: int,
        service_id: int,
        actor: str,
        actor_role: str,
        internal_token: str,
    ) -> dict:
        """Delete a service row on the canonical Epic-13 surface.

        Calls ``DELETE /api/projects/{project_id}/services/{service_id}``
        (story 13.02). Admin actors are rejected with 403
        ``admin_cannot_remove_service`` — surfaced as ``ApiError.detail``.
        """
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(
                "DELETE",
                f"{self._base_url}/api/projects/{project_id}/services/{service_id}",
                json={"actor": actor, "actor_role": actor_role},
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        _raise_for_status(response)
        return response.json()

    async def _bearer_post(
        self,
        path: str,
        *,
        internal_token: str,
        json: dict,
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(
                f"{self._base_url}{path}",
                json=json,
                headers={"Authorization": f"Bearer {internal_token}"},
            )

    async def list_projects(self) -> dict:
        return await self._get("/projects", auth=True)

    async def create_project(
        self, *, slug: str, name: str, description: str | None = None
    ) -> dict:
        return await self._post(
            "/projects",
            {"slug": slug, "name": name, "description": description},
            auth=True,
        )

    async def list_operators(self) -> dict:
        return await self._get("/operators", auth=True)

    async def attach_operator(
        self,
        *,
        username: str,
        project_id: int,
        chat_id: int | None = None,
        display_name: str | None = None,
    ) -> dict:
        payload: dict[str, object] = {
            "username": username,
            "project_id": project_id,
        }
        if chat_id is not None:
            payload["chat_id"] = chat_id
        if display_name is not None:
            payload["display_name"] = display_name
        return await self._post("/operators", payload, auth=True)

    async def detach_operator(self, *, username: str) -> dict:
        return await self._patch(
            f"/operators/{username}", {"is_active": False}, auth=True
        )

    async def find_candidate_by_short_id(self, *, short_id: str) -> dict:
        return await self._get(
            f"/knowledge/candidates/by-operator-file/{short_id}", auth=True
        )

    async def reassign_candidate(
        self, *, candidate_id: int, project_id: int
    ) -> dict:
        return await self._post(
            f"/knowledge/candidates/{candidate_id}/reassign",
            {"project_id": project_id},
            auth=True,
        )

    async def services_nl_propose(
        self,
        *,
        project_id: int,
        originating_operator: str,
        text: str,
        internal_token: str,
    ) -> dict:
        """Propose a services NL op (story 13.05 / 13.04 endpoint).

        Calls ``POST /api/projects/{project_id}/services/nl-ops`` with the
        internal service token. Returns the api body which includes
        ``session_id``, ``status``, ``preview``, ``op_type``, ``expires_at``,
        and on success also ``confirm_token``; on a single-pending replacement
        the api also echoes ``prior_cancelled_session_id``.
        """
        response = await self._bearer_post(
            f"/api/projects/{project_id}/services/nl-ops",
            internal_token=internal_token,
            json={
                "originating_operator": originating_operator,
                "text": text,
            },
        )
        _raise_for_status(response)
        return response.json()

    async def services_nl_confirm(
        self,
        *,
        project_id: int,
        session_id: int,
        presenter_operator: str,
        confirm_token: str,
        actor_role: str = "operator",
        internal_token: str,
    ) -> dict:
        """Confirm a pending services NL session (story 13.05).

        Calls ``POST /api/projects/{project_id}/services/nl-ops/{session_id}/confirm``.
        Raises ``ApiError`` with structured ``.detail`` set (e.g.
        ``invalid_confirm_token``, ``not_session_owner``, ``session_expired``,
        ``session_not_pending``, ``admin_cannot_remove_service``).
        """
        response = await self._bearer_post(
            f"/api/projects/{project_id}/services/nl-ops/{session_id}/confirm",
            internal_token=internal_token,
            json={
                "presenter_operator": presenter_operator,
                "confirm_token": confirm_token,
                "actor_role": actor_role,
            },
        )
        _raise_for_status(response)
        return response.json()

    async def services_nl_cancel(
        self,
        *,
        project_id: int,
        session_id: int,
        presenter_operator: str,
        internal_token: str,
    ) -> dict:
        """Cancel a pending services NL session (story 13.05)."""
        response = await self._bearer_post(
            f"/api/projects/{project_id}/services/nl-ops/{session_id}/cancel",
            internal_token=internal_token,
            json={"presenter_operator": presenter_operator},
        )
        _raise_for_status(response)
        return response.json()

    async def services_nl_latest_pending(
        self,
        *,
        project_id: int,
        operator: str,
        internal_token: str,
    ) -> dict | None:
        """Latest pending services NL session for the (project, operator) pair.

        Returns ``None`` on 404 (no pending). Never includes ``confirm_token``
        — the api does not echo tokens on this endpoint; the bot dispatcher
        caches the token returned by the prior propose call.
        """
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                (
                    f"{self._base_url}/api/projects/{project_id}/services/nl-ops/"
                    "latest-pending"
                ),
                params={"operator": operator},
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        if response.status_code == 404:
            return None
        _raise_for_status(response)
        return response.json()

    async def admin_nl_ops_propose(
        self, *, admin_username: str, utterance: str
    ) -> dict:
        return await self._post(
            "/admin/nl-ops",
            {"admin_username": admin_username, "utterance": utterance},
            auth=True,
        )

    async def admin_nl_ops_confirm(
        self, *, session_id: int, confirm_token: str
    ) -> dict:
        return await self._post(
            f"/admin/nl-ops/{session_id}/confirm",
            {"confirm_token": confirm_token},
            auth=True,
        )

    async def admin_nl_ops_cancel(self, *, session_id: int) -> dict:
        return await self._post(
            f"/admin/nl-ops/{session_id}/cancel",
            {},
            auth=True,
        )

    async def admin_nl_ops_latest_pending(
        self, *, admin_username: str
    ) -> dict:
        # Use _get with a query string.
        return await self._get(
            f"/admin/nl-ops/latest-pending?admin_username={admin_username}",
            auth=True,
        )

    async def find_operator_by_username(
        self, *, username: str
    ) -> dict | None:
        """Look up an operator via the unauthenticated internal endpoint.

        Returns None on 404 (not registered, or registered-but-inactive
        is handled by the caller). Re-raises on 5xx so the caller can
        decide whether to fall back to the primary operator.
        """
        try:
            return await self._get(f"/operators/by-username/{username}")
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise

    async def set_persona(
        self,
        *,
        first_name: str,
        last_name: str,
        updated_by: str,
        description: str | None = None,
        short_description: str | None = None,
    ) -> dict:
        payload: dict[str, str] = {
            "first_name": first_name,
            "last_name": last_name,
            "updated_by": updated_by,
        }
        if description is not None:
            payload["description"] = description
        if short_description is not None:
            payload["short_description"] = short_description
        return await self._post("/hitl/runtime-config/persona", payload)

    async def fetch_file_inspect(
        self,
        *,
        short_id: str,
        requester_username: str,
        internal_token: str,
    ) -> dict | None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/admin/files/{short_id}",
                params={"as_user": requester_username},
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        if response.status_code == 404:
            return None
        _raise_for_status(response)
        return response.json()

    async def search_files(
        self,
        *,
        query: str,
        requester_username: str,
        internal_token: str,
        limit: int = 10,
    ) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/admin/files/search",
                params={
                    "q": query,
                    "as_user": requester_username,
                    "limit": limit,
                },
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        _raise_for_status(response)
        return response.json()

    async def delete_operator_file(
        self,
        *,
        short_id: str,
        requester_username: str,
        internal_token: str,
    ) -> dict | None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.delete(
                f"{self._base_url}/admin/files/{short_id}",
                params={"as_user": requester_username},
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        if response.status_code == 404:
            return None
        _raise_for_status(response)
        return response.json()

    async def delete_all_operator_files(
        self,
        *,
        requester_username: str,
        internal_token: str,
    ) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.delete(
                f"{self._base_url}/admin/files",
                params={
                    "as_user": requester_username,
                    "confirm": "true",
                },
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        _raise_for_status(response)
        return response.json()

    async def _bearer_request(
        self,
        method: str,
        path: str,
        *,
        requester_username: str,
        internal_token: str,
        json: dict | None = None,
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(
                method,
                f"{self._base_url}{path}",
                json=json,
                params={"as_user": requester_username},
                headers={"Authorization": f"Bearer {internal_token}"},
            )
        return response

    async def list_project_prompts(
        self,
        *,
        project_slug: str,
        requester_username: str,
        internal_token: str,
    ) -> dict:
        response = await self._bearer_request(
            "GET",
            f"/projects/{project_slug}/prompts",
            requester_username=requester_username,
            internal_token=internal_token,
        )
        _raise_for_status(response)
        return response.json()

    async def get_project_prompt(
        self,
        *,
        project_slug: str,
        prompt_name: str,
        requester_username: str,
        internal_token: str,
    ) -> dict:
        response = await self._bearer_request(
            "GET",
            f"/projects/{project_slug}/prompts/{prompt_name}",
            requester_username=requester_username,
            internal_token=internal_token,
        )
        _raise_for_status(response)
        return response.json()

    async def restore_project_prompt(
        self,
        *,
        project_slug: str,
        prompt_name: str,
        version: int,
        requester_username: str,
        internal_token: str,
    ) -> dict:
        response = await self._bearer_request(
            "POST",
            f"/projects/{project_slug}/prompts/{prompt_name}/restore",
            requester_username=requester_username,
            internal_token=internal_token,
            json={"version": version},
        )
        _raise_for_status(response)
        return response.json()

    async def arm_prompt_pending_edit(
        self,
        *,
        project_slug: str,
        prompt_name: str,
        requester_username: str,
        internal_token: str,
    ) -> dict:
        response = await self._bearer_request(
            "POST",
            f"/projects/{project_slug}/prompts/{prompt_name}/pending",
            requester_username=requester_username,
            internal_token=internal_token,
        )
        _raise_for_status(response)
        return response.json()

    async def peek_pending_prompt_edit(
        self, *, requester_username: str, internal_token: str
    ) -> dict | None:
        response = await self._bearer_request(
            "GET",
            "/pending-prompt-edits",
            requester_username=requester_username,
            internal_token=internal_token,
        )
        if response.status_code == 404:
            return None
        _raise_for_status(response)
        return response.json()

    async def cancel_pending_prompt_edit(
        self, *, requester_username: str, internal_token: str
    ) -> dict:
        response = await self._bearer_request(
            "DELETE",
            "/pending-prompt-edits",
            requester_username=requester_username,
            internal_token=internal_token,
        )
        _raise_for_status(response)
        return response.json()

    async def consume_pending_prompt_edit(
        self,
        *,
        value: str,
        requester_username: str,
        internal_token: str,
    ) -> dict:
        response = await self._bearer_request(
            "POST",
            "/pending-prompt-edits/consume",
            requester_username=requester_username,
            internal_token=internal_token,
            json={"value": value},
        )
        _raise_for_status(response)
        return response.json()

    async def _post(
        self,
        path: str,
        json: dict,
        *,
        timeout_override: int | None = None,
        auth: bool = False,
    ) -> dict:
        timeout = timeout_override if timeout_override is not None else self._timeout
        headers = self._internal_headers() if auth else None
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self._base_url}{path}", json=json, headers=headers
            )
            _raise_for_status(response)
            return response.json()

    async def _get(self, path: str, *, auth: bool = False) -> dict:
        headers = self._internal_headers() if auth else None
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}{path}", headers=headers
            )
            _raise_for_status(response)
            return response.json()

    async def _patch(
        self, path: str, json: dict, *, auth: bool = False
    ) -> dict:
        headers = self._internal_headers() if auth else None
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.patch(
                f"{self._base_url}{path}", json=json, headers=headers
            )
            _raise_for_status(response)
            return response.json()
