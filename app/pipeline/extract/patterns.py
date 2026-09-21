"""The email pattern, shared by every extraction source.

Correction to the spec regex `[a-z0-9][a-z0-9._%+-]{0,63}@(?:[a-z0-9-]+\\.)+[a-z]{2,24}`:

* **It truncates instead of rejecting.** The engine restarts one character in, so a
  66-character local part matched its last 64, `_private@acme.com` yielded
  `private@acme.com`, and `-mike@acme.com` yielded `mike@acme.com` -- addresses that
  never appeared on the page, which Core Rule 1 forbids. A lookbehind now refuses a
  match that begins mid-token, so those produce no match at all. As a side effect the
  "local part longer than 64" drop rule in Stage 6 becomes reachable rather than
  unreachable-by-construction.
* **Underscore was missing from the local part**, so `first_last@acme.com` matched
  `last@acme.com` -- another fabricated address. Added.
* **The local part must end alphanumerically**, so `info.@acme.com` is not matched.
"""

import re

LOCAL_CHARS = r"a-z0-9._%+\-_"

EMAIL = re.compile(
    rf"(?<![{LOCAL_CHARS}@])"                       # never start mid-token
    rf"[a-z0-9](?:[{LOCAL_CHARS}]{{0,62}}[a-z0-9])?"
    r"@"
    r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"[a-z]{2,24}"
    r"(?![a-z0-9\-])",                              # a trailing '.' is fine, letters are not
    re.I,
)

EMAIL_FULL = re.compile(
    rf"^[a-z0-9](?:[{LOCAL_CHARS}]{{0,62}}[a-z0-9])?"
    r"@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24}$",
    re.I,
)


def clean(email: str) -> str:
    return email.strip().strip(".,;:!?)]}>\"'").lower()
