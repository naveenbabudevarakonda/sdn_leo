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
HYPATIA_BASE = (
    PWD+"/hypatia/paper/satellite_networks_state/gen_data/"
    "telesat_1015_isls_plus_grid_ground_stations_top_100_"
    "algorithm_free_one_only_over_isls"
)
ISLS_FILE = os.path.join(HYPATIA_BASE, 'isls.txt')

DEFAULT_GROUND_NODE_IDS = {
    'gs1': 351,   # Tokyo
    'gs2': 354,   # Sao Paulo
}
NUM_SATELLITES = 351
THRIFT_BASE_PORT = 9091
GRPC_BASE_PORT = 50051
HOST_LINK_DELAY_MS = 1
DEFAULT_GSL_DELAY_MS = 10
DEFAULT_ISL_DELAY_MS = 14


def node_name_for_id(node_id, ground_node_ids=None):
    if ground_node_ids is None:
        ground_node_ids = DEFAULT_GROUND_NODE_IDS
    if node_id == ground_node_ids['gs1']:
        return 'gs1'
    if node_id == ground_node_ids['gs2']:
        return 'gs2'
    if 0 <= node_id < NUM_SATELLITES:
        return f'sat{node_id}'
    raise ValueError(f'Unsupported node id: {node_id}')


def node_id_for_name(name, ground_node_ids=None):
    if ground_node_ids is None:
        ground_node_ids = DEFAULT_GROUND_NODE_IDS
    if name in ground_node_ids:
        return ground_node_ids[name]
    if name.startswith('sat'):
        return int(name[3:])
    raise ValueError(f'Unsupported node name: {name}')


def satellite_names():
    return [f'sat{sat_id}' for sat_id in range(NUM_SATELLITES)]


def load_isls(isls_file=ISLS_FILE):
    isls = []

    if not os.path.exists(isls_file):
        raise FileNotFoundError(f'ISLs file not found: {isls_file}')

    with open(isls_file) as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            isls.append((int(parts[0]), int(parts[1])))

    return isls


def build_switch_configs(ground_node_ids=None):
    if ground_node_ids is None:
        ground_node_ids = DEFAULT_GROUND_NODE_IDS
    sat_names = satellite_names()
    switch_names = ['gs1', 'gs2', *sat_names]
    node_ids = dict(ground_node_ids)
    for sat_id in range(NUM_SATELLITES):
        node_ids[f'sat{sat_id}'] = sat_id
    thrift_ports = {
        name: THRIFT_BASE_PORT + idx
        for idx, name in enumerate(switch_names)
    }
    grpc_ports = {
        name: GRPC_BASE_PORT + idx
        for idx, name in enumerate(switch_names)
    }

    return {
        'num_satellites': NUM_SATELLITES,
        'satellite_switches': sat_names,
        'switch_names': switch_names,
        'node_ids': node_ids,
        'thrift_ports': thrift_ports,
        'grpc_ports': grpc_ports,
    }


def build_constellation_edges(potential_gsl_sat_ids):
    isls = load_isls()
    edges = []
    for sat_a, sat_b in isls:
        edges.append((f'sat{sat_a}', f'sat{sat_b}',
                      DEFAULT_ISL_DELAY_MS))

    for sat_id in sorted(potential_gsl_sat_ids):
        sat_name = f'sat{sat_id}'
        edges.append(('gs1', sat_name, DEFAULT_GSL_DELAY_MS))
        edges.append((sat_name, 'gs2', DEFAULT_GSL_DELAY_MS))

    return edges


def path_to_switch_names(path, ground_node_ids=None):
    return [
        node_name_for_id(node_id, ground_node_ids)
        for node_id in path
    ]


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
        #time.sleep(2)

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


def install_all_rules(*args, **kwargs):
    """
    Compatibility shim.
    The full-constellation mode now installs path-specific rules
    from the controller after links are activated.
    """
    print('\nSkipping static Thrift rule installation.')
    print('Path-specific rules will be pushed by the controller.\n')


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


def collect_neighbor_ports(net, switches):
    neighbor_ports = {}
    for sw_name, sw in switches.items():
        ports = {}
        for intf_name, port_no in sw.port_map.items():
            intf = sw.intfs[port_no]
            link = getattr(intf, 'link', None)
            if link is None:
                continue
            peer = link.intf1 if link.intf2 == intf else link.intf2
            ports[peer.node.name] = port_no
        neighbor_ports[sw_name] = ports
    net.leo_config['neighbor_ports'] = neighbor_ports
    return neighbor_ports


def set_link_enabled(net, node_a, node_b, enabled):
    try:
        na = net.get(node_a)
        nb = net.get(node_b)
        links = net.linksBetween(na, nb)
        if not links:
            return
        state = 'up' if enabled else 'down'
        links[0].intf1.ifconfig(state)
        links[0].intf2.ifconfig(state)
    except Exception as e:
        print(f'  WARNING: link state {node_a}<->{node_b}: {e}')


def initialize_link_states(net):
    for node_a, node_b in net.leo_config['dynamic_edges']:
        set_link_enabled(net, node_a, node_b, False)
    net.leo_config['active_dynamic_edges'] = set()


def activate_path_links(net, path):
    switch_path = path_to_switch_names(
        path,
        net.leo_config.get('ground_node_ids'),
    )
    desired_edges = {
        tuple(sorted((left, right)))
        for left, right in zip(switch_path, switch_path[1:])
    }
    active_edges = net.leo_config.get('active_dynamic_edges', set())

    for edge in sorted(active_edges - desired_edges):
        set_link_enabled(net, edge[0], edge[1], False)

    for edge in sorted(desired_edges - active_edges):
        set_link_enabled(net, edge[0], edge[1], True)

    net.leo_config['active_dynamic_edges'] = desired_edges
    return switch_path


# ============================================================
# Build network
# ============================================================

def build_network(potential_gsl_sat_ids=None,
                  ground_node_ids=None,
                  ground_labels=None):
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
    if ground_node_ids is None:
        ground_node_ids = dict(DEFAULT_GROUND_NODE_IDS)
    topo_cfg = build_switch_configs(ground_node_ids=ground_node_ids)
    if potential_gsl_sat_ids is None:
        potential_gsl_sat_ids = set()
    potential_gsl_sat_ids = set(potential_gsl_sat_ids)
    constellation_edges = build_constellation_edges(
        potential_gsl_sat_ids
    )

    # Both hosts on /16 — kernel sends packets without gateway
    h1 = net.addHost('h1', ip='10.0.1.1/16',
                     mac='00:00:00:00:01:01')
    h2 = net.addHost('h2', ip='10.0.2.1/16',
                     mac='00:00:00:00:02:01')

    switches = {}
    for name in topo_cfg['switch_names']:
        sw = net.addSwitch(
            name,
            cls=BMv2Switch,
            json_path=P4_JSON,
            thrift_port=topo_cfg['thrift_ports'][name],
            grpc_port=topo_cfg['grpc_ports'][name],
            node_id=topo_cfg['node_ids'][name],
        )
        switches[name] = sw

    node_objs = {'h1': h1, 'h2': h2, **switches}
    net.addLink(h1, switches['gs1'],
                delay=f'{HOST_LINK_DELAY_MS}ms')
    net.addLink(switches['gs2'], h2,
                delay=f'{HOST_LINK_DELAY_MS}ms')

    dynamic_edges = set()
    for left, right, delay_ms in constellation_edges:
        net.addLink(
            node_objs[left],
            node_objs[right],
            delay=f'{delay_ms}ms'
        )
        dynamic_edges.add(tuple(sorted((left, right))))

    topo_cfg['potential_gsl_sat_ids'] = sorted(potential_gsl_sat_ids)
    topo_cfg['dynamic_edges'] = dynamic_edges
    topo_cfg['ground_node_ids'] = dict(ground_node_ids)
    topo_cfg['ground_labels'] = dict(ground_labels or {})
    topo_cfg['always_on_edges'] = {
        tuple(sorted(('h1', 'gs1'))),
        tuple(sorted(('gs2', 'h2'))),
    }
    net.leo_config = topo_cfg

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
    print(f"GS1 (node {DEFAULT_GROUND_NODE_IDS['gs1']}) = Tokyo")
    print(f"GS2 (node {DEFAULT_GROUND_NODE_IDS['gs2']}) = Sao Paulo")

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

    collect_neighbor_ports(net, switches)
    initialize_link_states(net)

    print("\n=== Port maps ===")
    for name, sw in list(switches.items())[:8]:
        print(f"  {name}: {sw.port_map}")
    print(f"  ... total switches: {len(switches)}")

    install_all_rules(switches)
    time.sleep(1)

    print("\nDynamic constellation links are down by default.")
    print("Start run_experiment.py to activate the current "
          "Hypatia path and install forwarding rules.")
    try:
        signal.pause()
    except KeyboardInterrupt:
        pass

    net.stop()
    print("Network stopped.")
