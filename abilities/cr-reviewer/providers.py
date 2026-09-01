"""Facts about the review system this ability's scope points at.

WHY THIS PROVIDER EXISTS AT ALL. Every other provider in this tree computes over files that
travel with the ability. This one reaches a host, which is a different kind of dependency and
the reason the capability layer was built: it declares `net`, so a machine that cannot reach
the review system is TOLD so before a run opens, instead of discovering it as a flow error.

WHAT IT DELIBERATELY DOES NOT DO — it never touches a credential store. Reading a review
needs an authenticated session, and a provider that reached into the user's credential files
to get one would be trading a much larger permission for a small fact. So authorization is
reported as a FACT ("this host wants credentials I am not using") rather than acquired. That
line is also the honest limit of what it can answer: measured against the real host, an
existing review and a nonexistent one return the SAME unauthenticated redirect, so whether a
given review EXISTS is not derivable here and no fact claims it.

That split is the general shape, not a special case:

    capability (engine)   is the host reachable at all           -> absent => facts unavailable
    fact (provider)       did it answer, and does it want auth   -> honest values
    not modelled          does this specific review exist        -> would need credentials
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2]
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from engine import facts, operators  # noqa: E402

REVIEW_HOST = "code.amazon.com"
REVIEW_URL = "https://code.amazon.com/reviews/{cr}"
FETCH_TIMEOUT = 6
# Any of these in the redirect target means "this host is asking for credentials", not
# "the thing you asked for is missing". Kept as data so a differently-fronted host needs a
# line here rather than a change in the classification below.
SSO_MARKERS = ("/SSO/", "sso", "midway", "auth")


@facts.provider("cr_review_state",
                requires=({"cmd": "curl"}, {"net": f"{REVIEW_HOST}:443"}),
                schema={
    # Did the review host produce an HTTP response at all. This is the one a claim of having
    # consulted the review can be checked against.
    "cr_host_answered": operators.T_BOOL,
    # It answered by asking for credentials. Not a failure — the expected answer for an
    # unauthenticated probe, and the reason `cr_readable` stays false.
    "cr_needs_authorization": operators.T_BOOL,
    # The review page itself came back. Only possible where the environment already carries an
    # authenticated session; never by this provider obtaining one.
    "cr_readable": operators.T_BOOL,
    "cr_http_status": operators.T_INT,
})
def _cr_review_state(ctx: dict) -> dict:
    """Probe the review host for the review this run is scoped to. No credentials, no writes."""
    empty = {"cr_host_answered": False, "cr_needs_authorization": False,
             "cr_readable": False, "cr_http_status": 0}
    cr = str(ctx.get("scope") or "").strip()
    if not cr:
        return empty
    url = REVIEW_URL.format(cr=cr)
    try:
        proc = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null",
             "-w", "%{http_code} %{redirect_url}",
             "--max-time", str(FETCH_TIMEOUT), url],
            capture_output=True, text=True, timeout=FETCH_TIMEOUT + 4,
        )
    except (OSError, subprocess.SubprocessError):
        return empty
    parts = (proc.stdout or "").strip().split(None, 1)
    status = int(parts[0]) if parts and parts[0].isdigit() else 0
    redirect = parts[1] if len(parts) > 1 else ""
    if status == 0:
        # curl could not get an answer. Distinct from an unavailable CAPABILITY: the engine
        # probed the host as reachable, so this is a transient or per-URL failure and belongs
        # in a fact the flow can condition on.
        return empty
    low = redirect.lower()
    return {
        "cr_host_answered": True,
        "cr_needs_authorization": bool(300 <= status < 400
                                       and any(m in low for m in SSO_MARKERS)),
        "cr_readable": status == 200,
        "cr_http_status": status,
    }
