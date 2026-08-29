"""Answer one question: is the service using the secret `stripe listen` signs with?

A signature mismatch and a forged webhook look identical from inside the
service -- both are simply unverifiable, and both must be refused. That makes a
misconfigured secret hard to tell apart from an attack, so check it directly.

Prints fingerprints, never the secrets themselves, so the output is safe to
paste or screenshot.

    python examples/check_webhook_secret.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


def main() -> int:
    configured = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    print(f"this shell's STRIPE_WEBHOOK_SECRET : "
          f"{fingerprint(configured) if configured else 'NOT SET'}")

    if not shutil.which("stripe"):
        print("stripe CLI not on PATH; cannot compare.", file=sys.stderr)
        return 1

    result = subprocess.run(["stripe", "listen", "--print-secret"],
                            capture_output=True, text=True)
    listen_secret = result.stdout.strip()
    if not listen_secret.startswith("whsec_"):
        print(f"could not read the CLI's secret: "
              f"{(result.stderr or result.stdout).strip()[:200]}", file=sys.stderr)
        return 1

    print(f"stripe listen signs with          : {fingerprint(listen_secret)}")
    print()
    if configured == listen_secret:
        print("Match. If webhooks still fail to verify, the service was started "
              "in a different shell than this one -- check that shell's value.")
        return 0

    print("MISMATCH. The service cannot verify what the CLI signs.")
    print("Restart the service with the secret `stripe listen` printed:")
    print('    $env:STRIPE_WEBHOOK_SECRET = "<the whsec_... from window 1>"')
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
