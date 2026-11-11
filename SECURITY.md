# Security Policy

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please report them using one of the following methods:

1. **GitHub Security Advisories** (preferred): Use the [Security Advisories](https://github.com/CubeGardenCosmos/cgbot/security/advisories/new) feature to privately report vulnerabilities
2. **Private Contact**: Contact the maintainer privately through GitHub

### What to Include

Please include the following information:

- Type of vulnerability
- Location in source code (file path, line numbers)
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

### Response Timeline

- Initial response: Within 48 hours
- Status update: Within 7 days
- Resolution timeline: Depends on severity and complexity

## Security Model

cgbot implements several security measures:

- **Identity Isolation**: Private chats and memory strictly isolated from group chats
- **Secrets Management**: Credentials injected at gateway level, never exposed to sandboxes
- **Sandbox Isolation**: Per-user Docker containers with no docker.sock access
- **Egress Control**: Outbound traffic through controlled proxy
- **Reply-as-Tool**: Models cannot directly output; must use explicit tools

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Known Security Considerations

- Requires proper secrets file permissions (0600)
- Sandboxes must not have access to host Docker socket
- Network isolation must be configured correctly
- API keys must be rotated if compromised

## Acknowledgments

We appreciate responsible disclosure of security vulnerabilities.
