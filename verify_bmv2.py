#!/usr/bin/env python3
# verify_bmv2.py

import subprocess, os, time, sys
from mininet.net import Mininet
from mininet.node import Host, Switch
from mininet.link import TCLink
from mininet.log import setLogLevel

# ── Compile P4 ──────────────────────────────────────────────
def compile_p4():
    print("=== Compiling basic.p4 ===")
    r = subprocess.run(
        ['p4c', '--target', 'bmv2', '--arch', 'v1model',
         '-o', 'build/', 'basic.p4'],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        print("COMPILE ERROR:\n", r.stderr)
        sys.exit(1)
    print("Compiled OK -> build/basic.json")

# ── BMv2 Switch Node ────────────────────────────────────────
class BMv2Switch(Switch):

    def __init__(self, name, json_path, thrift_port, **kwargs):
        Switch.__init__(self, name, **kwargs)
        self.json_path   = json_path
        self.thrift_port = thrift_port
        self.sw_proc     = None
        # Track port assignments for debugging
        self.port_map    = {}

    def start(self, controllers):
        ifaces = []
        port_num = 1   # FIX 1: BMv2 ports start at 1, not 0
        for intf in self.intfs.values():
            if intf.name != 'lo':
                ifaces += ['-i', f'{port_num}@{intf.name}']
                self.port_map[intf.name] = port_num
                port_num += 1

        cmd = (
            ['simple_switch'] +
            ifaces +
            ['--thrift-port', str(self.thrift_port),
             '--log-file', f'/tmp/{self.name}.log',
             '--log-flush',
             '--', self.json_path]
        )
        print(f"  Launching {self.name} with port map: {self.port_map}")
        self.sw_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(2)  # give BMv2 time to fully initialise

    def stop(self, deleteIntfs=True):
        if self.sw_proc:
            self.sw_proc.terminate()
        Switch.stop(self, deleteIntfs)

# ── Install rules via Thrift CLI ────────────────────────────
def install_rules(thrift_port, rules):
    cmds = '\n'.join(rules) + '\n'
    r = subprocess.run(
        ['simple_switch_CLI', '--thrift-port', str(thrift_port)],
        input=cmds, capture_output=True, text=True
    )
    return r.stdout

# ── Main ────────────────────────────────────────────────────
if __name__ == '__main__':
    os.makedirs('build', exist_ok=True)
    compile_p4()

    setLogLevel('warning')

    net = Mininet(controller=None, switch=BMv2Switch,
                  host=Host, link=TCLink)

    h1 = net.addHost('h1', ip='10.0.0.1/24',
                     mac='00:00:00:00:00:01')
    h2 = net.addHost('h2', ip='10.0.0.2/24',
                     mac='00:00:00:00:00:02')
    s1 = net.addSwitch('s1',
                       json_path='build/basic.json',
                       thrift_port=9090)

    net.addLink(h1, s1)
    net.addLink(h2, s1)
    net.start()

    # ── FIX 2: Static ARP entries so no ARP broadcast needed ─
    # Without this, the first packet is always an ARP request
    # (dst MAC = ff:ff:ff:ff:ff:ff) which misses all exact rules
    print("=== Adding static ARP entries ===")
    h1.cmd('arp -s 10.0.0.2 00:00:00:00:00:02')
    h2.cmd('arp -s 10.0.0.1 00:00:00:00:00:01')
    print("  h1 knows h2 MAC, h2 knows h1 MAC -- no ARP needed")

    # Confirm actual port assignments
    print(f"\n=== Port map on s1: {s1.port_map} ===")

    print("=== Installing forwarding rules ===")
    out = install_rules(9090, [
        # h1 MAC -> port 1 (h1-eth0 is on port 1)
        'table_add l2_forward forward 00:00:00:00:00:01 => 1',
        # h2 MAC -> port 2 (h2-eth0 is on port 2)
        'table_add l2_forward forward 00:00:00:00:00:02 => 2',
    ])
    # Print only the important lines
    for line in out.splitlines():
        if 'Adding' in line or 'handle' in line or 'Error' in line:
            print(' ', line)

    time.sleep(1)

    print("\n=== Testing connectivity ===")
    loss = net.ping([h1, h2], timeout=3)
    print(f"Ping loss: {loss}%")

    if loss == 0.0:
        print("\nSUCCESS: BMv2 + Mininet working correctly.")
        print("Ready for Step 2.")
    else:
        # Detailed diagnostics
        print("\nDiagnostics:")
        print("ARP table on h1:", h1.cmd('arp -n'))
        print("ARP table on h2:", h2.cmd('arp -n'))
        print("BMv2 log tail:")
        os.system('tail -20 /tmp/s1.log')

        # Check actual port numbers BMv2 sees
        print("\nBMv2 port status:")
        out = install_rules(9090, ['show_ports'])
        print(out)

    net.stop()
