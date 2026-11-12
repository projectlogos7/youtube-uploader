"""The one place that talks HTTPS. Tests replace `request`."""

import http.client
from urllib.parse import urlsplit


def request(method, url, headers=None, body=None, timeout=600):
    """Return (status, lower-cased headers dict, body bytes). Raises OSError on network failure."""
    u = urlsplit(url)
    conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout)
    try:
        conn.request(method, u.path + ("?" + u.query if u.query else ""), body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    except http.client.HTTPException as e:
        raise OSError(str(e)) from None
    finally:
        conn.close()
