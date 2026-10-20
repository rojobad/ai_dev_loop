"""Pinned Cursor SDK 1.0.37 adapter using only its public Python surface."""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, cast

from ai_dev_loop.config import CURSOR_SDK_VERSION, CursorSdkSection
from ai_dev_loop.integrations.cursor_sdk.port import (
    CursorSdkAgentIdentity,
    CursorSdkBridgeHealth,
    CursorSdkClientConfig,
    CursorSdkEnvelope,
    CursorSdkErrorCode,
    CursorSdkFailure,
    CursorSdkOperation,
    CursorSdkOutcome,
    CursorSdkPreflight,
    CursorSdkRunIdentity,
    CursorSdkRunPage,
    CursorSdkTerminalRun,
    CursorSdkUsage,
)
from ai_dev_loop.paths import (
    CursorSdkPathError,
    ensure_cursor_sdk_client_paths,
    read_cursor_credential,
)

_SAFE_METADATA = re.compile(r"^[A-Za-z0-9._:/+-]{1,128}$")
_CURSOR_AUTH_ENV_NAMES = frozenset(
    {
        "CURSOR_API_KEY",
        "CURSOR_SDK_BRIDGE_TOKEN",
        "CURSOR_SDK_BRIDGE_AUTH_TOKEN",
    }
)
_SAFE_NUMERIC_USAGE_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
        "reasoning_tokens",
    }
)


def build_cursor_sdk_client_config(
    selection: CursorSdkSection,
    *,
    workspace: Path,
    repository_root: Path,
    conversation_owner_identity: str,
) -> CursorSdkClientConfig:
    paths = ensure_cursor_sdk_client_paths(
        workspace=workspace,
        repository_root=repository_root,
        conversation_owner_identity=conversation_owner_identity,
    )
    return CursorSdkClientConfig(
        sdk_version=selection.sdk_version,
        workspace=paths.workspace,
        state_root=paths.state_root,
        store_root=paths.store_root,
        model=selection.model,
        parameters=selection.wire_parameters(),
        credential_ref=selection.credential_ref,
        setting_sources=tuple(selection.setting_sources),
        sandbox=selection.sandbox,
    )


@contextmanager
def _without_cursor_auth_environment(api_key: str) -> Iterator[None]:
    removed: dict[str, str] = {}
    for name, value in tuple(os.environ.items()):
        cursor_secret_name = name.startswith("CURSOR_") and ("KEY" in name or "TOKEN" in name)
        if name in _CURSOR_AUTH_ENV_NAMES or cursor_secret_name or value == api_key:
            removed[name] = value
            os.environ.pop(name, None)
    try:
        yield
    finally:
        os.environ.update(removed)


class CursorSdkAdapter:
    """One explicit bridge/client lifecycle owned by a future attempt worker."""

    def __init__(
        self,
        config: CursorSdkClientConfig,
        *,
        repository_root: Path,
        sdk_loader: Callable[[], ModuleType | Any] | None = None,
    ) -> None:
        self._config = config
        self._repository_root = repository_root
        self._sdk_loader = sdk_loader
        self._sdk: Any = None
        self._client: Any = None
        self._api_key: str | None = None
        self._agents: dict[str, Any] = {}
        self._runs: dict[str, Any] = {}
        self._health: CursorSdkBridgeHealth | None = None

    def __enter__(self) -> CursorSdkAdapter:
        outcome = self.open()
        if outcome.failure is not None:
            raise RuntimeError(f"Cursor SDK adapter open failed: {outcome.failure.code}")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def open(self) -> CursorSdkOutcome[CursorSdkBridgeHealth]:
        if self._client is not None:
            return CursorSdkOutcome.failed(
                self._failure(CursorSdkErrorCode.INVALID_STATE, CursorSdkOperation.OPEN)
            )
        try:
            sdk = (
                self._sdk_loader()
                if self._sdk_loader is not None
                else importlib.import_module("cursor_sdk")
            )
        except (ImportError, ModuleNotFoundError):
            return CursorSdkOutcome.failed(
                self._failure(CursorSdkErrorCode.SDK_UNAVAILABLE, CursorSdkOperation.OPEN)
            )
        installed_version = self._sdk_version(sdk)
        if installed_version != self._config.sdk_version or installed_version != CURSOR_SDK_VERSION:
            return CursorSdkOutcome.failed(
                self._failure(CursorSdkErrorCode.VERSION_MISMATCH, CursorSdkOperation.OPEN)
            )
        self._sdk = sdk
        try:
            api_key = read_cursor_credential(
                self._config.credential_ref,
                repository_root=self._repository_root,
            )
            # Keep the active value only in memory before bridge launch so an
            # SDK launch error cannot copy it into otherwise-safe metadata.
            self._api_key = api_key
            local_options = self._local_options(sdk)
            with _without_cursor_auth_environment(api_key):
                client = sdk.CursorClient.launch_bridge(
                    workspace=self._config.workspace,
                    state_root=self._config.state_root,
                    local=local_options,
                    timeout=30,
                    client_timeout=60,
                    max_retries=0,
                    allow_api_key_env_fallback=True,
                )
            self._client = client
            ping = client.ping()
            version = client.get_version()
            if ping != "pong" or not isinstance(version, dict):
                self.close()
                return CursorSdkOutcome.failed(
                    self._failure(CursorSdkErrorCode.INVALID_RESPONSE, CursorSdkOperation.OPEN)
                )
            bridge_version = str(version.get("bridgeVersion", ""))
            protocol_version = str(version.get("protocolVersion", ""))
            if not bridge_version or not protocol_version:
                self.close()
                return CursorSdkOutcome.failed(
                    self._failure(CursorSdkErrorCode.INVALID_RESPONSE, CursorSdkOperation.OPEN)
                )
            health = CursorSdkBridgeHealth(
                sdk_version=installed_version,
                bridge_version=bridge_version,
                protocol_version=protocol_version,
            )
            self._health = health
            return CursorSdkOutcome.success(health)
        except CursorSdkPathError:
            self.close()
            return CursorSdkOutcome.failed(
                self._failure(CursorSdkErrorCode.INVALID_LOCAL_PATH, CursorSdkOperation.OPEN)
            )
        except Exception as exc:
            failure = self._classify(exc, CursorSdkOperation.OPEN)
            self.close()
            return CursorSdkOutcome.failed(failure)

    def close(self) -> None:
        client = self._client
        self._client = None
        self._sdk = None
        self._api_key = None
        self._health = None
        self._agents.clear()
        self._runs.clear()
        if client is not None:
            with suppress(Exception):
                client.close()

    def preflight(self) -> CursorSdkOutcome[CursorSdkPreflight]:
        invalid = self._require_open(CursorSdkOperation.PREFLIGHT)
        if invalid is not None:
            return CursorSdkOutcome.failed(invalid)
        try:
            self._client.me(api_key=self._api_key)
            models = tuple(self._client.models.list(api_key=self._api_key))
            selected = next(
                (model for model in models if str(getattr(model, "id", "")) == self._config.model),
                None,
            )
            if selected is None:
                return CursorSdkOutcome.failed(
                    self._failure(CursorSdkErrorCode.INVALID_REQUEST, CursorSdkOperation.PREFLIGHT)
                )
            expected = tuple(sorted(self._config.parameters))
            variants = tuple(getattr(selected, "variants", ()) or ())
            exact_variant = any(
                tuple(
                    sorted(
                        (str(getattr(item, "id", "")), str(getattr(item, "value", "")))
                        for item in (getattr(variant, "params", ()) or ())
                    )
                )
                == expected
                for variant in variants
            )
            if not exact_variant:
                return CursorSdkOutcome.failed(
                    self._failure(CursorSdkErrorCode.INVALID_REQUEST, CursorSdkOperation.PREFLIGHT)
                )
            assert self._health is not None
            return CursorSdkOutcome.success(
                CursorSdkPreflight(
                    health=self._health,
                    authenticated=True,
                    model=self._config.model,
                    parameters=expected,
                )
            )
        except Exception as exc:
            return CursorSdkOutcome.failed(self._classify(exc, CursorSdkOperation.PREFLIGHT))

    def create_agent(self) -> CursorSdkOutcome[CursorSdkAgentIdentity]:
        invalid = self._require_open(CursorSdkOperation.CREATE)
        if invalid is not None:
            return CursorSdkOutcome.failed(invalid)
        try:
            agent = self._client.create_agent(self._agent_options())
            agent_id = str(getattr(agent, "agent_id", ""))
            if not agent_id:
                return CursorSdkOutcome.failed(
                    self._failure(CursorSdkErrorCode.INVALID_RESPONSE, CursorSdkOperation.CREATE)
                )
            self._agents[agent_id] = agent
            return CursorSdkOutcome.success(CursorSdkAgentIdentity(agent_id=agent_id))
        except Exception as exc:
            return CursorSdkOutcome.failed(self._classify(exc, CursorSdkOperation.CREATE))

    def resume_agent(self, agent_id: str) -> CursorSdkOutcome[CursorSdkAgentIdentity]:
        invalid = self._require_open(CursorSdkOperation.RESUME, agent_id=agent_id)
        if invalid is not None:
            return CursorSdkOutcome.failed(invalid)
        try:
            agent = self._client.resume_agent(agent_id, self._agent_options())
            resumed_id = str(getattr(agent, "agent_id", ""))
            if resumed_id != agent_id:
                return CursorSdkOutcome.failed(
                    self._failure(
                        CursorSdkErrorCode.INVALID_RESPONSE,
                        CursorSdkOperation.RESUME,
                        agent_id=agent_id,
                    )
                )
            self._agents[agent_id] = agent
            return CursorSdkOutcome.success(CursorSdkAgentIdentity(agent_id=agent_id))
        except Exception as exc:
            return CursorSdkOutcome.failed(
                self._classify(exc, CursorSdkOperation.RESUME, agent_id=agent_id)
            )

    def send(self, agent_id: str, prompt: str) -> CursorSdkOutcome[CursorSdkRunIdentity]:
        invalid = self._require_open(CursorSdkOperation.SEND, agent_id=agent_id)
        if invalid is not None:
            return CursorSdkOutcome.failed(invalid)
        if not prompt.strip():
            return CursorSdkOutcome.failed(
                self._failure(
                    CursorSdkErrorCode.INVALID_REQUEST,
                    CursorSdkOperation.SEND,
                    agent_id=agent_id,
                )
            )
        agent = self._agents.get(agent_id)
        if agent is None:
            resumed = self.resume_agent(agent_id)
            if resumed.failure is not None:
                return CursorSdkOutcome.failed(resumed.failure)
            agent = self._agents[agent_id]
        try:
            run = agent.send(prompt, self._send_options())
            run_id = self._run_id(run)
            returned_agent_id = str(getattr(run, "agent_id", agent_id))
            if not run_id or returned_agent_id != agent_id:
                return CursorSdkOutcome.failed(
                    self._failure(
                        CursorSdkErrorCode.INVALID_RESPONSE,
                        CursorSdkOperation.SEND,
                        agent_id=agent_id,
                    )
                )
            self._runs[run_id] = run
            return CursorSdkOutcome.success(CursorSdkRunIdentity(agent_id=agent_id, run_id=run_id))
        except Exception as exc:
            return CursorSdkOutcome.failed(
                self._classify(exc, CursorSdkOperation.SEND, agent_id=agent_id)
            )

    def stream(
        self,
        agent_id: str,
        run_id: str,
        *,
        mode: Literal["live", "replay"] = "live",
        after_offset: str | None = None,
        on_envelope: Callable[[CursorSdkEnvelope], None] | None = None,
    ) -> CursorSdkOutcome[CursorSdkTerminalRun]:
        invalid = self._require_open(
            CursorSdkOperation.STREAM,
            agent_id=agent_id,
            run_id=run_id,
        )
        if invalid is not None:
            return CursorSdkOutcome.failed(invalid)
        try:
            if mode == "replay":
                run = self._get_public_run(agent_id, run_id)
                # Pinned 1.0.37 reports supports("observe") == false even when
                # public replay works. Invoke observe directly and never gate it.
                # ObserveRun does not mutate a handle created by GetRun, so use
                # the public wait operation for the authoritative terminal result.
                for event in run.observe(after_offset=after_offset):
                    if on_envelope is not None:
                        on_envelope(self._envelope(event))
                converted = self._terminal_result(run, run.wait())
            else:
                run = self._runs.get(run_id)
                if run is None:
                    run = self._get_public_run(agent_id, run_id)
                iterator = run.events() if hasattr(run, "events") else run.stream()
                for event in iterator:
                    if on_envelope is not None:
                        on_envelope(self._envelope(event))
                converted = self._terminal_result(run, run.wait())
            if converted.agent_id != agent_id or converted.run_id != run_id:
                return CursorSdkOutcome.failed(
                    self._failure(
                        CursorSdkErrorCode.INVALID_RESPONSE,
                        CursorSdkOperation.STREAM,
                        agent_id=agent_id,
                        run_id=run_id,
                    )
                )
            return CursorSdkOutcome.success(converted)
        except Exception as exc:
            return CursorSdkOutcome.failed(
                self._classify(
                    exc,
                    CursorSdkOperation.STREAM,
                    agent_id=agent_id,
                    run_id=run_id,
                )
            )

    def get_run(
        self,
        agent_id: str,
        run_id: str,
    ) -> CursorSdkOutcome[CursorSdkTerminalRun]:
        invalid = self._require_open(
            CursorSdkOperation.GET_RUN,
            agent_id=agent_id,
            run_id=run_id,
        )
        if invalid is not None:
            return CursorSdkOutcome.failed(invalid)
        try:
            converted = self._terminal_run(self._get_public_run(agent_id, run_id))
            if converted.agent_id != agent_id or converted.run_id != run_id:
                return CursorSdkOutcome.failed(
                    self._failure(
                        CursorSdkErrorCode.INVALID_RESPONSE,
                        CursorSdkOperation.GET_RUN,
                        agent_id=agent_id,
                        run_id=run_id,
                    )
                )
            return CursorSdkOutcome.success(converted)
        except Exception as exc:
            return CursorSdkOutcome.failed(
                self._classify(
                    exc,
                    CursorSdkOperation.GET_RUN,
                    agent_id=agent_id,
                    run_id=run_id,
                )
            )

    def list_runs(
        self,
        agent_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> CursorSdkOutcome[CursorSdkRunPage]:
        invalid = self._require_open(CursorSdkOperation.LIST_RUNS, agent_id=agent_id)
        if invalid is not None:
            return CursorSdkOutcome.failed(invalid)
        try:
            page = self._client.list_runs(
                agent_id,
                runtime="local",
                cwd=self._config.workspace,
                cursor=cursor,
                limit=limit,
                api_key=self._api_key,
            )
            runs = tuple(self._terminal_run(run) for run in page)
            if any(run.agent_id != agent_id for run in runs):
                return CursorSdkOutcome.failed(
                    self._failure(
                        CursorSdkErrorCode.INVALID_RESPONSE,
                        CursorSdkOperation.LIST_RUNS,
                        agent_id=agent_id,
                    )
                )
            raw_next_cursor = getattr(page, "next_cursor", None)
            next_cursor = (
                str(raw_next_cursor).strip() if raw_next_cursor is not None else None
            ) or None
            return CursorSdkOutcome.success(CursorSdkRunPage(runs=runs, next_cursor=next_cursor))
        except Exception as exc:
            return CursorSdkOutcome.failed(
                self._classify(exc, CursorSdkOperation.LIST_RUNS, agent_id=agent_id)
            )

    def list_agent_messages(self, agent_id: str) -> CursorSdkOutcome[int]:
        """Return the public agent history count without reading a run conversation."""

        invalid = self._require_open(CursorSdkOperation.LIST_MESSAGES, agent_id=agent_id)
        if invalid is not None:
            return CursorSdkOutcome.failed(invalid)
        agent = self._agents.get(agent_id)
        if agent is None:
            resumed = self.resume_agent(agent_id)
            if resumed.failure is not None:
                return CursorSdkOutcome.failed(resumed.failure)
            agent = self._agents[agent_id]
        try:
            messages = agent.list_messages()
            if not isinstance(messages, list):
                return CursorSdkOutcome.failed(
                    self._failure(
                        CursorSdkErrorCode.INVALID_RESPONSE,
                        CursorSdkOperation.LIST_MESSAGES,
                        agent_id=agent_id,
                    )
                )
            return CursorSdkOutcome.success(len(messages))
        except Exception as exc:
            return CursorSdkOutcome.failed(
                self._classify(exc, CursorSdkOperation.LIST_MESSAGES, agent_id=agent_id)
            )

    def cancel(
        self,
        agent_id: str,
        run_id: str,
    ) -> CursorSdkOutcome[CursorSdkRunIdentity]:
        invalid = self._require_open(
            CursorSdkOperation.CANCEL,
            agent_id=agent_id,
            run_id=run_id,
        )
        if invalid is not None:
            return CursorSdkOutcome.failed(invalid)
        try:
            # Register the exact persisted run through get_run before cancel;
            # detached cancel did not work in the pinned-version POC.
            run = self._get_public_run(agent_id, run_id)
            returned_agent_id = str(getattr(run, "agent_id", ""))
            returned_run_id = self._run_id(run)
            if returned_agent_id != agent_id or returned_run_id != run_id:
                return CursorSdkOutcome.failed(
                    self._failure(
                        CursorSdkErrorCode.INVALID_RESPONSE,
                        CursorSdkOperation.CANCEL,
                        agent_id=agent_id,
                        run_id=run_id,
                    )
                )
            run.cancel()
            return CursorSdkOutcome.success(CursorSdkRunIdentity(agent_id=agent_id, run_id=run_id))
        except Exception as exc:
            return CursorSdkOutcome.failed(
                self._classify(
                    exc,
                    CursorSdkOperation.CANCEL,
                    agent_id=agent_id,
                    run_id=run_id,
                )
            )

    def _sdk_version(self, sdk: Any) -> str:
        if self._sdk_loader is not None:
            return str(getattr(sdk, "__version__", CURSOR_SDK_VERSION))
        try:
            return importlib.metadata.version("cursor-sdk")
        except importlib.metadata.PackageNotFoundError:
            return ""

    def _local_options(self, sdk: Any) -> Any:
        return sdk.LocalAgentOptions(
            cwd=self._config.workspace,
            setting_sources=list(self._config.setting_sources),
            sandbox_options=sdk.SandboxOptions(enabled=self._config.sandbox == "enabled"),
            store=sdk.LocalAgentStoreConfig(type="jsonl", root_dir=str(self._config.store_root)),
        )

    def _model_selection(self) -> Any:
        assert self._sdk is not None
        return self._sdk.ModelSelection(
            id=self._config.model,
            params=tuple(
                self._sdk.ModelParameterValue(id=key, value=value)
                for key, value in self._config.parameters
            ),
        )

    def _agent_options(self) -> Any:
        assert self._sdk is not None
        return self._sdk.AgentOptions(
            model=self._model_selection(),
            api_key=self._api_key,
            local=self._local_options(self._sdk),
        )

    def _send_options(self) -> Any:
        assert self._sdk is not None
        return self._sdk.SendOptions(model=self._model_selection())

    def _get_public_run(self, agent_id: str, run_id: str) -> Any:
        return self._client.get_run(
            run_id,
            {
                "runtime": "local",
                "agentId": agent_id,
                "apiKey": self._api_key,
            },
        )

    def _terminal_run(self, value: Any) -> CursorSdkTerminalRun:
        return self._terminal_result(value, value)

    def _terminal_result(self, identity: Any, result: Any) -> CursorSdkTerminalRun:
        usage_value = getattr(result, "usage", None)
        usage = None
        if usage_value is not None:
            usage = CursorSdkUsage(
                input_tokens=getattr(usage_value, "input_tokens", None),
                output_tokens=getattr(usage_value, "output_tokens", None),
                cache_read_tokens=getattr(usage_value, "cache_read_tokens", None),
                cache_write_tokens=getattr(usage_value, "cache_write_tokens", None),
                total_tokens=getattr(usage_value, "total_tokens", None),
                reasoning_tokens=getattr(usage_value, "reasoning_tokens", None),
            )
        return CursorSdkTerminalRun(
            agent_id=str(getattr(identity, "agent_id", "")),
            run_id=CursorSdkAdapter._run_id(identity),
            status=str(getattr(result, "status", "")),
            text=self._redact_active_credential(str(getattr(result, "result", ""))),
            usage=usage,
        )

    def _redact_active_credential(self, text: str) -> str:
        if self._api_key:
            return text.replace(self._api_key, "[redacted]")
        return text

    @staticmethod
    def _run_id(value: Any) -> str:
        native_id = getattr(value, "id", None)
        if native_id is not None:
            return str(native_id)
        return str(getattr(value, "run_id", ""))

    def _envelope(self, value: Any) -> CursorSdkEnvelope:
        payload = self._safe_payload(value)
        offset = str(payload.get("offset", getattr(value, "offset", "")))
        if not offset:
            raise ValueError("SDK envelope missing offset")
        return CursorSdkEnvelope(offset=offset, payload=payload)

    def _safe_payload(self, value: Any) -> dict[str, Any]:
        seen: set[int] = set()

        def clean(item: object, *, depth: int = 0) -> object:
            if depth > 12:
                return "[redacted-depth]"
            if isinstance(item, dict):
                result: dict[str, object] = {}
                for key, child in item.items():
                    name = str(key)
                    if name in _SAFE_NUMERIC_USAGE_FIELDS:
                        # Exact provider counter names are the sole exception to
                        # the broad secret-token key filter.  Preserve their raw
                        # scalar type so strict validation can reject malformed
                        # numeric values; no string or nested value passes it.
                        if child is None or isinstance(child, (int, float, bool)):
                            result[name] = child
                        continue
                    if any(token in name.lower() for token in ("api_key", "token", "secret")):
                        continue
                    result[name] = clean(child, depth=depth + 1)
                return result
            if isinstance(item, (list, tuple)):
                return [clean(child, depth=depth + 1) for child in item]
            if isinstance(item, (str, int, float, bool)) or item is None:
                if isinstance(item, str) and self._api_key and self._api_key in item:
                    return "[redacted]"
                return item
            identity = id(item)
            if identity in seen:
                return "[redacted-cycle]"
            seen.add(identity)
            try:
                if is_dataclass(item):
                    expanded: object = asdict(cast(Any, item))
                elif hasattr(item, "model_dump"):
                    expanded = item.model_dump(mode="json")
                elif hasattr(item, "__dict__"):
                    expanded = dict(vars(item))
                else:
                    expanded = str(item)
                return clean(expanded, depth=depth + 1)
            finally:
                seen.discard(identity)

        cleaned = clean(value)
        return cleaned if isinstance(cleaned, dict) else {"value": cleaned}

    def _require_open(
        self,
        operation: CursorSdkOperation,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> CursorSdkFailure | None:
        if self._client is not None and self._sdk is not None and self._api_key is not None:
            return None
        return self._failure(
            CursorSdkErrorCode.INVALID_STATE,
            operation,
            agent_id=agent_id,
            run_id=run_id,
        )

    @staticmethod
    def _failure(
        code: CursorSdkErrorCode,
        operation: CursorSdkOperation,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
        retry_after: str | None = None,
        request_id: str | None = None,
        evidence_class: Literal["typed", "structured", "message"] | None = None,
    ) -> CursorSdkFailure:
        return CursorSdkFailure(
            code=code,
            operation=operation,
            agent_id=agent_id,
            run_id=run_id,
            retry_after=retry_after,
            request_id=request_id,
            evidence_class=evidence_class,
        )

    def _is_error(self, exc: Exception, name: str) -> bool:
        errors = getattr(self._sdk, "errors", None)
        error_type = getattr(errors, name, None)
        return isinstance(error_type, type) and isinstance(exc, error_type)

    def _safe_metadata(self, value: object) -> str | None:
        text = str(value) if value is not None else ""
        if self._api_key and self._api_key in text:
            return None
        return text if _SAFE_METADATA.fullmatch(text) else None

    def _classify(
        self,
        exc: Exception,
        operation: CursorSdkOperation,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> CursorSdkFailure:
        retry_after = self._safe_metadata(getattr(exc, "retry_after", None))
        request_id = self._safe_metadata(getattr(exc, "request_id", None))
        evidence_class: Literal["typed", "structured", "message"] | None = None
        if self._is_error(exc, "AuthenticationError"):
            code = CursorSdkErrorCode.AUTHENTICATION_FAILED
        elif self._is_error(exc, "RateLimitError"):
            code = CursorSdkErrorCode.RATE_LIMITED
        elif self._is_error(exc, "APITimeoutError"):
            code = CursorSdkErrorCode.TIMEOUT
        elif self._is_error(exc, "AgentNotFoundError") or self._is_error(exc, "NotFoundError"):
            code = CursorSdkErrorCode.UNKNOWN_IDENTITY
        elif self._is_error(exc, "AgentBusyError"):
            code = CursorSdkErrorCode.BUSY
            evidence_class = "typed"
        elif self._is_error(exc, "InternalServerError"):
            native_codes = {
                str(getattr(exc, "code", "")).lower(),
                str(getattr(exc, "proto_error_code", "")).lower(),
            }
            message = str(getattr(exc, "message", exc)).lower()
            if any(code.endswith("feature_unavailable") for code in native_codes) or (
                "[feature_unavailable]" in message
            ):
                code = CursorSdkErrorCode.FEATURE_UNAVAILABLE
            elif any(code.endswith("agent_busy") for code in native_codes):
                code = CursorSdkErrorCode.BUSY
                evidence_class = "structured"
            elif operation is CursorSdkOperation.SEND and "already has active run" in message:
                code = CursorSdkErrorCode.BUSY
                evidence_class = "message"
            else:
                code = CursorSdkErrorCode.SERVER_ERROR
        elif self._is_error(exc, "NetworkError"):
            code = CursorSdkErrorCode.NETWORK
        elif self._is_error(exc, "BadRequestError") or self._is_error(exc, "ConfigurationError"):
            code = CursorSdkErrorCode.INVALID_REQUEST
        else:
            code = CursorSdkErrorCode.SDK_ERROR
        return self._failure(
            code,
            operation,
            agent_id=agent_id,
            run_id=run_id,
            retry_after=retry_after,
            request_id=request_id,
            evidence_class=evidence_class,
        )
