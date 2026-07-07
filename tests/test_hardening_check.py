# SPDX-License-Identifier: MIT
"""Unit tests for host-hardening-check/hardening-check.py.

Every check is a pure function over file contents / parsed values, so the
suite runs on fixtures on any OS. The live sweep runs on the Ubuntu runner
in CI's smoke job.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parent.parent / "host-hardening-check" / "hardening-check.py"
    spec = importlib.util.spec_from_file_location("hardening_check", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


hc = _load_module()


# ---------------------------------------------------------------------------
# sshd_config parsing
# ---------------------------------------------------------------------------

class ParseSshdConfigTest(unittest.TestCase):
    def test_basic_parse_lowercases_keys_and_values(self):
        cfg = hc.parse_sshd_config("PermitRootLogin No\nPasswordAuthentication no\n")
        self.assertEqual(cfg["permitrootlogin"], "no")

    def test_first_occurrence_wins(self):
        cfg = hc.parse_sshd_config("PermitRootLogin no\nPermitRootLogin yes\n")
        self.assertEqual(cfg["permitrootlogin"], "no")

    def test_comments_and_blanks_ignored(self):
        cfg = hc.parse_sshd_config("# PermitRootLogin yes\n\nPermitRootLogin no\n")
        self.assertEqual(cfg["permitrootlogin"], "no")

    def test_match_block_stops_global_parsing(self):
        cfg = hc.parse_sshd_config(
            "PasswordAuthentication no\nMatch User deploy\n    PasswordAuthentication yes\n")
        self.assertEqual(cfg["passwordauthentication"], "no")


class CheckSshTest(unittest.TestCase):
    def test_hardened_config_is_clean(self):
        cfg = {"permitrootlogin": "no", "passwordauthentication": "no"}
        self.assertEqual(hc.check_ssh(cfg), [])

    def test_root_login_yes_is_high(self):
        f = hc.check_ssh({"permitrootlogin": "yes", "passwordauthentication": "no"})
        self.assertEqual(f[0].severity, "high")
        self.assertIn("PermitRootLogin", f[0].message)

    def test_absent_password_auth_defaults_to_enabled(self):
        f = hc.check_ssh({"permitrootlogin": "no"})
        self.assertEqual(len(f), 1)
        self.assertIn("defaults to yes", f[0].message)

    def test_empty_passwords_is_high(self):
        f = hc.check_ssh({"passwordauthentication": "no", "permitemptypasswords": "yes"})
        self.assertEqual(f[0].severity, "high")

    def test_generous_maxauthtries_is_low(self):
        f = hc.check_ssh({"passwordauthentication": "no", "maxauthtries": "20"})
        self.assertEqual(f[0].severity, "low")


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------

class CheckAccountsTest(unittest.TestCase):
    def test_normal_passwd_is_clean(self):
        text = "root:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1::/usr/sbin:/usr/sbin/nologin\n"
        self.assertEqual(hc.check_passwd(text), [])

    def test_duplicate_uid_zero(self):
        text = "root:x:0:0::/root:/bin/bash\nbackdoor:x:0:0::/root:/bin/bash\n"
        f = hc.check_passwd(text)
        self.assertEqual(f[0].severity, "high")
        self.assertIn("backdoor", f[0].message)

    def test_hash_stored_in_passwd(self):
        text = "legacy:$6$salt$hash:1001:1001::/home/legacy:/bin/bash\n"
        f = hc.check_passwd(text)
        self.assertEqual(f[0].severity, "high")
        self.assertIn("world-readable", f[0].message)

    def test_empty_shadow_password(self):
        f = hc.check_shadow("root:$6$ok:19000:0:99999:7:::\nkiosk::19000:0:99999:7:::\n")
        self.assertEqual(len(f), 1)
        self.assertIn("kiosk", f[0].message)

    def test_shadow_world_readable(self):
        self.assertEqual(hc.check_shadow_perms(0o100640), [])
        f = hc.check_shadow_perms(0o100644)
        self.assertEqual(f[0].severity, "high")


# ---------------------------------------------------------------------------
# sudo
# ---------------------------------------------------------------------------

class CheckSudoersTest(unittest.TestCase):
    def test_nopasswd_all_flagged(self):
        f = hc.check_sudoers("deploy ALL=(ALL) NOPASSWD: ALL\n", "/etc/sudoers.d/deploy")
        self.assertEqual(len(f), 1)
        self.assertIn("deploy", f[0].message)

    def test_scoped_nopasswd_not_flagged(self):
        f = hc.check_sudoers("backup ALL=(root) NOPASSWD: /usr/bin/rsync\n", "/etc/sudoers")
        self.assertEqual(f, [])

    def test_comments_and_defaults_ignored(self):
        text = "# user ALL=(ALL) NOPASSWD: ALL\nDefaults env_reset\n"
        self.assertEqual(hc.check_sudoers(text, "/etc/sudoers"), [])

    def test_group_writable_sudoers_file(self):
        self.assertEqual(hc.check_sudoers_file_mode("/etc/sudoers", 0o100440), [])
        f = hc.check_sudoers_file_mode("/etc/sudoers.d/x", 0o100660)
        self.assertEqual(f[0].severity, "high")


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------

TCP_FIXTURE = (
    "  sl  local_address rem_address   st tx_queue rx_queue\n"
    "   0: 00000000:0016 00000000:0000 0A 00000000:00000000\n"   # 0.0.0.0:22 LISTEN
    "   1: 0100007F:1538 00000000:0000 0A 00000000:00000000\n"   # 127.0.0.1:5432 LISTEN
    "   2: 00000000:1F40 00000000:0000 0A 00000000:00000000\n"   # 0.0.0.0:8000 LISTEN
    "   3: 00000000:0050 0100007F:C350 01 00000000:00000000\n"   # established, not LISTEN
)
TCP6_FIXTURE = (
    "  sl  local_address rem_address st\n"
    "   0: 00000000000000000000000000000000:18EB 00000000000000000000000000000000:0000 0A\n"  # [::]:6379
)


class ListenersTest(unittest.TestCase):
    def test_parse_wildcard_listeners(self):
        ports = hc.parse_wildcard_listeners(TCP_FIXTURE, TCP6_FIXTURE)
        # 5432 is bound to localhost only; 80 is ESTABLISHED, not LISTEN
        self.assertEqual(ports, {22, 8000, 6379})

    def test_risky_port_is_high(self):
        f = hc.check_listeners({22, 6379}, allowed={22})
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].severity, "high")
        self.assertIn("Redis", f[0].message)

    def test_unknown_ports_grouped_as_low(self):
        f = hc.check_listeners({8000, 9090}, allowed=set())
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].severity, "low")
        self.assertIn("8000", f[0].message)
        self.assertIn("9090", f[0].message)

    def test_allowed_ports_are_silent(self):
        self.assertEqual(hc.check_listeners({22, 80, 8080}, allowed={22, 80, 8080}), [])


class FirewallTest(unittest.TestCase):
    def test_nothing_queried_stays_silent(self):
        self.assertEqual(hc.check_firewall(None, None, None), [])

    def test_ufw_active_passes(self):
        self.assertEqual(hc.check_firewall("Status: active\n", None, None), [])

    def test_nft_ruleset_passes(self):
        nft = "table inet filter {\n  chain input {\n    type filter hook input priority 0;\n  }\n}\n"
        self.assertEqual(hc.check_firewall(None, nft, None), [])

    def test_iptables_with_rules_passes(self):
        ipt = "-P INPUT ACCEPT\n-A INPUT -p tcp --dport 22 -j ACCEPT\n"
        self.assertEqual(hc.check_firewall(None, None, ipt), [])

    def test_default_accept_and_no_rules_is_flagged(self):
        ipt = "-P INPUT ACCEPT\n-P FORWARD ACCEPT\n-P OUTPUT ACCEPT\n"
        f = hc.check_firewall("Status: inactive\n", "", ipt)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].severity, "medium")


# ---------------------------------------------------------------------------
# kernel sysctls
# ---------------------------------------------------------------------------

class SysctlTest(unittest.TestCase):
    GOOD = {
        "kernel.randomize_va_space": "2",
        "net.ipv4.tcp_syncookies": "1",
        "net.ipv4.conf.all.accept_redirects": "0",
        "net.ipv4.conf.all.rp_filter": "1",
        "kernel.kptr_restrict": "1",
        "fs.protected_symlinks": "1",
        "net.ipv4.ip_forward": "0",
    }

    def test_hardened_values_are_clean(self):
        self.assertEqual(hc.check_sysctls(self.GOOD), [])

    def test_aslr_off_is_high(self):
        f = hc.check_sysctls(dict(self.GOOD, **{"kernel.randomize_va_space": "0"}))
        self.assertEqual(f[0].severity, "high")
        self.assertIn("ASLR", f[0].message)

    def test_missing_values_are_skipped_not_flagged(self):
        self.assertEqual(hc.check_sysctls({}), [])

    def test_ip_forward_is_low_with_caveat(self):
        f = hc.check_sysctls(dict(self.GOOD, **{"net.ipv4.ip_forward": "1"}))
        self.assertEqual(f[0].severity, "low")
        self.assertIn("container hosts", f[0].message)


# ---------------------------------------------------------------------------
# filesystem
# ---------------------------------------------------------------------------

class FilesystemTest(unittest.TestCase):
    def test_world_writable_regular_file(self):
        files = [("/etc/app.conf", 0o100666), ("/etc/sane.conf", 0o100644)]
        f = hc.check_world_writable(files)
        self.assertEqual(len(f), 1)
        self.assertIn("app.conf", f[0].message)

    def test_world_writable_limit(self):
        files = [(f"/etc/f{i}", 0o100666) for i in range(50)]
        self.assertEqual(len(hc.check_world_writable(files, limit=10)), 10)

    def test_tmp_without_sticky_bit(self):
        f = hc.check_sticky_bits([("/tmp", 0o41777), ("/var/tmp", 0o40777)])
        self.assertEqual(len(f), 1)
        self.assertIn("/var/tmp", f[0].message)

    def test_known_suid_binaries_pass(self):
        self.assertEqual(hc.check_suid(["/usr/bin/sudo", "/usr/bin/passwd"]), [])

    def test_unknown_suid_binary_flagged(self):
        f = hc.check_suid(["/usr/local/bin/backdoor", "/usr/bin/sudo"])
        self.assertEqual(len(f), 1)
        self.assertIn("backdoor", f[0].message)


# ---------------------------------------------------------------------------
# patching
# ---------------------------------------------------------------------------

class PatchingTest(unittest.TestCase):
    def test_reboot_required(self):
        f = hc.check_patching(True, False, None)
        self.assertEqual(f[0].severity, "medium")

    def test_apt_without_auto_upgrades(self):
        f = hc.check_patching(False, True, None)
        self.assertIn("unattended-upgrades", f[0].message)

    def test_apt_with_auto_upgrades_enabled(self):
        text = 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n'
        self.assertEqual(hc.check_patching(False, True, text), [])

    def test_non_apt_system_not_flagged(self):
        self.assertEqual(hc.check_patching(False, False, None), [])


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

class RenderTest(unittest.TestCase):
    def test_json_shape_and_sorting(self):
        findings = [hc.Finding("low", "kernel", "a"), hc.Finding("high", "ssh", "b")]
        payload = json.loads(hc.render_json(findings, skipped=["sudo (needs root)"]))
        self.assertEqual(payload["summary"], {"high": 1, "medium": 0, "low": 1})
        self.assertEqual(payload["findings"][0]["severity"], "high")
        self.assertEqual(payload["skipped"], ["sudo (needs root)"])

    def test_clean_table(self):
        self.assertIn("No findings", hc.render_table([], color=False))

    def test_table_counts(self):
        findings = [hc.Finding("high", "ssh", "x"), hc.Finding("high", "ssh", "y")]
        out = hc.render_table(findings, color=False)
        self.assertIn("2 finding(s): 2 high, 0 medium, 0 low", out)


if __name__ == "__main__":
    unittest.main()
