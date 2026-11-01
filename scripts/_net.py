"""HTTPS downloads that verify certificates on any Python install.

python.org builds on macOS ship without a CA bundle, and some Linux Pythons
point at a bundle that is not there; plain `urlopen` then fails with
CERTIFICATE_VERIFY_FAILED. `get` tries the default store first, then the
bundles the machine or `certifi` provides. It never turns verification off.

Stdlib only.
"""

from __future__ import annotations

import os
import ssl
import urllib.error
import urllib.request

_BUNDLES = ("/etc/ssl/cert.pem",                         # macOS, BSD
            "/etc/ssl/certs/ca-certificates.crt",        # Debian, Ubuntu, Arch
            "/etc/pki/tls/certs/ca-bundle.crt",          # Fedora, RHEL
            "/etc/ssl/ca-bundle.pem")                    # openSUSE


def available() -> str:
    """"" when this Python can do HTTPS, else why not."""
    try:
        import _ssl  # noqa: F401
    except ImportError:
        return ("this Python was built without the ssl module, so it cannot download anything. "
                "Install Python from python.org, or on Debian/Ubuntu: apt install python3 libssl3")
    return ""


def _contexts():
    yield ssl.create_default_context()
    try:
        import certifi
        yield ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for env in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if os.environ.get(env):
            yield ssl.create_default_context(cafile=os.environ[env])
    for path in _BUNDLES:
        if os.path.exists(path):
            yield ssl.create_default_context(cafile=path)


def get(url: str, timeout: float = 120) -> bytes:
    why = available()
    if why:
        raise OSError(why)
    last: Exception | None = None
    for ctx in _contexts():
        try:
            with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
                return r.read()
        except urllib.error.URLError as e:
            if not isinstance(getattr(e, "reason", None), ssl.SSLError):
                raise
            last = e
        except ssl.SSLError as e:
            last = e
    raise OSError(f"no CA bundle could verify {url}: {last}. On macOS run "
                  "'Install Certificates.command' from the Python folder; elsewhere install ca-certificates")
