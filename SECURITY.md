# Security Policy

We welcome good-faith reports of security vulnerabilities in this
project and will work with reporters to coordinate fixes and disclosure.

## Supported Versions

Security fixes target the latest release and the current default branch.
Older versions may require an upgrade.

## Reporting a Vulnerability

Please report vulnerabilities privately through this repository's
Security → Advisories → Report a vulnerability feature.

Do not include vulnerability details, exploit code, credentials, or
channel keys in public issues, discussions, or pull requests.

Please include:
- Affected version or commit.
- A description of the vulnerability and its potential impact.
- Steps to reproduce it, preferably with a minimal proof of concept.
- Relevant configuration and sanitized logs.
- Any suggested mitigation.

Never include real passwords, API tokens, private messages, or mesh
channel keys. Use test credentials and synthetic data.

## Scope

Reports may concern the bot's code, bundled plugins, configuration,
installation scripts, or dependency usage.

Examples include:
- Command injection or unintended code execution.
- Unauthorized access to administrative commands.
- Disclosure of credentials, channel keys, or private messages.
- Improper handling of radio, MQTT, API, or plugin input.
- Path traversal or unintended file access.
- Input that crashes the bot or causes uncontrolled retransmission.

Third-party firmware, services, and independently maintained plugins
should be reported to their respective maintainers. We welcome reports
showing how those issues affect this project.

## Research Guidelines

Test only on systems and devices you own or have explicit permission
to test. Prefer isolated instances, simulated input, and test channels.

This policy does not authorize testing against public mesh networks,
other people's nodes, hosted services, or the maintainer's infrastructure.

Avoid disrupting radio traffic, flooding channels, accessing other
people's data, or making destructive changes. Stop testing once you
have enough evidence to demonstrate the issue.

## Response and Coordination

We aim to:
- Acknowledge reports within 7 calendar days.
- Provide an initial assessment within 14 calendar days.
- Share progress updates at least every 30 calendar days while a
  confirmed vulnerability remains unresolved.

These are targets, not guarantees.

Please allow up to 90 calendar days from the initial report for
coordinated remediation before publishing technical details.
We will work with you to agree on a disclosure date. Active
exploitation or significant immediate risk may require earlier
disclosure; extensions should be mutually agreed.

If you receive no acknowledgment within 7 days, please follow up
through the private reporting channel.

## Disclosure and Credit

When appropriate, we will publish a security advisory describing
affected versions, impact, fixes, and available mitigations.

We will credit reporters with their permission and respect requests
for anonymity.

## Rewards

This project does not currently offer a paid bug bounty.
