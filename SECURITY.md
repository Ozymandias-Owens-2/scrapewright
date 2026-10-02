# Security

scrapewright fetches URLs that strangers choose, renders some of them in a
browser, and holds the credit balances people have paid for. Those are the
three things worth attacking, and the three things this document is about.

## Reporting a vulnerability

Email **fedordyatlov@outlook.com** with "scrapewright security" in the
subject. Please include what you did, what happened, and what you expected.

You will get an acknowledgement within 72 hours and an honest assessment
within a week: this is a one-maintainer project, so that is the schedule it
can actually keep rather than one that sounds impressive. If a fix is
warranted I will tell you when it ships, and credit you unless you would
rather I did not.

Please give it 90 days before publishing. If I have gone quiet for a month,
publish — silence is not a reason to sit on a real finding.

Nothing here is a bug bounty. There is no money behind it, only attention.

## In scope

* `scrapewright.app` and the API behind it
* The `scrapewright` package on PyPI
* This repository, including its build and release workflow

## Out of scope

* The sites scrapewright fetches. Point it at a vulnerability of your own,
  not at someone else's server.
* Denial of service by volume. The rate limits are documented and deliberate.
* Reports from automated scanners with no demonstrated impact.

## What is already defended, and how

**Server-side request forgery.** The service fetches whatever URL a caller
names, which without care makes it a proxy into places the caller cannot
reach: loopback, private ranges, link-local, cloud metadata endpoints. Every
address a hostname resolves to is checked, not just the first; legacy IPv4
spellings (`0177.0.0.1`, `2130706433`) are normalised first; IPv4-mapped IPv6
is unwrapped; and redirects are followed by hand so each hop is checked
again. See `scrapewright/safeurl.py`.

**A known residual risk:** DNS rebinding. Between the check and the
connection, a name can answer differently. Closing it properly means pinning
the resolved address into the socket, which is not done yet. It is written
in the module that would have to change.

**Keys.** API keys, email addresses and IP addresses are stored only as
SHA-256 hashes. A key is shown once at creation and cannot be recovered by
anyone, including me.

**Payments.** Card details never reach this service; Stripe is the merchant
of record. The webhook is verified by signature, and refuses outright if no
signing secret is configured rather than trusting an unsigned caller.

**Secrets** live in Fly secrets and the environment. None are in this
repository, and its history has been checked.

**Robots.** The crawler identifies itself honestly and obeys robots.txt per
RFC 9309, including the parts that read backwards: 4xx allows, 5xx forbids.

**The browser runs on its own machine.** Chromium is the part most likely to
be broken into -- the delivery mechanism for a renderer exploit is a URL,
and this service accepts URLs for money. It runs as a separate Fly app with
no secrets, no volume and no public address, reachable only over the private
network and only with a shared token, stopping between bursts so a
compromise does not outlive the traffic that caused it. An escape there
lands on a container that can fetch web pages, which is what the product
sells. It also starts with a scrubbed environment and its own sandbox where
the kernel allows one.

One caveat, stated because it is a real hole in the above: if the render
service cannot be reached, the API renders in its own process rather than
failing every customer at once. That puts a browser back beside the secrets.
It is logged at error level each time, so it is a thing to notice and fix
rather than a quiet default.

## What is not defended yet, and is known

* **DNS rebinding**, as above.
* **Key rotation** is manual.

These are listed because a security page that claims everything is handled
is worth less than one that says where the edges are.
