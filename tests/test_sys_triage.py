# SPDX-License-Identifier: MIT
"""Unit tests for sys-triage/triage.py.

All parsers take /proc file contents as strings and all checks take parsed
values, so everything here runs on text fixtures — no Linux needed, which
also means the suite passes on the Windows/macOS dev box. The live path is
exercised on a real Ubuntu runner in CI.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parent.parent / "sys-triage" / "triage.py"
    spec = importlib.util.spec_from_file_location("sys_triage", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


st = _load_module()


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

STAT_BEFORE = "cpu  1000 0 500 8000 200 0 50 250 0 0\ncpu0 500 0 250 4000 100 0 25 125 0 0\n"
STAT_AFTER = "cpu  1400 0 700 8300 400 0 50 450 0 0\ncpu0 700 0 350 4150 200 0 25 225 0 0\n"


class CpuParsingTest(unittest.TestCase):
    def test_parse_loadavg(self):
        self.assertEqual(st.parse_loadavg("1.50 0.75 0.30 2/345 12345\n"), (1.5, 0.75, 0.3))

    def test_parse_stat_cpu_uses_aggregate_line(self):
        cpu = st.parse_stat_cpu(STAT_BEFORE)
        self.assertEqual(cpu["user"], 1000)
        self.assertEqual(cpu["steal"], 250)

    def test_cpu_percentages(self):
        pct = st.cpu_percentages(st.parse_stat_cpu(STAT_BEFORE), st.parse_stat_cpu(STAT_AFTER))
        # deltas: user 400, system 200, idle 300, iowait 200, steal 200 -> total 1300
        self.assertAlmostEqual(pct["user"], 100 * 400 / 1300, places=1)
        self.assertAlmostEqual(pct["steal"], 100 * 200 / 1300, places=1)

    def test_zero_delta_does_not_divide_by_zero(self):
        cpu = st.parse_stat_cpu(STAT_BEFORE)
        self.assertEqual(st.cpu_percentages(cpu, cpu)["user"], 0.0)


class MemoryParsingTest(unittest.TestCase):
    MEMINFO = "MemTotal:       16384000 kB\nMemAvailable:    1200000 kB\nSwapTotal:       2097152 kB\nSwapFree:        2097152 kB\n"

    def test_parse_meminfo(self):
        m = st.parse_meminfo(self.MEMINFO)
        self.assertEqual(m["MemTotal"], 16384000)
        self.assertEqual(m["MemAvailable"], 1200000)

    def test_parse_pressure(self):
        psi = st.parse_pressure("some avg10=1.25 avg60=0.80 avg300=0.30 total=12345\n"
                                "full avg10=0.50 avg60=0.20 avg300=0.10 total=678\n")
        self.assertEqual(psi, {"some_avg10": 1.25, "full_avg10": 0.5})

    def test_parse_vmstat(self):
        vm = st.parse_vmstat("nr_free_pages 12345\npswpin 10\npswpout 400\n")
        self.assertEqual(vm["pswpout"], 400)


DISKSTATS_BEFORE = (
    "   8       0 sda 1000 0 80000 4000 2000 0 160000 9000 0 5000 13000\n"
    "   8       1 sda1 999 0 79000 3900 1999 0 159000 8900 0 4900 12800\n"
    "   7       0 loop0 50 0 400 10 0 0 0 0 0 10 10\n"
    " 259       0 nvme0n1 5000 0 400000 2500 8000 0 640000 4500 0 6000 7000\n"
    " 259       1 nvme0n1p1 4999 0 399000 2499 7999 0 639000 4499 0 5999 6999\n"
)
DISKSTATS_AFTER = (
    "   8       0 sda 1100 0 88000 4500 2100 0 168000 10500 0 6900 15000\n"
    "   8       1 sda1 1099 0 87000 4400 2099 0 167000 10400 0 6800 14800\n"
    "   7       0 loop0 60 0 480 12 0 0 0 0 0 12 12\n"
    " 259       0 nvme0n1 5100 0 408000 2550 8100 0 648000 4550 0 6100 7100\n"
    " 259       1 nvme0n1p1 5099 0 407000 2549 8099 0 647000 4549 0 6099 7099\n"
)


class DiskParsingTest(unittest.TestCase):
    def test_partitions_and_virtual_devices_are_skipped(self):
        devs = st.parse_diskstats(DISKSTATS_BEFORE)
        self.assertEqual(set(devs), {"sda", "nvme0n1"})

    def test_disk_rates(self):
        rates = st.disk_rates(st.parse_diskstats(DISKSTATS_BEFORE),
                              st.parse_diskstats(DISKSTATS_AFTER), interval=2.0)
        # sda: io_ticks delta 1900ms over 2000ms -> 95% busy; 200 ios, 2000ms ticks -> await 10ms
        self.assertAlmostEqual(rates["sda"]["util_pct"], 95.0, places=1)
        self.assertAlmostEqual(rates["sda"]["await_ms"], 10.0, places=1)
        self.assertAlmostEqual(rates["sda"]["iops"], 100.0, places=1)

    def test_skip_dev_patterns(self):
        for name in ("loop0", "ram1", "zram0", "sda2", "nvme0n1p3", "mmcblk0p1", "sr0"):
            self.assertIsNotNone(st.SKIP_DEV_RE.match(name), name)
        for name in ("sda", "nvme0n1", "vdb", "mmcblk0", "dm-0", "md0"):
            self.assertIsNone(st.SKIP_DEV_RE.match(name), name)


class NetworkParsingTest(unittest.TestCase):
    SNMP = ("Ip: Forwarding DefaultTTL\nIp: 2 64\n"
            "Tcp: ActiveOpens PassiveOpens OutSegs RetransSegs\n"
            "Tcp: 100 50 100000 1500\n")

    def test_parse_net_snmp(self):
        tcp = st.parse_net_snmp(self.SNMP)
        self.assertEqual(tcp["OutSegs"], 100000)
        self.assertEqual(tcp["RetransSegs"], 1500)

    def test_tcp_state_counts(self):
        tcp = ("  sl  local_address rem_address   st tx_queue\n"
               "   0: 00000000:0016 00000000:0000 0A 00000000\n"
               "   1: 0100007F:1F90 0100007F:C350 01 00000000\n"
               "   2: 0100007F:1F90 0100007F:C351 06 00000000\n")
        tcp6 = ("  sl  local_address rem_address st\n"
                "   0: 00000000000000000000000000000000:0016 00000000000000000000000000000000:0000 0A\n")
        counts = st.tcp_state_counts(tcp, tcp6)
        self.assertEqual(counts, {"LISTEN": 2, "ESTABLISHED": 1, "TIME_WAIT": 1})


class ProcessParsingTest(unittest.TestCase):
    def test_parse_pid_stat(self):
        line = "1234 (python3) S 1 1234 1234 0 -1 4194304 500 0 0 0 300 200 0 0 20 0 4 0 12345 100000000 2560 18446744073709551615\n"
        p = st.parse_pid_stat(line)
        self.assertEqual(p["comm"], "python3")
        self.assertEqual(p["state"], "S")
        self.assertEqual(p["cpu_jiffies"], 500)   # utime 300 + stime 200
        self.assertEqual(p["rss_pages"], 2560)

    def test_comm_with_spaces_and_parens(self):
        line = "99 (tmux: server (2)) Z 1 99 99 0 -1 4194304 1 0 0 0 5 5 0 0 20 0 1 0 1 1000 10 18446744073709551615\n"
        p = st.parse_pid_stat(line)
        self.assertEqual(p["comm"], "tmux: server (2)")
        self.assertEqual(p["state"], "Z")

    def test_garbage_returns_none(self):
        self.assertIsNone(st.parse_pid_stat("not a stat line"))


class OomScanTest(unittest.TestCase):
    def test_finds_oom_kills(self):
        log = ("[100.0] usb 1-1: new device\n"
               "[200.0] Out of memory: Killed process 4242 (java) total-vm:9000000kB\n"
               "[201.0] oom_reaper: reaped process 4242 (java)\n")
        hits = st.scan_oom(log)
        self.assertEqual(len(hits), 2)
        self.assertIn("Killed process 4242", hits[0])

    def test_clean_log(self):
        self.assertEqual(st.scan_oom("[1.0] all quiet\n"), [])

    def test_file_nr(self):
        self.assertEqual(st.parse_file_nr("4512\t0\t9223372036854775807\n"),
                         (4512, 9223372036854775807))


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

CALM_CPU = {"user": 5.0, "system": 2.0, "iowait": 1.0, "steal": 0.0, "idle": 92.0,
            "nice": 0.0, "irq": 0.0, "softirq": 0.0}


class CheckCpuTest(unittest.TestCase):
    def test_calm_box_has_no_findings(self):
        self.assertEqual(st.check_cpu(0.5, 4, CALM_CPU, {}), [])

    def test_load_over_cores(self):
        f = st.check_cpu(6.0, 4, CALM_CPU, {})
        self.assertEqual([x.severity for x in f], ["medium"])
        f = st.check_cpu(10.0, 4, CALM_CPU, {})
        self.assertEqual([x.severity for x in f], ["high"])

    def test_steal_is_flagged(self):
        cpu = dict(CALM_CPU, steal=25.0)
        f = st.check_cpu(0.5, 4, cpu, {})
        self.assertEqual(f[0].severity, "high")
        self.assertIn("steal", f[0].message)


class CheckMemoryTest(unittest.TestCase):
    def test_healthy_memory(self):
        meminfo = {"MemTotal": 16000000, "MemAvailable": 8000000}
        self.assertEqual(st.check_memory(meminfo, {}, 0, 0, []), [])

    def test_low_available_is_high_severity(self):
        meminfo = {"MemTotal": 16000000, "MemAvailable": 400000}  # 2.5%
        f = st.check_memory(meminfo, {}, 0, 0, [])
        self.assertEqual(f[0].severity, "high")

    def test_swap_thrash(self):
        meminfo = {"MemTotal": 16000000, "MemAvailable": 8000000}
        f = st.check_memory(meminfo, {}, 50, 2000, [])
        self.assertEqual(f[0].severity, "high")
        self.assertIn("thrash", f[0].message)

    def test_oom_kills_reported(self):
        meminfo = {"MemTotal": 16000000, "MemAvailable": 8000000}
        f = st.check_memory(meminfo, {}, 0, 0, ["Out of memory: Killed process 1 (a)"])
        self.assertEqual(f[0].severity, "high")
        self.assertIn("OOM", f[0].message)


class CheckFilesystemsTest(unittest.TestCase):
    def test_thresholds(self):
        mounts = {
            "/": {"used_pct": 50.0, "inode_pct": 10.0},
            "/data": {"used_pct": 90.0, "inode_pct": 10.0},
            "/var": {"used_pct": 40.0, "inode_pct": 97.0},
        }
        f = st.check_filesystems(mounts)
        by_msg = {x.message: x.severity for x in f}
        self.assertEqual(len(f), 2)
        self.assertEqual(by_msg["/data: 90% of space used"], "medium")
        self.assertEqual(by_msg["/var: 97% of inodes used"], "high")


class CheckDiskIoTest(unittest.TestCase):
    def test_saturated_device(self):
        f = st.check_disk_io({"sda": {"util_pct": 99.0, "await_ms": 40.0, "iops": 300.0}})
        self.assertEqual(f[0].severity, "high")

    def test_idle_device_with_stale_util_is_ignored(self):
        # near-zero iops means the util figure is meaningless noise
        f = st.check_disk_io({"sda": {"util_pct": 99.0, "await_ms": 0.0, "iops": 0.1}})
        self.assertEqual(f, [])


class CheckNetworkTest(unittest.TestCase):
    def test_retransmissions(self):
        self.assertEqual(st.check_network(0.1, {}, None), [])
        self.assertEqual(st.check_network(2.0, {}, None)[0].severity, "medium")
        self.assertEqual(st.check_network(8.0, {}, None)[0].severity, "high")

    def test_conntrack_nearly_full(self):
        f = st.check_network(0.0, {}, (90000, 100000))
        self.assertEqual(f[0].severity, "high")
        self.assertIn("conntrack", f[0].message)

    def test_close_wait_pileup(self):
        f = st.check_network(0.0, {"CLOSE_WAIT": 5000}, None)
        self.assertIn("CLOSE_WAIT", f[0].message)


class CheckSystemTest(unittest.TestCase):
    def test_failed_units_listed_individually(self):
        f = st.check_system(None, 0, ["nginx.service", "backup.timer"])
        self.assertEqual(len(f), 2)
        self.assertIn("nginx.service", f[0].message)

    def test_fd_exhaustion(self):
        f = st.check_system((85000, 100000), 0, [])
        self.assertEqual(f[0].severity, "high")

    def test_zombie_gradation(self):
        self.assertEqual(st.check_system(None, 5, []), [])
        self.assertEqual(st.check_system(None, 50, [])[0].severity, "low")
        self.assertEqual(st.check_system(None, 500, [])[0].severity, "medium")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class RenderTest(unittest.TestCase):
    def test_render_json_is_valid_and_sorted(self):
        findings = [st.Finding("low", "system", "a"), st.Finding("high", "cpu", "b")]
        payload = json.loads(st.render_json({"anything": 1}, findings))
        self.assertEqual(payload["summary"], {"high": 1, "medium": 0, "low": 1})
        self.assertEqual([f["severity"] for f in payload["findings"]], ["high", "low"])


if __name__ == "__main__":
    unittest.main()
