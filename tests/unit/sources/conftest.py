"""
Test configuration for arip/sources unit tests.

This conftest.py applies to all tests in tests/unit/sources/.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESPX URL-MATCHING PATTERN — read this before writing a new source test
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
When mocking httpx requests with respx 0.21.x (pinned in pyproject.toml),
do NOT use ``mock.get(URL)`` or ``mock.post(URL)``.  These helpers register
a ``Method`` pattern that compares the stored str ``'GET'`` against the
bytes ``b'GET'`` that ``HTTPCoreMocker.to_httpx_request`` passes from
httpcore — the equality check is always False and the route never matches.

This is a known bug fixed in respx ≥ 0.22.0:
  https://github.com/lundberg/respx/issues/277
  https://github.com/lundberg/respx/pull/278

Use the url__startswith pattern instead — it registers no Method pattern
and matches any request whose URL starts with the given prefix:

    with respx.mock as mock:
        mock.route(url__startswith=SOURCE_BASE_URL).mock(
            return_value=httpx.Response(200, text=FIXTURE_XML)
        )
        result = source.fetch()

If respx is upgraded to ≥ 0.22.0, ``mock.get(URL)`` can be restored and
this notice should be removed.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import pytest


@pytest.fixture(autouse=True)
def clear_socks_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove SOCKS proxy environment variables before each source test.

    The sandbox/CI environment may set ``ALL_PROXY=socks5h://...``.  When
    ``httpx.Client()`` is initialised it reads all proxy env vars eagerly and
    tries to build a SOCKS transport, which requires the ``socksio`` package.
    Clearing these variables lets ``httpx.Client`` initialise successfully with
    the remaining HTTP proxy settings, after which ``respx`` intercepts all
    requests via its ``MockTransport`` before any real network I/O occurs.

    Scoped to *this* conftest so the fix does not bleed into unrelated test
    modules.
    """
    for var in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "FTP_PROXY",
        "ftp_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
