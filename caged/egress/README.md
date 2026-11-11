# Sandbox Egress

Mihomo is the only outbound path for sandboxes. Its mixed HTTP/SOCKS listener is `0.0.0.0:7890`; **do not publish
that port on the host**. Domain sniffing remains disabled.

## Policy

Rules are evaluated top to bottom; the first match wins. The authoritative list is `render.py`.

| # | Match | Action |
|---|-------|--------|
| 1 | Source `172.30.1.128/25` (privileged, `PRIVILEGED_CIDR`) | `host-mihomo`, all ports |
| 2 | Protected hosts on any port except configured | `REJECT` (optional, `PROTECTED_HOSTS`) |
| 3 | LAN CIDR (optional, `LAN_CIDR`) | `DIRECT`, all ports |
| 4 | Any destination port other than 80 and 443, external traffic | `REJECT` |
| 5 | `10/8`, `172.16/12`, `127/8`, `169.254/16`, `100.64/10`, `::1`, `fc00::/7`, `fe80::/10` | `REJECT` |
| 6 | Domain `*.cn` or `geosite:cn` (mainland domains only) | `DIRECT` |
| 7 | Everything else, including raw mainland IPs | `host-mihomo` |

In short:

- **External traffic is HTTP and HTTPS only** (ports 80 and 443). Rule 4 rejects SSH, SMTP and other TCP ports
  to any destination not matched by rules 1–3. Privileged sources keep rule 1.
- **Mainland China domains go direct** from the egress container by domain only (rule 6). Raw mainland IPs are
  not special-cased: they go through the host proxy on 80/443 (rule 7).
- **Everything else goes through the host proxy**: the dedicated authenticated host listener pinned to the
  configured proxy group (`HOST_PROXY_GROUP`). The egress config defines no proxy groups.
- **Raw IP destinations, mainland or not,** go through the host proxy on 80/443. They are not rejected, except
  private ranges in rule 5.
- **Private, loopback, link-local and CGNAT destinations are rejected** (rule 5), so sandboxes cannot reach the
  Docker networks (`172.17/16`, `172.30.x`), the egress container or other host services by address.
  Rule 5 resolves domain names through egress DNS, so a domain that resolves into those ranges is also rejected.
- **Optional LAN access**: If `LAN_CIDR` is set, that range gets DIRECT access (rule 3). Protected hosts
  (`PROTECTED_HOSTS`) reject all ports except the specified one (rule 2), allowing SSH access to specific hosts
  while blocking other services.

Sandboxes must send hostnames through the proxy (HTTP CONNECT, or `socks5h`, not locally resolving SOCKS5).
Egress resolves DNS on its internet-connected network; sandbox clients need no public DNS access. Docker
embedded DNS still resolves internal service names.

## Residual risks and requirements

- **Names that egress cannot resolve are not blocked by address.** Rule 5 matches only resolved addresses. A name
  that egress cannot resolve (for example `localhost` or `host.docker.internal`) falls through to rule 7 and is
  resolved by the host proxy on the host itself. Blocking such names needs explicit `DOMAIN` rules. The current
  policy does not include them.
- **Two-resolver gap.** Egress checks the resolved address, and the host proxy resolves the name again. A DNS
  answer that changes between the two lookups (DNS rebinding) can evade rule 5. This is not fully prevented.
- **Raw-IP mainland destinations go through the host proxy.** `GEOIP,CN` was tried and rejected: mihomo v1.19.19
  reports the host's GeoIP database (`Country.mmdb`/`geoip.metadb`) as invalid. No GeoIP-based DIRECT rule exists,
  so a mainland address reached by IP on 80/443 is proxied, not direct. Domain-based mainland traffic stays DIRECT.
- **Mainland domain rules need `geosite.dat`.** Compose mounts the host's copy at `/run/mihomo/geosite.dat`
  (`GEOSITE_FILE`, default `/etc/mihomo/geosite.dat`). The egress container runs with `label:disable`, so the host
  file is not relabeled.

## Compose requirements

```yaml
services:
  caged-egress:
    build: ./egress
    environment:
      HOST_PROXY_PORT: ${HOST_PROXY_PORT:-7895}
      HOST_PROXY_USER: ${HOST_PROXY_USER:?required}
      HOST_PROXY_PASS: ${HOST_PROXY_PASS:?required}
      HOST_PROXY_GROUP: ${HOST_PROXY_GROUP:?required}
      PRIVILEGED_CIDR: ${PRIVILEGED_CIDR:-172.30.1.128/25}
      LAN_CIDR: ${LAN_CIDR:-}
      PROTECTED_HOSTS: ${PROTECTED_HOSTS:-}
    extra_hosts:
      - host.docker.internal:host-gateway
    volumes:
      - ${GEOSITE_FILE:-/etc/mihomo/geosite.dat}:/run/mihomo/geosite.dat:ro
    networks:
      caged_internal: {}
      caged_egress:
        ipv4_address: ${EGRESS_IP:-172.30.2.2}
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true, label:disable]
    tmpfs: [/run/mihomo]
networks:
  caged_internal:
    name: caged_internal
    internal: true
    ipam:
      config:
        - subnet: 172.30.1.0/24
          ip_range: 172.30.1.0/25
  caged_egress:
    name: caged_egress
    ipam:
      config:
        - subnet: ${EGRESS_SUBNET:-172.30.2.0/24}
          ip_range: 172.30.2.128/25
```

The example is abbreviated. The repository compose file is authoritative.

Only egress joins the internet-connected egress network. Sandbox clients do not.
Sandboxes use `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` = `http://caged-egress:7890`; `NO_PROXY` includes
`caged-docker-proxy,localhost,127.0.0.1`. Rebuild and recreate egress after editing `render.py` or
`config.template.json`. Invalid settings or missing credentials prevent startup. `PRIVILEGED_CIDR` is optional
(default `172.30.1.128/25`); only change it together with manager IP allocation. Never expose host proxy
credentials to sandboxes.

## Dedicated host listener

Host `.env` (gitignored) must contain generated `HOST_PROXY_USER`, `HOST_PROXY_PASS`, `HOST_PROXY_PORT=7895`,
`HOST_PROXY_GROUP`, `EGRESS_SUBNET=172.30.2.0/24`, and `EGRESS_IP=172.30.2.2`. These are credentials, not literal
YAML substitutions; the renderer serializes them safely. The host listener requires a recent mihomo supporting
authenticated `listeners` and the listener `proxy` target.

The setup uses **both** a separate authenticated listener and a source-scoped rule. Existing host
listener/rules/proxy groups are left intact; only traffic originating in the dedicated egress subnet matches the
new rule. The dedicated listener pins traffic to the configured `HOST_PROXY_GROUP` target independently of the
host's normal rule routing.

Add the following to the existing host `/etc/mihomo/config.yaml` (merge, do not replace). Substitute credentials
from `.env` without logging them:

```yaml
listeners:
  - name: caged
    type: mixed
    listen: 0.0.0.0
    port: 7895
    users:
      - username: <HOST_PROXY_USER>
        password: <HOST_PROXY_PASS>
    proxy: <HOST_PROXY_GROUP>
rules:
  - SRC-IP-CIDR,172.30.2.0/24,<HOST_PROXY_GROUP>,no-resolve
  # all pre-existing rules follow unchanged
```

`sudo python3 egress/install_host_listener.py` implements this on the Docker host: timestamped mode-0600 backup,
candidate `mihomo -t`, atomic config replacement, and hot reload using the existing loopback controller. It never
restarts the host service. Reload failure restores and reloads the original. The host needs PyYAML. Config/backup
contain credentials and must stay private. Keep the listener firewalled to the Docker host/project network where
possible; auth is mandatory even on the dedicated port. No host `:7890` setting is changed.

## Tests

```sh
python3 -m unittest discover -s egress -p 'test_config.py' -v   # offline renderer tests
# On the Docker host, after the dedicated listener is installed and the new image is deployed:
python3 egress/test_integration.py
```

The unit tests check the rendered rule list, its ordering invariants, that every target is `host-mihomo`,
`DIRECT` or `REJECT`, that no rule references `LA` or defines a proxy group, and that the renderer rejects
invalid credentials, ports and privileged CIDRs.

The integration test builds a uniquely named disposable image and two temporary networks, with lower-/upper-half
client IPs in `172.29.251.0/24`. It checks mainland HTTPS direct, non-mainland HTTPS through the host proxy,
privileged HTTPS, no direct internet for both clients, and rejection of a non-80/443 port and a private
destination. Containers, test networks and the test image are removed even on failure. Registry/base image caches
are not deleted.
