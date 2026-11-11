"""Unit tests for mihomo egress configuration renderer (offline)."""

import json
import sys
from pathlib import Path
import unittest

# Ensure sibling imports work regardless of runner invocation directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from render import PRIVATE_IP_CIDRS, render_config, parse_protected_hosts

TEMPLATE_PATH = Path(__file__).resolve().parent / 'config.template.json'

PRIVILEGED = 'SRC-IP-CIDR,172.30.1.128/25,host-mihomo,no-resolve'
PORT_GATE = 'AND,((NOT,((DST-PORT,80))),(NOT,((DST-PORT,443)))),REJECT'
PRIVATE_REJECTS = [f'IP-CIDR,{net},REJECT' for net in PRIVATE_IP_CIDRS]


def rule_target(rule):
    """Return the target field of a mihomo rule, ignoring a trailing no-resolve flag."""
    fields = rule.split(',')
    return fields[-2] if fields[-1] == 'no-resolve' else fields[-1]


class BaseConfigTest(unittest.TestCase):
    """Base test case providing template loading and valid default credentials."""

    def setUp(self):
        self.template_text = TEMPLATE_PATH.read_text()
        self.valid_env = {
            'HOST_PROXY_USER': 'testuser',
            'HOST_PROXY_PASS': 'testpass',
            'HOST_PROXY_PORT': '7895',
        }


class TestRendererContract(BaseConfigTest):
    """Tests verifying the full output schema and default contract of render_config."""

    def test_renders_valid_config(self):
        config = render_config(self.template_text, self.valid_env)
        self.assertEqual(config['proxies'][0]['port'], 7895)
        self.assertEqual(config['proxies'][0]['username'], 'testuser')
        self.assertEqual(config['proxies'][0]['password'], 'testpass')
        self.assertEqual(config['proxies'][0]['name'], 'host-mihomo')

    def test_default_rules_no_lan(self):
        """Without LAN_CIDR and PROTECTED_HOSTS, no LAN access is allowed."""
        config = render_config(self.template_text, self.valid_env)
        rules = config['rules']
        self.assertEqual(rules[0], PRIVILEGED)
        # No LAN DIRECT rule should be present
        self.assertFalse(any('DIRECT,no-resolve' in r and 'IP-CIDR' in r for r in rules))
        # Should have privileged, port gate, private rejects, mainland, match
        self.assertEqual(len(rules), 1 + 1 + len(PRIVATE_REJECTS) + 3)

    def test_with_lan_cidr(self):
        """LAN_CIDR adds a DIRECT rule."""
        env = {**self.valid_env, 'LAN_CIDR': '192.168.0.0/16'}
        config = render_config(self.template_text, env)
        rules = config['rules']
        self.assertIn('IP-CIDR,192.168.0.0/16,DIRECT,no-resolve', rules)
        # LAN rule should come before port gate
        lan_idx = rules.index('IP-CIDR,192.168.0.0/16,DIRECT,no-resolve')
        gate_idx = rules.index(PORT_GATE)
        self.assertLess(lan_idx, gate_idx)

    def test_with_protected_hosts(self):
        """PROTECTED_HOSTS adds rejection rules."""
        env = {**self.valid_env, 'PROTECTED_HOSTS': '192.168.1.10:22,192.168.1.20:2022'}
        config = render_config(self.template_text, env)
        rules = config['rules']
        self.assertIn('AND,((IP-CIDR,192.168.1.10/32,no-resolve),(NOT,((DST-PORT,22)))),REJECT', rules)
        self.assertIn('AND,((IP-CIDR,192.168.1.20/32,no-resolve),(NOT,((DST-PORT,2022)))),REJECT', rules)

    def test_protected_hosts_before_lan(self):
        """Protected host rules come before LAN DIRECT rule."""
        env = {
            **self.valid_env,
            'LAN_CIDR': '192.168.0.0/16',
            'PROTECTED_HOSTS': '192.168.1.10:22'
        }
        config = render_config(self.template_text, env)
        rules = config['rules']
        protected_idx = next(i for i, r in enumerate(rules) if '192.168.1.10' in r)
        lan_idx = rules.index('IP-CIDR,192.168.0.0/16,DIRECT,no-resolve')
        self.assertLess(protected_idx, lan_idx)

    def test_invalid_template(self):
        with self.assertRaises(json.JSONDecodeError):
            render_config('not json', self.valid_env)

    def test_missing_proxy_config(self):
        with self.assertRaises(KeyError):
            render_config('{}', self.valid_env)


class TestPolicyInvariants(BaseConfigTest):
    """Tests the ordering and target invariants of the egress policy."""

    def test_port_gate_precedes_private_rejects(self):
        config = render_config(self.template_text, self.valid_env)
        rules = config['rules']
        gate_idx = rules.index(PORT_GATE)
        first_private = rules.index(PRIVATE_REJECTS[0])
        self.assertLess(gate_idx, first_private)

    def test_private_rejects_precede_mainland(self):
        config = render_config(self.template_text, self.valid_env)
        rules = config['rules']
        last_private = max(rules.index(r) for r in PRIVATE_REJECTS)
        cn_domain = rules.index('DOMAIN-SUFFIX,cn,DIRECT')
        self.assertLess(last_private, cn_domain)

    def test_mainland_precedes_match(self):
        config = render_config(self.template_text, self.valid_env)
        rules = config['rules']
        cn_geosite = rules.index('GEOSITE,cn,DIRECT')
        match = rules.index('MATCH,host-mihomo')
        self.assertLess(cn_geosite, match)

    def test_all_targets_valid(self):
        config = render_config(self.template_text, self.valid_env)
        for rule in config['rules']:
            target = rule_target(rule)
            self.assertIn(target, ('host-mihomo', 'DIRECT', 'REJECT'))

    def test_no_proxy_groups_defined(self):
        config = render_config(self.template_text, self.valid_env)
        self.assertNotIn('proxy-groups', config)

    def test_no_la_references(self):
        config = render_config(self.template_text, self.valid_env)
        for rule in config['rules']:
            self.assertNotIn('LA', rule)
            self.assertFalse(rule.startswith('DOMAIN-SUFFIX') and rule.endswith('host-mihomo'))


class TestEnvironmentValidation(BaseConfigTest):
    """Tests validating required and optional environment variable handling."""

    def test_missing_user_rejected(self):
        env = {'HOST_PROXY_PASS': 'testpass', 'HOST_PROXY_PORT': '7895'}
        with self.assertRaises(ValueError, msg='HOST_PROXY_USER'):
            render_config(self.template_text, env)

    def test_missing_pass_rejected(self):
        env = {'HOST_PROXY_USER': 'testuser', 'HOST_PROXY_PORT': '7895'}
        with self.assertRaises(ValueError, msg='HOST_PROXY_PASS'):
            render_config(self.template_text, env)

    def test_default_port(self):
        env = {'HOST_PROXY_USER': 'testuser', 'HOST_PROXY_PASS': 'testpass'}
        config = render_config(self.template_text, env)
        self.assertEqual(config['proxies'][0]['port'], 7895)

    def test_custom_port(self):
        env = {**self.valid_env, 'HOST_PROXY_PORT': '8080'}
        config = render_config(self.template_text, env)
        self.assertEqual(config['proxies'][0]['port'], 8080)

    def test_invalid_port_rejected(self):
        for port in ('0', '65536', 'abc', '-1'):
            env = {**self.valid_env, 'HOST_PROXY_PORT': port}
            with self.assertRaises(ValueError):
                render_config(self.template_text, env)

    def test_default_privileged_cidr(self):
        config = render_config(self.template_text, self.valid_env)
        self.assertEqual(config['rules'][0], PRIVILEGED)

    def test_custom_privileged_cidr(self):
        env = {**self.valid_env, 'PRIVILEGED_CIDR': '10.0.0.0/24'}
        config = render_config(self.template_text, env)
        self.assertEqual(config['rules'][0], 'SRC-IP-CIDR,10.0.0.0/24,host-mihomo,no-resolve')

    def test_invalid_privileged_cidr_rejected(self):
        env = {**self.valid_env, 'PRIVILEGED_CIDR': 'not-a-cidr'}
        with self.assertRaises(ValueError):
            render_config(self.template_text, env)

    def test_invalid_lan_cidr_rejected(self):
        env = {**self.valid_env, 'LAN_CIDR': 'not-a-cidr'}
        with self.assertRaises(ValueError):
            render_config(self.template_text, env)

    def test_invalid_protected_hosts_rejected(self):
        # Missing port
        env = {**self.valid_env, 'PROTECTED_HOSTS': '192.168.1.10'}
        with self.assertRaises(ValueError):
            render_config(self.template_text, env)
        # Invalid port
        env = {**self.valid_env, 'PROTECTED_HOSTS': '192.168.1.10:abc'}
        with self.assertRaises(ValueError):
            render_config(self.template_text, env)
        # Port out of range
        env = {**self.valid_env, 'PROTECTED_HOSTS': '192.168.1.10:99999'}
        with self.assertRaises(ValueError):
            render_config(self.template_text, env)


class TestProtectedHostsParser(unittest.TestCase):
    """Tests for the protected hosts parser."""

    def test_empty_string(self):
        self.assertEqual(parse_protected_hosts(''), [])

    def test_single_host(self):
        result = parse_protected_hosts('192.168.1.10:22')
        self.assertEqual(result, [('192.168.1.10', 22)])

    def test_multiple_hosts(self):
        result = parse_protected_hosts('192.168.1.10:22,192.168.1.20:2022')
        self.assertEqual(result, [('192.168.1.10', 22), ('192.168.1.20', 2022)])

    def test_whitespace_handling(self):
        result = parse_protected_hosts(' 192.168.1.10:22 , 192.168.1.20:2022 ')
        self.assertEqual(result, [('192.168.1.10', 22), ('192.168.1.20', 2022)])

    def test_empty_entries_skipped(self):
        result = parse_protected_hosts('192.168.1.10:22,,192.168.1.20:2022')
        self.assertEqual(result, [('192.168.1.10', 22), ('192.168.1.20', 2022)])


class TestCredentialEscapingAndPreservation(BaseConfigTest):
    """Tests preserving credentials with special characters, quotes, and backslashes."""

    def test_special_characters_preserved(self):
        user_with_special = 'user"with\\special\'chars'
        pass_with_special = 'pass"with\\special\'chars'
        env = {
            'HOST_PROXY_USER': user_with_special,
            'HOST_PROXY_PASS': pass_with_special,
            'HOST_PROXY_PORT': '7895',
        }
        config = render_config(self.template_text, env)
        self.assertEqual(config['proxies'][0]['username'], user_with_special)
        self.assertEqual(config['proxies'][0]['password'], pass_with_special)
        # Verify JSON round-trip
        json_text = json.dumps(config)
        reloaded = json.loads(json_text)
        self.assertEqual(reloaded['proxies'][0]['username'], user_with_special)
        self.assertEqual(reloaded['proxies'][0]['password'], pass_with_special)


class TestTemplateStructure(unittest.TestCase):
    """Tests template JSON loading and error handling."""

    def test_template_is_valid_json(self):
        config = json.loads(TEMPLATE_PATH.read_text())
        self.assertIn('proxies', config)
        self.assertIn('rules', config)
        self.assertEqual(len(config['proxies']), 1)

    def test_template_has_expected_fields(self):
        config = json.loads(TEMPLATE_PATH.read_text())
        expected_fields = ['mixed-port', 'allow-lan', 'bind-address', 'mode', 'log-level',
                          'ipv6', 'sniffer', 'geo-auto-update', 'geodata-mode', 'dns',
                          'proxies', 'rules']
        for field in expected_fields:
            self.assertIn(field, config)


if __name__ == '__main__':
    unittest.main()
