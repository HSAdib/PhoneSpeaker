"""
firewall.py — Cross-platform firewall helper (Windows netsh & Linux ufw/firewalld/iptables).
"""

import logging
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

PORTS = [5004, 5005, 5006]
RULE_NAME_TMPL = "PhoneSpeaker UDP {port}"


def _netsh_add_cmd(port: int) -> list:
    return [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={RULE_NAME_TMPL.format(port=port)}",
        "protocol=UDP",
        f"localport={port}",
        "dir=in",
        "action=allow",
        "enable=yes",
        "profile=any",
    ]


def get_firewall_commands() -> list:
    if sys.platform.startswith("linux"):
        if shutil.which("ufw"):
            return [f"sudo ufw allow {p}/udp" for p in PORTS]
        if shutil.which("firewall-cmd"):
            return [f"sudo firewall-cmd --add-port={p}/udp --permanent" for p in PORTS] + ["sudo firewall-cmd --reload"]
        return [f"sudo iptables -I INPUT -p udp --dport {p} -j ACCEPT" for p in PORTS]
    return [" ".join(_netsh_add_cmd(port)) for port in PORTS]


def apply_firewall_rules() -> tuple:
    if sys.platform.startswith("linux"):
        cmds = []
        if shutil.which("ufw"):
            cmds = [f"ufw allow {p}/udp" for p in PORTS]
        elif shutil.which("firewall-cmd"):
            cmds = [f"firewall-cmd --add-port={p}/udp --permanent" for p in PORTS] + ["firewall-cmd --reload"]
        else:
            cmds = [f"iptables -I INPUT -p udp --dport {p} -j ACCEPT" for p in PORTS]

        shell_str = " && ".join(cmds)
        if shutil.which("pkexec"):
            try:
                ret = subprocess.run(["pkexec", "sh", "-c", shell_str], capture_output=True, text=True, timeout=30)
                if ret.returncode == 0:
                    return True, "Linux firewall rules applied for UDP ports 5004, 5005, and 5006."
            except Exception as exc:
                logger.error("Linux pkexec firewall error: %s", exc)
        return False, "Run in terminal:\n" + "\n".join(get_firewall_commands())

    # Windows implementation
    results = []
    direct_ok = True
    for port in PORTS:
        cmd = _netsh_add_cmd(port)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0 or "already exists" in (result.stdout + result.stderr).lower():
                results.append(f"Port {port}: OK")
            else:
                direct_ok = False
                break
        except Exception:
            direct_ok = False
            break

    if direct_ok and results:
        return True, "\n".join(results)

    try:
        ps_commands = "; ".join([
            f"netsh advfirewall firewall add rule name='PhoneSpeaker UDP {p}' protocol=UDP localport={p} dir=in action=allow enable=yes profile=any"
            for p in PORTS
        ])
        elevate_cmd = [
            "powershell.exe", "-NoProfile", "-Command",
            f"Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -Command \"{ps_commands}\"'"
        ]
        ret = subprocess.run(elevate_cmd, capture_output=True, text=True, timeout=20)
        if ret.returncode == 0:
            return True, "Firewall rules requested via administrator prompt.\nPorts 5004, 5005 & 5006 allowed on all profiles."
    except Exception as exc:
        logger.error("Failed to run elevated firewall command: %s", exc)

    return False, (
        "Administrator elevation was cancelled or failed.\n"
        "Please run manually as Administrator:\n"
        + "\n".join(get_firewall_commands())
    )
