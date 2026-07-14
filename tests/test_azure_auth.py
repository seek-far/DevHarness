"""Entra ID token acquisition: caching, refresh-before-expiry, single-flight.

`DefaultAzureCredential` resolves to a Managed Identity on Azure compute and to
the developer's `az login` session locally — same code path, different source.
These tests mock the credential so CI never touches Azure.

Three properties matter:
  * a token is reused until it is close to expiry (minting is a network call);
  * it is refreshed BEFORE it actually expires, so a long fix() can't hand the
    LLM client a token that dies mid-flight;
  * N concurrent callers on a cold cache mint ONE token, not N.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from services import azure_auth  # noqa: E402


class _FakeToken:
    def __init__(self, token: str, expires_on: float):
        self.token = token
        self.expires_on = expires_on


class _FakeCredential:
    """Stand-in for DefaultAzureCredential. Counts how often a token is minted."""

    calls = 0
    ttl = 3600.0
    fail_with: Exception | None = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs

    def get_token(self, scope):
        if type(self).fail_with:
            raise type(self).fail_with
        type(self).calls += 1
        return _FakeToken(f"token-{type(self).calls}", time.time() + type(self).ttl)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    azure_auth.reset_cache()
    _FakeCredential.calls = 0
    _FakeCredential.ttl = 3600.0
    _FakeCredential.fail_with = None
    _FakeCredential.last_kwargs = {}
    fake_mod = types.SimpleNamespace(DefaultAzureCredential=_FakeCredential)
    monkeypatch.setitem(sys.modules, "azure.identity", fake_mod)
    yield
    azure_auth.reset_cache()


def test_token_is_cached_across_calls():
    a = azure_auth.get_entra_token("scope-a")
    b = azure_auth.get_entra_token("scope-a")
    assert a == b == "token-1"
    assert _FakeCredential.calls == 1, "second call must hit the cache, not the network"


def test_refreshes_before_actual_expiry():
    """A token valid for another 60s is INSIDE the 300s skew window, so it must
    be refreshed now rather than handed out and expiring mid-request."""
    _FakeCredential.ttl = 60.0
    first = azure_auth.get_entra_token("scope-a")
    second = azure_auth.get_entra_token("scope-a")
    assert first == "token-1"
    assert second == "token-2", "token inside the refresh skew must be re-minted"
    assert _FakeCredential.calls == 2


def test_distinct_scopes_and_client_ids_do_not_share_cache():
    azure_auth.get_entra_token("scope-a")
    azure_auth.get_entra_token("scope-b")
    azure_auth.get_entra_token("scope-a", client_id="uami-1")
    assert _FakeCredential.calls == 3


def test_user_assigned_identity_client_id_is_forwarded():
    azure_auth.get_entra_token("scope-a", client_id="uami-42")
    assert _FakeCredential.last_kwargs == {"managed_identity_client_id": "uami-42"}


def test_system_assigned_identity_passes_no_client_id():
    azure_auth.get_entra_token("scope-a")
    assert _FakeCredential.last_kwargs == {}


def test_concurrent_cold_cache_mints_exactly_one_token():
    """Single-flight. Without the lock, N react_loop threads on a cold cache
    would each mint a token — N network round-trips and N tokens in flight."""
    barrier = threading.Barrier(8)
    results: list[str] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        tok = azure_auth.get_entra_token("scope-a")
        with lock:
            results.append(tok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    assert set(results) == {"token-1"}
    assert _FakeCredential.calls == 1


def test_credential_failure_is_fatal_and_actionable():
    """A misconfigured identity must not degrade to an empty key — that would
    surface later as an opaque 401 from the model endpoint."""
    _FakeCredential.fail_with = RuntimeError("no managed identity found")
    with pytest.raises(azure_auth.AzureAuthError) as exc:
        azure_auth.get_entra_token("scope-a")
    msg = str(exc.value)
    assert "Cognitive Services OpenAI User" in msg  # tells you the missing role
    assert "az login" in msg                        # …and the local fix
