#!/usr/bin/env python3
# topo.py — LEO Mininet topology with BMv2 P4 switches

import subprocess, time, os, sys
from mininet.net    import Mininet
from mininet.node   import Host, Switch
from mininet.link   import TCLink
from mininet.log    import setLogLevel, info, error

PWD=os.path.dirname(os.path.realpath(__file__))
P4_JSON = PWD+"/build/leo_switch.json"
P4_INFO = PWD+"/build/leo_switch.p4info.txt"

# Node IDs match Hypatia Telesat numbering
# Tokyo=GS351, Sao Paulo=GS354 (longer path = more handovers)
NODE_IDS = {
    'gs1' : 351,   # Tokyo
    'sat1': 14,
    'sat2': 15,
    'gs2' : 354,   # Sao Paulo  ← changed from 352 (Delhi)
}

THRIFT_PORTS = {
    'gs1': 9091, 'sat1': 9092, 'sat2': 9093, 'gs2': 9094
}
GRPC_PORTS = {
    'gs1': 50051, 'sat1': 50052, 'sat2': 50053, 'gs2': 50054
}

# Initial link delays — will be updated dynamically
INITIAL_DELAYS = {
    ('h1',   'gs1') : 1,
    ('gs1',  'sat1'): 10,
    ('sat1', 'sat2'): 14,
    ('sat2', 'gs2') : 10,
    ('gs2',  'h2')  : 1,
}


# ============================================================
# BMv2 Switch Node
# ============================================================

class BMv2Switch(Switch):

    def __init__(self, name, json_path, thrift_port,
                 grpc_port, node_id=0, **kwargs):
        Switch.__init__(self, name, **kwargs)
        self.json_path   = json_path
        self.thrift_port = thrift_port
        self.grpc_port   = grpc_port
        self.node_id     = node_id
        self.sw_proc     = None
        self.port_map    = {}

    def start(self, controllers):
        ifaces  = []
        port_no = 1
        for intf in self.intfs.values():
            if intf.name != 'lo':
                ifaces += ['-i', f'{port_no}@{intf.name}']
                self.port_map[intf.name] = port_no
                port_no += 1

        log_path = f'/tmp/bmv2_{self.name}.log'
        cmd = (
            ['simple_switch_grpc'] +
            ifaces +
            [
                '--thrift-port', str(self.thrift_port),
                '--device-id',   str(self.node_id),
                '-L',            'warn',
                self.json_path,
                '--',
                '--grpc-server-addr',
                f'0.0.0.0:{self.grpc_port}',
            ]
        )

        info(f'  Starting {self.name} '
             f'(thrift={self.thrift_port} '
             f'grpc={self.grpc_port} '
             f'node_id={self.node_id})\n')

        self.log_f   = open(log_path, 'w')
        self.sw_proc = subprocess.Popen(
            cmd,
            stdout=self.log_f,
            stderr=self.log_f
        )
        time.sleep(2)

        if self.sw_proc.poll() is not None:
            error(f'ERROR: {self.name} died. '
                  f'Check {log_path}\n')

    def stop(self, deleteIntfs=True):
        if self.sw_proc and self.sw_proc.poll() is None:
            self.sw_proc.terminate()
            self.sw_proc.wait()
        if hasattr(self, 'log_f'):
            self.log_f.close()
        Switch.stop(self, deleteIntfs)

    def is_running(self):
        return (self.sw_proc is not None and
                self.sw_proc.poll() is None)


# ============================================================
# Thrift rule installation (used for initial setup only)
# ============================================================

def thrift_cmd(thrift_port, commands):
    out = subprocess.run(
        ['simple_switch_CLI',
         '--thrift-port', str(thrift_port)],
        input='\n'.join(commands) + '\n',
        capture_output=True, text=True, timeout=10
    )
    return out.stdout


def install_all_rules(switches):
    """Install initial forwarding rules via Thrift CLI."""
    print("\n=== Installing initial forwarding rules ===")

    rules = {
        'gs1' : [
            'table_add ipv4_lpm do_forward 10.0.2.1/32 => 2',
            'table_add ipv4_lpm do_forward 10.0.1.1/32 => 1',
        ],
        'sat1': [
            'table_add ipv4_lpm do_forward 10.0.2.1/32 => 2',
            'table_add ipv4_lpm do_forward 10.0.1.1/32 => 1',
        ],
        'sat2': [
            'table_add ipv4_lpm do_forward 10.0.2.1/32 => 2',
            'table_add ipv4_lpm do_forward 10.0.1.1/32 => 1',
        ],
        'gs2' : [
            'table_add ipv4_lpm do_forward 10.0.2.1/32 => 2',
            'table_add ipv4_lpm do_forward 10.0.1.1/32 => 1',
        ],
    }

    for sw_name, cmds in rules.items():
        out = thrift_cmd(THRIFT_PORTS[sw_name], cmds)
        for line in out.splitlines():
            if any(k in line for k in
                   ['Adding', 'handle', 'Error']):
                print(f'  [{sw_name}] {line.strip()}')

    print("Initial rules installed.\n")


# ============================================================
# Dynamic link delay update
# ============================================================

def update_link_delay(net, node_a, node_b, delay_ms):
    """
    Dynamically update TC delay on a Mininet link.
    Called by controller every topology step.
    """
    try:
        na    = net.get(node_a)
        nb    = net.get(node_b)
        links = net.linksBetween(na, nb)
        if links:
            delay_str = f'{delay_ms:.1f}ms'
            links[0].intf1.config(delay=delay_str)
            links[0].intf2.config(delay=delay_str)
    except Exception as e:
        print(f'  WARNING: delay update {node_a}<->'
              f'{node_b}: {e}')


# ============================================================
# Build network
# ============================================================

def build_network():
    """Build and return the Mininet network."""
    for fpath, label in [
        (P4_JSON, 'leo_switch.json'),
        (P4_INFO, 'leo_switch.p4info.txt')
    ]:
        if not os.path.exists(fpath):
            print(f'ERROR: {label} not found at {fpath}')
            print('Compile with: p4c --target bmv2 '
                  '--arch v1model '
                  '--p4runtime-files build/leo_switch.p4info.txt'
                  ' -o build leo_switch.p4')
            sys.exit(1)

    net = Mininet(controller=None, host=Host, link=TCLink)

    # Both hosts on /16 — kernel sends packets without gateway
    h1 = net.addHost('h1', ip='10.0.1.1/16',
                     mac='00:00:00:00:01:01')
    h2 = net.addHost('h2', ip='10.0.2.1/16',
                     mac='00:00:00:00:02:01')

    switches = {}
    for name in ['gs1', 'sat1', 'sat2', 'gs2']:
        sw = net.addSwitch(
            name,
            cls=BMv2Switch,
            json_path=P4_JSON,
            thrift_port=THRIFT_PORTS[name],
            grpc_port=GRPC_PORTS[name],
            node_id=NODE_IDS[name],
        )
        switches[name] = sw

    # Link order determines port numbering inside BMv2
    net.addLink(h1,              switches['gs1'],
                delay=f"{INITIAL_DELAYS[('h1','gs1')]}ms")
    net.addLink(switches['gs1'], switches['sat1'],
                delay=f"{INITIAL_DELAYS[('gs1','sat1')]}ms")
    net.addLink(switches['sat1'], switches['sat2'],
                delay=f"{INITIAL_DELAYS[('sat1','sat2')]}ms")
    net.addLink(switches['sat2'], switches['gs2'],
                delay=f"{INITIAL_DELAYS[('sat2','gs2')]}ms")
    net.addLink(switches['gs2'], h2,
                delay=f"{INITIAL_DELAYS[('gs2','h2')]}ms")

    return net, switches


# ============================================================
# Verify connectivity
# ============================================================

def verify_connectivity(net):
    h1 = net.get('h1')
    h2 = net.get('h2')

    # Static ARP to bypass ARP broadcasts
    h1.cmd('arp -s 10.0.2.1 00:00:00:00:02:01')
    h2.cmd('arp -s 10.0.1.1 00:00:00:00:01:01')

    print("\n=== Testing connectivity ===")
    loss = net.ping([h1, h2], timeout=3)
    print(f"Ping loss: {loss}%")
    return loss


# ============================================================
# Main — standalone mode (without controller)
# ============================================================

if __name__ == '__main__':
    import signal
    setLogLevel('info')

    print("=== Building LEO Mininet topology ===")
    print(f"GS1 (node {NODE_IDS['gs1']}) = Tokyo")
    print(f"GS2 (node {NODE_IDS['gs2']}) = Sao Paulo")

    net, switches = build_network()
    net.start()

    print("\nWaiting for BMv2 to initialise...")
    time.sleep(3)

    print("\n=== Switch status ===")
    all_ok = True
    for name, sw in switches.items():
        status = "RUNNING" if sw.is_running() else "DEAD"
        print(f"  {name}: {status}  "
              f"(thrift={sw.thrift_port}, "
              f"grpc={sw.grpc_port})")
        if not sw.is_running():
            print(f"    Log: /tmp/bmv2_{name}.log")
            all_ok = False

    if not all_ok:
        print("Some switches failed. Check logs.")
        net.stop()
        sys.exit(1)

    print("\n=== Port maps ===")
    for name, sw in switches.items():
        print(f"  {name}: {sw.port_map}")

    install_all_rules(switches)
    time.sleep(1)

    loss = verify_connectivity(net)

    if loss == 0.0:
        print("\nSUCCESS: Network working.")
        print("\nNetwork is running. Press Ctrl+C to stop.")
        print("Start controller.py or run_experiment.py "
              "in another terminal.")
        try:
            signal.pause()
        except KeyboardInterrupt:
            pass
    else:
        print("\nConnectivity failed.")
        print("Check /tmp/bmv2_gs1.log for errors.")

    net.stop()
    print("Network stopped.")
