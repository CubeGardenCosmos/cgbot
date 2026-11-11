"""Render the egress mihomo config without interpolating YAML or logging secrets.

Policy: HTTP/HTTPS (ports 80 and 443) only. Mainland China domains (.cn, geosite:cn) go DIRECT; raw IP
destinations, including mainland ones, go through the authenticated host proxy. Loopback, RFC 1918,
link-local and CGNAT destinations are rejected, except optional LAN access configured via environment.
"""
import ipaddress
import json
import os
from pathlib import Path
import sys
from collections.abc import Mapping

# Destinations no sandbox may reach: loopback, RFC 1918, link-local, CGNAT and IPv6 equivalents.
PRIVATE_IP_CIDRS = ('10.0.0.0/8', '172.16.0.0/12', '127.0.0.0/8', '169.254.0.0/16', '100.64.0.0/10',
                    '::1/128', 'fc00::/7', 'fe80::/10')


def parse_protected_hosts(value: str) -> list[tuple[str, int]]:
    """Parse PROTECTED_HOSTS env var into list of (host, allowed_port) tuples."""
    if not value:
        return []
    result = []
    for entry in value.split(','):
        entry = entry.strip()
        if not entry:
            continue
        if ':' not in entry:
            raise ValueError(f'Invalid PROTECTED_HOSTS entry: {entry} (expected host:port)')
        host, port_str = entry.rsplit(':', 1)
        try:
            port = int(port_str)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise ValueError(f'Invalid port in PROTECTED_HOSTS: {entry}')
        result.append((host, port))
    return result


def render_config(template: str, environ: Mapping[str, str]) -> dict:
    config = json.loads(template)
    user, password = environ.get('HOST_PROXY_USER', ''), environ.get('HOST_PROXY_PASS', '')
    if not user or not password:
        raise ValueError('HOST_PROXY_USER and HOST_PROXY_PASS are required')
    port = int(environ.get('HOST_PROXY_PORT', '7895'))
    if not 1 <= port <= 65535:
        raise ValueError('Invalid host proxy port')
    cidr = str(ipaddress.IPv4Network(environ.get('PRIVILEGED_CIDR', '172.30.1.128/25')))
    
    # Parse LAN configuration
    lan_cidr = environ.get('LAN_CIDR', '').strip()
    if lan_cidr:
        # Validate CIDR
        ipaddress.IPv4Network(lan_cidr)
    
    protected_hosts = parse_protected_hosts(environ.get('PROTECTED_HOSTS', ''))
    
    proxy = config['proxies'][0]
    proxy.update(port=port, username=user, password=password)
    
    rules = [
        f'SRC-IP-CIDR,{cidr},host-mihomo,no-resolve',
    ]
    
    # Add protected host rules (reject all ports except the specified one)
    for host, allowed_port in protected_hosts:
        rules.append(f'AND,((IP-CIDR,{host}/32,no-resolve),(NOT,((DST-PORT,{allowed_port})))),REJECT')
    
    # Add LAN DIRECT rule if configured
    if lan_cidr:
        rules.append(f'IP-CIDR,{lan_cidr},DIRECT,no-resolve')
    
    # External traffic: HTTP/HTTPS only (no SSH, SMTP or arbitrary TCP). Must precede every
    # DIRECT rule for external destinations and the host-mihomo catch-all below.
    rules.append('AND,((NOT,((DST-PORT,80))),(NOT,((DST-PORT,443)))),REJECT')
    
    # Private destinations, resolved so a domain pointing at them is rejected too.
    # Must precede the mainland DIRECT rules so no domain can reach private space directly.
    rules.extend([f'IP-CIDR,{net},REJECT' for net in PRIVATE_IP_CIDRS])
    
    # Mainland China by domain only. GEOIP,CN was tried and rejected: mihomo 1.19.19 reports
    # the host's GeoIP database as invalid, so raw mainland IPs go through the host proxy.
    rules.extend([
        'DOMAIN-SUFFIX,cn,DIRECT',
        'GEOSITE,cn,DIRECT',
        # Everything else (HTTP/HTTPS only) goes through the host proxy.
        'MATCH,host-mihomo',
    ])
    
    config['rules'] = rules
    return config


def main():
    try:
        config = render_config(Path('/app/config.template.json').read_text(), os.environ)
        output = Path('/run/mihomo/config.yaml')
        output.parent.mkdir(parents=True, exist_ok=True)
        # JSON is a YAML subset; serialization safely escapes credential characters.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(config, stream)
    except (ValueError, OSError, KeyError):
        sys.exit('Invalid egress configuration; refusing to start')
    os.execv('/mihomo', ['/mihomo', '-d', '/run/mihomo', '-f', str(output)])


if __name__ == '__main__':
    main()
