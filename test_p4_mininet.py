#!/usr/bin/env python3
# verify_setup.py

from mininet.net import Mininet
from mininet.topo import Topo
from mininet.node import OVSSwitch, Controller
from mininet.log import setLogLevel

class TwoSwitchTopo(Topo):
    def build(self):
        h1 = self.addHost('h1', ip='10.0.0.1/24')
        h2 = self.addHost('h2', ip='10.0.0.2/24')
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        self.addLink(h1, s1)
        self.addLink(s1, s2)
        self.addLink(s2, h2)

if __name__ == '__main__':
    setLogLevel('info')
    topo = TwoSwitchTopo()

    # controller=None tells Mininet not to search for OpenFlow controller
    # OVSSwitch in standalone mode handles forwarding on its own
    net = Mininet(
        topo=topo,
        switch=OVSSwitch,
        controller=None
    )

    # Set each switch to standalone mode (self-learning L2 switch)
    for sw in net.switches:
        sw.start([])

    net.start()

    print("\n--- Testing h1 to h2 connectivity ---")
    result = net.ping([net.get('h1'), net.get('h2')])
    print(f"Ping loss: {result}%")

    if result == 0.0:
        print("\nSUCCESS: Mininet + OVS switches working correctly.")
        print("Now verify BMv2 is available...")
        import subprocess
        bmv2_check = subprocess.run(
            ['which', 'simple_switch'],
            capture_output=True, text=True
        )
        if bmv2_check.stdout.strip():
            print(f"BMv2 found at: {bmv2_check.stdout.strip()}")
            print("\nFULL SETUP VERIFIED. Ready for Step 2.")
        else:
            print("WARNING: simple_switch not found in PATH.")
            print("Check BMv2 installation before proceeding.")
    else:
        print("\nFAILED: Switch connectivity issue.")

    net.stop()
