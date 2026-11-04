"""Read-only launcher-side readiness checks. No task, workflow or function registration.

Run with the launcher's Python: ``python -m laueportal.remote.health --config <bdp.yaml>``.
Only a small, fixed vocabulary leaves this process; SDK errors can contain authorization
parameters, private paths and URLs and must never be printed or returned.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from laueportal.remote.settings import RemoteSettings, StorageSettings, collection_path, load_settings

CheckName = Literal["dm", "flows", "compute", "aps", "eagle", "crux"]
CheckState = Literal["ok", "login_required", "denied", "unavailable", "unknown", "disabled"]
CHECKS: tuple[CheckName, ...] = ("dm", "flows", "compute", "aps", "eagle", "crux")


class RemoteCheck(BaseModel):
    """One observed component; the detail is fixed text, never an upstream exception."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: CheckName
    state: CheckState
    detail: str = Field(max_length=160)


def failure(name: CheckName, error: Exception) -> RemoteCheck:
    """Distinguish a sign-in challenge from permission denial, an outage, and an unknown result."""
    from laueportal.remote.stage import LoginRefused

    authorization = getattr(error, "authorization_parameters", None)
    raw = getattr(error, "raw_json", None)
    if isinstance(raw, dict):
        authorization = authorization or raw.get("authorization_parameters")
    status = getattr(error, "http_status", None)
    code = str(getattr(error, "code", "")).lower()
    if (
        isinstance(error, LoginRefused)
        or authorization
        or status == 401
        or code in {"invalid_grant", "invalid_token", "consentrequired"}
    ):
        state, detail = "login_required", "Sign in to Globus"
    elif status == 403:
        state, detail = "denied", "Access denied"
    elif isinstance(status, int) and status >= 500:
        state, detail = "unavailable", "Service unavailable"
    else:
        state, detail = "unknown", "Check unavailable"
    return RemoteCheck(name=name, state=state, detail=detail)


def checked(name: CheckName, call: Callable[[], Any], detail: str = "Available") -> RemoteCheck:
    try:
        call()
    except Exception as error:
        return failure(name, error)
    return RemoteCheck(name=name, state="ok", detail=detail)


def globus_checks(settings: RemoteSettings) -> list[RemoteCheck]:
    """Only authenticated GETs. Disable SDK login prompts and Gladier auto-registration."""
    import globus_sdk
    from globus_sdk.scopes import ComputeScopes, FlowsScopes, TransferScopes
    from globus_sdk.transport import RequestsTransport, RetryConfig

    from laueportal.remote.flow import client_class
    from laueportal.remote.stage import LoginRefused, _refuse_logins, configure_auth

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise LoginRefused("Monitoring cannot prompt for login")

    configure_auth(settings)
    client = client_class(settings)(auto_registration=False)
    manager = client.login_manager
    _refuse_logins(manager, refuse)
    manager.add_requirements([cast(Any, TransferScopes.all)])
    app = manager.globus_app if hasattr(manager, "globus_app") else None
    # The confidential-identity manager has no GlobusApp. Its authorizers renew noninteractively.
    authorizers = manager.get_manager_authorizers() if app is None else {}

    def api(factory: Any, scope: Any) -> Any:
        # FlowsClient defaults to its broad `all` scope, which existing submission
        # logins need not hold. Ask only for the scope this read actually uses.
        auth = {"app": app, "app_scopes": [scope]} if app is not None else {"authorizer": authorizers[str(scope)]}
        return factory(**auth, transport=RequestsTransport(http_timeout=4), retry_config=RetryConfig(max_retries=0))

    flows = api(globus_sdk.FlowsClient, FlowsScopes.view_flows)
    compute = api(globus_sdk.ComputeClientV2, ComputeScopes.all)
    transfer = api(globus_sdk.TransferClient, TransferScopes.all)
    flow_id = client.get_flow_id()
    result = [
        checked("flows", lambda: flows.get_flow(flow_id))
        if flow_id
        else RemoteCheck(name="flows", state="unknown", detail="Flow is not registered")
    ]
    target = settings.targets.crux
    if target is None:
        raise ValueError("Crux is not configured")
    endpoint = target.compute_endpoint
    if endpoint:
        result.append(checked("compute", lambda: compute.get_endpoint(endpoint)))
        try:
            status = compute.get_endpoint_status(endpoint)["status"]
            result.append(
                RemoteCheck(
                    name="crux",
                    state="ok" if status == "online" else "unavailable" if status == "offline" else "unknown",
                    detail="Online" if status == "online" else "Offline" if status == "offline" else "Status unknown",
                )
            )
        except Exception as error:
            result.append(failure("crux", error))
    else:
        result.extend(
            RemoteCheck(name=n, state="unknown", detail="Endpoint is not configured") for n in ("compute", "crux")
        )
    locations: tuple[tuple[CheckName, StorageSettings | None, str | None], ...] = (
        ("aps", settings.storage.aps, settings.experiment_directory),
        ("eagle", settings.storage.eagle, target.workspace),
    )
    for name, storage, path in locations:
        if storage is None or not storage.collection_id or not path:
            result.append(RemoteCheck(name=name, state="unknown", detail="Storage is not configured"))
        else:
            result.append(
                checked(
                    name,
                    lambda s=storage, p=path: transfer.operation_stat(s.collection_id, path=collection_path(s, p)),
                    "Accessible",
                )
            )
    return result


def collect(settings: RemoteSettings) -> list[RemoteCheck]:
    """Check DM independently, so a Globus login failure cannot hide a working DM service."""
    from laueportal.remote.dm import DMClient

    dm = settings.dm.model_copy(update={"timeout_seconds": 8})
    result = [checked("dm", lambda: DMClient(dm).workflow())]
    try:
        result.extend(globus_checks(settings))
    except Exception as error:
        result.extend(failure(name, error) for name in CHECKS if name != "dm")
    by_name = {check.name: check for check in result}
    return [by_name[name] for name in CHECKS]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    arguments = parser.parse_args()
    try:
        checks = collect(load_settings(arguments.config))
    except Exception:
        checks = [RemoteCheck(name=name, state="unknown", detail="Check unavailable") for name in CHECKS]
    print(json.dumps([check.model_dump() for check in checks]))


if __name__ == "__main__":
    main()
