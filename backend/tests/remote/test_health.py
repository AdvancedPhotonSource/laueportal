"""Read-only health exercises the real check orchestration with stand-in service clients."""

from types import SimpleNamespace

import pytest

from laueportal.remote import health
from laueportal.remote.settings import parse_settings


@pytest.fixture
def services(monkeypatch):
    import globus_sdk

    from laueportal.remote import flow, stage

    calls = []
    manager = SimpleNamespace(globus_app=object(), add_requirements=lambda scopes: None)
    client = SimpleNamespace(login_manager=manager, get_flow_id=lambda: "registered-flow")

    def factory(**kwargs):
        assert kwargs == {"auto_registration": False}
        return client

    monkeypatch.setattr(flow, "client_class", lambda settings: factory)
    monkeypatch.setattr(stage, "configure_auth", lambda settings: None)
    monkeypatch.setattr(stage, "_refuse_logins", lambda manager, refuse: setattr(manager, "login", refuse))

    class API:
        def __init__(self, **kwargs):
            assert kwargs["app"] is manager.globus_app
            # In particular, FlowsClient must not add its default `all` scope,
            # which would falsely demand a new login for a working submission identity.
            assert [str(scope) for scope in kwargs["app_scopes"]] in [
                [str(globus_sdk.scopes.FlowsScopes.view_flows)],
                [str(globus_sdk.scopes.ComputeScopes.all)],
                [str(globus_sdk.scopes.TransferScopes.all)],
            ]

        def get_flow(self, identifier):
            calls.append(("flow", identifier))

        def get_endpoint(self, identifier):
            calls.append(("endpoint", identifier))

        def get_endpoint_status(self, identifier):
            calls.append(("status", identifier))
            return {"status": "online"}

        def operation_stat(self, identifier, *, path):
            calls.append(("stat", identifier, path))

    for name in ("FlowsClient", "ComputeClientV2", "TransferClient"):
        monkeypatch.setattr(globus_sdk, name, API)
    return calls, API, manager


def configured():
    return parse_settings(
        {
            "experiment_directory": "/aps/experiment",
            "targets": {"crux": {"enabled": True, "compute_endpoint": "endpoint", "workspace": "/eagle/workspace"}},
            "storage": {
                "aps": {"collection_id": "aps", "filesystem_root": "/aps", "collection_root": "/"},
                "eagle": {"collection_id": "eagle", "filesystem_root": "/eagle", "collection_root": "/"},
            },
        }
    )


def test_checks_only_read_existing_registration_status_and_storage(services):
    calls, _, manager = services
    checks = health.globus_checks(configured())
    assert {c.state for c in checks} == {"ok"}
    assert calls == [
        ("flow", "registered-flow"),
        ("endpoint", "endpoint"),
        ("status", "endpoint"),
        ("stat", "aps", "/experiment"),
        ("stat", "eagle", "/workspace"),
    ]
    with pytest.raises(Exception, match="cannot prompt"):
        manager.login()


def test_a_collection_login_challenge_does_not_hide_a_working_endpoint(services, monkeypatch):
    _, api, _ = services

    class Challenge(Exception):
        http_status = 403
        raw_json = {"authorization_parameters": {"session_required_single_domain": ["example.invalid"]}}

    def stat(self, identifier, *, path):
        if identifier == "eagle":
            raise Challenge("secret token and private URL must not escape")

    monkeypatch.setattr(api, "operation_stat", stat)
    result = {c.name: c for c in health.globus_checks(configured())}
    assert result["crux"].state == "ok"
    assert result["aps"].state == "ok"
    assert result["eagle"].state == "login_required"
    assert result["eagle"].detail == "Sign in to Globus"


@pytest.mark.parametrize(("http_status", "state"), [(403, "denied"), (503, "unavailable"), (None, "unknown")])
def test_failures_are_classified_without_repeating_upstream_error_text(http_status, state):
    error = RuntimeError("private credential")
    error.http_status = http_status
    check = health.failure("flows", error)
    assert check.state == state
    assert "private credential" not in check.model_dump_json()


def test_core_endpoint_offline_is_observed_without_starting_a_user_endpoint(services, monkeypatch):
    _, api, _ = services
    monkeypatch.setattr(api, "get_endpoint_status", lambda *_args: {"status": "offline"})
    result = {c.name: c for c in health.globus_checks(configured())}
    assert result["compute"].state == "ok"
    assert result["crux"].state == "unavailable"


def test_a_revoked_refresh_token_is_login_needed_even_when_auth_returns_http_400():
    class Revoked(Exception):
        http_status = 400
        code = "invalid_grant"

    assert health.failure("compute", Revoked("private OAuth error")).state == "login_required"
