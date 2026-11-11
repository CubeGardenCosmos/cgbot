# Caged - Codex Sandbox Infrastructure

Caged provides a secure, isolated Docker environment for Codex coding sandboxes. It consists of two main services:
a Docker API proxy with an allowlist, and an egress proxy with strict network policies.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Host System                          │
│  ┌──────────────┐         ┌──────────────┐                 │
│  │   Docker     │         │   Mihomo     │                 │
│  │   Socket     │         │   (host)     │                 │
│  └──────┬───────┘         └──────┬───────┘                 │
│         │                        │                          │
│         │                        │  host.docker.internal    │
└─────────┼────────────────────────┼──────────────────────────┘
          │                        │
          │ /var/run/docker.sock   │ :7895 (authenticated)
          │                        │
┌─────────▼────────────────────────▼──────────────────────────┐
│                    Caged Stack (Docker)                     │
│                                                             │
│  ┌─────────────────────┐      ┌─────────────────────┐      │
│  │ caged-docker-proxy  │      │   caged-egress      │      │
│  │  (HAProxy :2375)    │      │   (Mihomo :7890)    │      │
│  └──────────┬──────────┘      └──────────┬──────────┘      │
│             │                            │                 │
│    caged_ctl│                   caged_egress               │
│             │                            │                 │
│             │                   caged_internal             │
│             │                            │                 │
│  ┌──────────▼────────────────────────────▼──────────┐      │
│  │              cgbot + Sandboxes                   │      │
│  └──────────────────────────────────────────────────┘      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Components

### caged-docker-proxy

HAProxy frontend to the Docker socket with a strict allowlist. Only the following endpoints are permitted:

- `GET /version`, `/_ping`, `/containers/json`, `/containers/<id>/json`, `/exec/<id>/json`, `/networks/<name>`
- `POST /containers/create`, `/containers/<id>/(start|stop|exec)`, `/exec/<id>/start`
- `DELETE /containers/<id>`

**All other endpoints are denied**, including `/images/*`, `/containers/<id>/logs`, and `/containers/<id>/wait`.

### caged-egress

Mihomo-based HTTP/SOCKS proxy with strict egress policies:

- External traffic limited to HTTP/HTTPS (ports 80, 443)
- Mainland China domains go DIRECT
- All other traffic goes through an authenticated host proxy
- Private IP ranges are rejected
- Optional LAN access via `LAN_CIDR` and `PROTECTED_HOSTS`

See [egress/README.md](egress/README.md) for detailed policy documentation.

## Networks

- **caged_internal** (172.30.1.0/24): Internal network for sandboxes and cgbot. No internet access.
  - Lower half (172.30.1.0/25): Dynamic IPs for sandboxes
  - Upper half (172.30.1.128/25): Privileged sources (bypass port restrictions)
- **caged_ctl**: Internal network for cgbot to reach docker-proxy. No internet access.
- **caged_egress** (172.30.2.0/24): Outbound network for egress only.
  - Static IP: 172.30.2.2 (configurable via `EGRESS_IP`)
  - Dynamic range: 172.30.2.128/25
- **cgbot_net**: External network for NapCat ↔ cgbot communication (created separately).

## Setup

### 1. Configure Environment

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
# Edit .env with your credentials
```

Required values:
- `HOST_PROXY_USER`, `HOST_PROXY_PASS`: Authentication for the host mihomo listener
- `HOST_PROXY_GROUP`: Target proxy group in host mihomo config

### 2. Install Host Listener

On the Docker host with mihomo running:

```bash
sudo python3 egress/install_host_listener.py
```

This adds a dedicated authenticated listener and source rule to the host mihomo config without restarting
the service.

### 3. Start Caged

```bash
docker compose up -d
```

This creates the networks and starts the docker-proxy and egress services.

### 4. Attach cgbot

cgbot's `deploy/docker-compose.yml` joins the external networks:

```yaml
networks:
  caged_internal:
    external: true
  caged_ctl:
    external: true
  cgbot_net:
    external: true
```

cgbot configuration defaults:

```toml
[coding.docker]
host = "http://caged-docker-proxy:2375"
network = "caged_internal"
egress_proxy = "http://caged-egress:7890"
```

## Security Model

### Isolation

- Sandboxes run on `caged_internal` with no direct internet access
- All external traffic must go through `caged-egress`
- Egress enforces HTTP/HTTPS-only policy and rejects private IP destinations
- Docker API access is restricted to an allowlist via `caged-docker-proxy`

### Hardening

All services run with:
- `cap_drop: [ALL]` - No Linux capabilities
- `security_opt: no-new-privileges:true` - Cannot gain additional privileges
- `label:disable` - SELinux compatibility for volume mounts
- `tmpfs: [/run/mihomo]` - Ephemeral config storage for egress

### Credentials

- Host proxy credentials are required and never logged
- Credentials are serialized safely into mihomo config
- `.env` file is gitignored and must be kept private
- Timestamped backups of host mihomo config are created with mode 0600

## Maintenance

### Updating Egress Policy

After modifying `egress/render.py` or `egress/config.template.json`:

```bash
docker compose up -d --build caged-egress
```

### Testing

```bash
# Unit tests (offline)
python3 -m unittest discover -s egress -p 'test_config.py' -v

# Integration test (on Docker host with listener installed)
python3 egress/test_integration.py
```

### Logs

```bash
docker compose logs -f caged-egress
docker compose logs -f caged-docker-proxy
```

## Troubleshooting

### Egress won't start

Check that all required environment variables are set in `.env`:
- `HOST_PROXY_USER`, `HOST_PROXY_PASS`
- `HOST_PROXY_GROUP`

### Sandboxes can't reach external sites

1. Verify egress is running: `docker compose ps`
2. Check egress logs: `docker compose logs caged-egress`
3. Ensure host mihomo listener is installed and running
4. Verify `HOST_PROXY_GROUP` matches a valid group in host config

### Docker API requests denied

The docker-proxy only allows specific endpoints. Check [docker-proxy/haproxy.cfg](docker-proxy/haproxy.cfg)
for the allowlist. Commonly needed endpoints like `/images/*` and `/containers/<id>/logs` are **denied by design**.
