# Security Policy

## Supported versions

While the project is in `0.x`, only the latest minor version receives security fixes.
Once `1.0.0` ships, this section will be updated with a longer support window.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a vulnerability

**Please do not file public GitHub issues for security problems.** Instead, use one of:

- GitHub's [private vulnerability reporting](https://github.com/millerlai/tradestation-data-provider/security/advisories/new)
  (preferred — keeps the discussion attached to the repo).
- Email `miller.lai@gmail.com` with subject prefix `[security] tradestation-data-provider`.

When reporting, include:
- Affected version (`python -c "import tradestation_data; print(tradestation_data.__version__)"`).
- A minimal proof-of-concept or description of the attack surface you observed.
- Whether the issue is exploitable with the default `config/sinks.yaml` or requires a
  modified configuration.

Expect an acknowledgement within 7 days. We will coordinate a fix and disclosure
timeline with you before any public mention.

## Threat model notes

A few properties of this package warrant explicit calling out:

### `config/sinks.yaml` is trusted input

The sink registry resolves each entry's `class:` field via `importlib.import_module`
followed by `getattr` — i.e. arbitrary Python from any installed package. **This is by
design**, because the pluggability point is the whole reason the framework exists. The
flip side is:

- **Never load `sinks.yaml` from untrusted sources** (user uploads, network endpoints,
  unauthenticated APIs). A malicious config that points `class:` at any callable on
  `sys.path` will run that callable with the runtime's privileges.
- Treat `sinks.yaml` the same way you treat a Python file in your codebase: it should
  live in version control alongside your project, reviewed like code.

This is the same trust boundary every plugin-loading framework (pytest entry points,
Django apps, Flask blueprints) maintains. We mention it explicitly so it doesn't get
mistaken for ordinary "config".

### Tick / bar data is not authenticated

The C++ DLL publishes over a local ZeroMQ PUB socket (`tcp://127.0.0.1:5555` by
default). The Python side does not verify the publisher's identity — it trusts whatever
arrives on the subscribed topic. Bind the DLL to a loopback address (the default)
unless you intend to deliberately expose the feed; never bind it to `0.0.0.0` on a
shared network without an authenticated reverse proxy.

### No secrets are persisted by this package

The package does not handle API keys, broker credentials, or any other secrets. If you
build a sink that *does* (e.g. a sink that ships ticks to a remote service), make sure
you read its secrets from environment variables or an external secret store — not from
`sinks.yaml`.
