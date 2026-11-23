# Claude Desktop / Cowork: admin checklist

TEMPLATE: these steps come from `briefcase-ai connect cowork` and have not been checked against the
current admin console. Menu names may differ; the settings to look for are the OpenTelemetry export
switch and its OTLP endpoint.

Telemetry for Claude Desktop and Cowork is set by an organization admin; nothing on a user's machine
can turn it on.

- [ ] Open Organization settings > Data and privacy > Monitoring.
- [ ] Enable OpenTelemetry export.
- [ ] Set the OTLP endpoint to the receiver your organization runs: `https://otel.example.com`
- [ ] If the receiver needs a token, add the header `Authorization=Bearer <token>`.
- [ ] On the receiver host, if the export arrives as protobuf: `pip install 'briefcase-ai[receiver]'`.
- [ ] Send one message from Claude Desktop and check that a record arrives (`briefcase-ai receive --status`).

Cowork always sends `user.email` and `workspace.host_paths`. The receiver replaces the e-mail with a
keyed hash and drops the paths unless it is started with `--keep-content`.

There is no hook in Claude Desktop that the briefcase-ai policy can use, so the policy is not enforced there.
