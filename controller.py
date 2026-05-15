#!/usr/bin/env python3
# controller.py — P4Runtime controller with dynamic routing
# Reads Hypatia snapshots and updates P4 rules + Mininet delays

import grpc
import socket
import time
import os
import sys
import json
import copy
import argparse
import threading
import subprocess
import statistics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from p4.v1  import p4runtime_pb2, p4runtime_pb2_grpc
from p4.config.v1 import p4info_pb2
import google.protobuf.text_format as text_format

PWD=os.path.dirname(os.path.realpath(__file__))

BASE_DIR = PWD
P4INFO   = os.path.join(BASE_DIR, "build/leo_switch.p4info.txt")
P4JSON   = os.path.join(BASE_DIR, "build/leo_switch.json")

HYPATIA_BASE = PWD+"/hypatia/paper/satellite_networks_state/gen_data/telesat_1015_isls_plus_grid_ground_stations_top_100_algorithm_free_one_only_over_isls"
DYNAMIC_DIR = os.path.join(
    HYPATIA_BASE, "dynamic_state_1000ms_for_200s"
)
GROUND_STATIONS_FILE = os.path.join(HYPATIA_BASE, "ground_stations.txt")

NUM_SATS = 351
DEFAULT_GS_NODE_IDS = {
    'gs1': 351,
    'gs2': 354,
}

STEP_S = 10
DURATION = 200


def load_ground_station_catalog(gs_file=GROUND_STATIONS_FILE):
    stations = []
    if not os.path.exists(gs_file):
        return stations
    with open(gs_file) as f:
        for line in f:
            parts = line.strip().split(',')
            if len(parts) < 2:
                continue
            gs_index = int(parts[0])
            name = parts[1]
            stations.append({
                'index': gs_index,
                'name': name,
                'node_id': NUM_SATS + gs_index,
            })
    return stations


def normalize_ground_station_name(name):
    return ''.join(ch.lower() for ch in name if ch.isalnum())


def resolve_ground_station(selection, stations):
    if selection is None:
        raise ValueError('Ground station selection is required')

    text = str(selection).strip()
    lowered = text.lower()
    if lowered.startswith('gs'):
        lowered = lowered[2:]

    if lowered.isdigit():
        value = int(lowered)
        for station in stations:
            if station['index'] == value or station['node_id'] == value:
                return station

    wanted = normalize_ground_station_name(text)
    for station in stations:
        if normalize_ground_station_name(station['name']) == wanted:
            return station

    raise ValueError(f'Unknown ground station: {selection}')


def build_gs_context(src_selection=None, dst_selection=None):
    stations = load_ground_station_catalog()
    defaults = {
        'gs1': DEFAULT_GS_NODE_IDS['gs1'],
        'gs2': DEFAULT_GS_NODE_IDS['gs2'],
    }

    default_src = next(
        (s for s in stations if s['node_id'] == defaults['gs1']),
        None
    )
    default_dst = next(
        (s for s in stations if s['node_id'] == defaults['gs2']),
        None
    )
    src_station = resolve_ground_station(
        src_selection if src_selection is not None
        else (default_src['index'] if default_src else defaults['gs1']),
        stations
    )
    dst_station = resolve_ground_station(
        dst_selection if dst_selection is not None
        else (default_dst['index'] if default_dst else defaults['gs2']),
        stations
    )
    if src_station['node_id'] == dst_station['node_id']:
        raise ValueError('Source and destination ground stations must be different')

    by_slot = {
        'gs1': src_station,
        'gs2': dst_station,
    }
    return {
        'stations': stations,
        'by_slot': by_slot,
        'node_ids': {
            slot: station['node_id']
            for slot, station in by_slot.items()
        },
        'labels': {
            slot: station['name']
            for slot, station in by_slot.items()
        }
    }


def get_delay_for_satellite(sat_node_id):
    orbit_idx = sat_node_id // 13
    if orbit_idx <= 4 or orbit_idx >= 22:
        return 7.0, 9.0
    elif orbit_idx <= 8 or orbit_idx >= 18:
        return 9.0, 12.0
    elif orbit_idx <= 12 or orbit_idx >= 14:
        return 11.0, 15.0
    else:
        return 13.0, 18.0


def estimate_path_delay(path):
    if not path or len(path) < 2:
        return 0.0

    sats = [n for n in path if n < NUM_SATS]
    if not sats:
        return 5.0

    gsl_ms, isl_ms = get_delay_for_satellite(sats[0])
    n_gsl_hops = 2
    n_isl_hops = max(0, len(sats) - 1)
    total = n_gsl_hops * gsl_ms + n_isl_hops * isl_ms
    return round(total, 1)


def parse_fstate_delta(fstate_path):
    delta = {}
    if not os.path.exists(fstate_path):
        return delta
    with open(fstate_path) as f:
        for line in f:
            parts = line.strip().split(',')
            if len(parts) < 3:
                continue
            src = int(parts[0])
            dst = int(parts[1])
            nh = int(parts[2])
            if src not in delta:
                delta[src] = {}
            delta[src][dst] = nh
    return delta


def trace_path(fwd_table, src, dst, max_hops=15):
    path = [src]
    current = src
    visited = {src}
    for _ in range(max_hops):
        nh = fwd_table.get(current, {}).get(dst)
        if nh is None or nh in visited:
            return None
        path.append(nh)
        visited.add(nh)
        if nh == dst:
            return path
        current = nh
    return None


def load_snapshots(dynamic_dir, duration=DURATION,
                   step_ns=1_000_000_000,
                   gs_node_ids=None,
                   gs_labels=None):
    print('=== Loading Hypatia snapshots (cumulative) ===')
    snapshots = []
    cumulative = {}
    if gs_node_ids is None:
        gs_node_ids = DEFAULT_GS_NODE_IDS
    if gs_labels is None:
        gs_labels = {'gs1': 'Tokyo', 'gs2': 'Sao Paulo'}

    gs1 = gs_node_ids['gs1']
    gs2 = gs_node_ids['gs2']
    prev_path = None
    handover_count = 0

    for t in range(duration):
        ts = t * step_ns
        fpath = os.path.join(dynamic_dir, f'fstate_{ts}.txt')
        delta = parse_fstate_delta(fpath)

        for src, dsts in delta.items():
            if src not in cumulative:
                cumulative[src] = {}
            cumulative[src].update(dsts)

        fwd = copy.deepcopy(cumulative)
        path = trace_path(fwd, gs1, gs2)

        if prev_path is not None and path != prev_path:
            handover_count += 1
            sats_prev = [n for n in prev_path if n < NUM_SATS]
            sats_new = [n for n in path if n < NUM_SATS] if path else []
            print(f'  *** HANDOVER at t={t}s: {sats_prev} -> {sats_new}')

        prev_path = path
        est_delay = estimate_path_delay(path) if path else 0

        if t % 20 == 0:
            sats = [n for n in path if n < NUM_SATS] if path else []
            print(f'  t={t:3d}s: delta={len(delta):4d}, '
                  f'path={path}, sats={sats}, '
                  f'delay≈{est_delay}ms')

        snapshots.append({
            'time_s': t,
            'fwd_table': fwd,
            'path': path,
            'est_delay': est_delay,
        })

    print(f'\nLoaded {len(snapshots)} snapshots.')
    print(f'Total handovers detected: {handover_count}')
    print(f'GS pair: {gs_labels["gs1"]}(node {gs1}) -> '
          f'{gs_labels["gs2"]}(node {gs2})\n')
    return snapshots


def extract_satellites_from_path(path):
    return [node for node in (path or []) if node < NUM_SATS]


def path_edges(path):
    return list(zip(path, path[1:])) if path else []


def satellite_count_bounds(snapshots):
    counts = [
        len(extract_satellites_from_path(snap['path']))
        for snap in snapshots
        if snap.get('path')
    ]
    if not counts:
        return 0, 0
    return min(counts), max(counts)


def derive_potential_gsl_sat_ids(snapshots, gs_node_ids=None):
    if gs_node_ids is None:
        gs_node_ids = DEFAULT_GS_NODE_IDS
    sat_ids = set()
    for snap in snapshots:
        path = snap.get('path') or []
        if len(path) < 2:
            continue
        if path[0] == gs_node_ids['gs1'] and path[1] < NUM_SATS:
            sat_ids.add(path[1])
        if path[-1] == gs_node_ids['gs2'] and path[-2] < NUM_SATS:
            sat_ids.add(path[-2])
    return sat_ids


def path_switch_name(node_id, gs_node_ids=None):
    if gs_node_ids is None:
        gs_node_ids = DEFAULT_GS_NODE_IDS
    if node_id == gs_node_ids['gs1']:
        return 'gs1'
    if node_id == gs_node_ids['gs2']:
        return 'gs2'
    if 0 <= node_id < NUM_SATS:
        return f'sat{node_id}'
    raise ValueError(f'Unsupported node id: {node_id}')


class SwitchConnection:

    def __init__(self, name, host, port, device_id,
                 p4info, p4info_path, bmv2_json_path,
                 election_id=1):
        self.name = name
        self.device_id = device_id
        self.p4info = p4info
        self.election_id = election_id

        addr = f'{host}:{port}'
        channel = grpc.insecure_channel(addr)
        self.stub = p4runtime_pb2_grpc.P4RuntimeStub(channel)

        self._open_stream()
        self._send_master_arbitration()
        self.set_pipeline_config(p4info_path, bmv2_json_path)

        print(f'  [{name}] Connected ({addr}, device_id={device_id})')

    def _open_stream(self):
        self._stream_queue = []
        self._stop = False

        def gen():
            while not self._stop:
                if self._stream_queue:
                    yield self._stream_queue.pop(0)
                else:
                    time.sleep(0.05)

        self._resp = self.stub.StreamChannel(gen())

        def drain():
            try:
                for _ in self._resp:
                    pass
            except Exception:
                pass

        threading.Thread(target=drain, daemon=True).start()

    def _send_master_arbitration(self):
        req = p4runtime_pb2.StreamMessageRequest()
        req.arbitration.device_id = self.device_id
        req.arbitration.election_id.high = 0
        req.arbitration.election_id.low = self.election_id
        self._stream_queue.append(req)
        time.sleep(0.5)

    def set_pipeline_config(self, p4info_path, bmv2_json_path):
        p4info_obj = p4info_pb2.P4Info()
        with open(p4info_path) as f:
            text_format.Parse(f.read(), p4info_obj)
        with open(bmv2_json_path, 'rb') as f:
            dev_cfg = f.read()

        req = p4runtime_pb2.SetForwardingPipelineConfigRequest()
        req.device_id = self.device_id
        req.election_id.high = 0
        req.election_id.low = self.election_id
        req.action = (
            p4runtime_pb2
            .SetForwardingPipelineConfigRequest
            .VERIFY_AND_COMMIT
        )
        req.config.p4info.CopyFrom(p4info_obj)
        req.config.p4_device_config = dev_cfg

        self.stub.SetForwardingPipelineConfig(req)

    def _table_id(self, name):
        for t in self.p4info.tables:
            if t.preamble.name == name:
                return t.preamble.id
        raise KeyError(f'Table not found: {name}')

    def _field_id(self, table, field):
        for t in self.p4info.tables:
            if t.preamble.name == table:
                for mf in t.match_fields:
                    if mf.name == field:
                        return mf.id
        raise KeyError(f'Field {field} not in {table}')

    def _action_id(self, name):
        for a in self.p4info.actions:
            if a.preamble.name == name:
                return a.preamble.id
        raise KeyError(f'Action not found: {name}')

    def _action_param_id(self, action, param):
        for a in self.p4info.actions:
            if a.preamble.name == action:
                for p in a.params:
                    if p.name == param:
                        return p.id
        raise KeyError(f'Param {param} not in {action}')

    def _counter_id(self, name):
        for c in self.p4info.counters:
            if c.preamble.name == name:
                return c.preamble.id
        raise KeyError(f'Counter not found: {name}')

    @staticmethod
    def _parse_prefix(prefix_str):
        ip, plen = prefix_str.split('/')
        return socket.inet_aton(ip), int(plen)

    def write_table_entry(self, table_name, match_fields,
                          action_name, action_params,
                          update_type='INSERT'):
        entry = p4runtime_pb2.TableEntry()
        entry.table_id = self._table_id(table_name)

        for fname, mtype, val in match_fields:
            mf = entry.match.add()
            mf.field_id = self._field_id(table_name, fname)
            if mtype == 'lpm':
                ip_b, plen = self._parse_prefix(val)
                mf.lpm.value = ip_b
                mf.lpm.prefix_len = plen

        action = entry.action.action
        action.action_id = self._action_id(action_name)
        for pname, pval in action_params:
            p = action.params.add()
            p.param_id = self._action_param_id(action_name, pname)
            p.value = pval

        upd = p4runtime_pb2.Update()
        upd.type = getattr(p4runtime_pb2.Update, update_type)
        upd.entity.table_entry.CopyFrom(entry)

        req = p4runtime_pb2.WriteRequest()
        req.device_id = self.device_id
        req.election_id.high = 0
        req.election_id.low = self.election_id
        req.updates.append(upd)
        self.stub.Write(req)

    def clear_table(self, table_name):
        rreq = p4runtime_pb2.ReadRequest()
        rreq.device_id = self.device_id
        ent = rreq.entities.add()
        ent.table_entry.table_id = self._table_id(table_name)

        to_delete = []
        try:
            for resp in self.stub.Read(rreq):
                for e in resp.entities:
                    to_delete.append(e.table_entry)
        except grpc.RpcError:
            return

        if not to_delete:
            return

        wreq = p4runtime_pb2.WriteRequest()
        wreq.device_id = self.device_id
        wreq.election_id.low = self.election_id
        for te in to_delete:
            u = wreq.updates.add()
            u.type = p4runtime_pb2.Update.DELETE
            u.entity.table_entry.CopyFrom(te)
        self.stub.Write(wreq)

    def read_counter_bytes(self, counter_name, index):
        req = p4runtime_pb2.ReadRequest()
        req.device_id = self.device_id
        ent = req.entities.add()
        ent.counter_entry.counter_id = self._counter_id(counter_name)
        ent.counter_entry.index.index = index
        try:
            for resp in self.stub.Read(req):
                for e in resp.entities:
                    return e.counter_entry.data.byte_count
        except grpc.RpcError:
            pass
        return 0


class TrafficGenerator:

    def __init__(self):
        self.h1_pid = self._find_pid('h1')
        self.h2_pid = self._find_pid('h2')
        if self.h1_pid:
            print(f'  TrafficGen: h1 PID={self.h1_pid}')
        else:
            print('  TrafficGen: h1 PID not found')

    def _find_pid(self, host_name):
        for pattern in [f'mininet:{host_name}', f'bash.*{host_name}']:
            try:
                out = subprocess.run(
                    ['pgrep', '-f', pattern],
                    capture_output=True, text=True
                )
                pid = out.stdout.strip().split('\n')[0]
                if pid and pid.isdigit():
                    return pid
            except Exception:
                pass
        return None

    def ping_once(self, count=3, timeout=3):
        if not self.h1_pid:
            return None, 100

        cmd = ['sudo', 'mnexec', '-a', str(self.h1_pid),
               'ping', '-c', str(count),
               '-W', str(timeout), '10.0.2.1']
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=count * timeout + 5
            )
            avg_rtt = None
            loss_pct = 100.0

            for line in out.stdout.splitlines():
                if 'rtt min' in line or 'round-trip' in line:
                    try:
                        avg_rtt = float(
                            line.split('=')[1].strip().split('/')[1]
                        )
                    except Exception:
                        pass
                if 'packet loss' in line:
                    try:
                        loss_pct = float(
                            [x for x in line.split() if '%' in x][0]
                            .replace('%', '')
                        )
                    except Exception:
                        pass
            return avg_rtt, loss_pct
        except Exception:
            return None, 100

    def start_iperf_server(self):
        if not self.h2_pid:
            return
        cmd = ['sudo', 'mnexec', '-a', str(self.h2_pid),
               'iperf3', '-s', '-D']
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(0.5)
            print('  iperf3 server started on h2')
        except Exception as e:
            print(f'  iperf3 server failed: {e}')


class LEOController:

    def __init__(self, p4info_path, policy='shortest_path',
                 switch_addresses=None,
                 switch_device_ids=None,
                 gs_node_ids=None,
                 gs_labels=None):
        self.policy = policy
        self.conns = {}
        self.gs_node_ids = dict(gs_node_ids or DEFAULT_GS_NODE_IDS)
        self.gs_labels = dict(gs_labels or {
            'gs1': 'Tokyo',
            'gs2': 'Sao Paulo',
        })
        self.switch_addresses = (
            switch_addresses or {
                'gs1': ('127.0.0.1', 50051),
                'sat1': ('127.0.0.1', 50052),
                'sat2': ('127.0.0.1', 50053),
                'gs2': ('127.0.0.1', 50054),
            }
        )
        self.switch_device_ids = (
            switch_device_ids or {
                'gs1': 351,
                'sat1': 14,
                'sat2': 15,
                'gs2': 354,
            }
        )
        self.switch_chain = list(self.switch_addresses.keys())
        self.switch_name_by_id = {
            device_id: name
            for name, device_id in self.switch_device_ids.items()
        }
        self.metric_switch = 'gs1'
        self.measurements = {
            'time_s': [],
            'est_delay_ms': [],
            'real_rtt_ms': [],
            'loss_pct': [],
            'isl_hops': [],
            'n_updates': [],
            'satellite_id': [],
            'gsl_delay_ms': [],
            'isl_delay_ms': [],
            'handover': [],
        }
        self._prev_path = None
        self._programmed_switches = set()

        self.p4info = p4info_pb2.P4Info()
        with open(p4info_path) as f:
            text_format.Parse(f.read(), self.p4info)
        print(f'P4Info loaded: {p4info_path}')

    def connect_all(self):
        print('\n=== Connecting to BMv2 switches ===')
        for sw_name in ['gs1', 'gs2']:
            if sw_name in self.switch_addresses:
                self.ensure_switch_connected(sw_name)
        print('Ground stations connected. Satellites will connect on demand.\n')

    def ensure_switch_connected(self, name):
        if name in self.conns:
            return self.conns[name]

        host, port = self.switch_addresses[name]
        self.conns[name] = SwitchConnection(
            name=name,
            host=host,
            port=port,
            device_id=self.switch_device_ids[name],
            p4info=self.p4info,
            p4info_path=P4INFO,
            bmv2_json_path=P4JSON,
        )
        return self.conns[name]

    def switch_path_for_logical_path(self, path):
        return [
            path_switch_name(node_id, self.gs_node_ids)
            for node_id in path
        ]

    def _pb(self, port):
        return port.to_bytes(2, 'big')

    def install_lpm(self, sw, prefix, port,
                    update_type='INSERT'):
        self.conns[sw].write_table_entry(
            table_name='MyIngress.ipv4_lpm',
            match_fields=[('hdr.ipv4.dstAddr', 'lpm', prefix)],
            action_name='MyIngress.do_forward',
            action_params=[('port', self._pb(port))],
            update_type=update_type,
        )

    def apply_rules(self, path, neighbor_ports):
        if not path:
            return 0

        switch_path = self.switch_path_for_logical_path(path)
        for sw in switch_path:
            self.ensure_switch_connected(sw)

        to_clear = self._programmed_switches | set(switch_path)
        for sw in to_clear:
            self.ensure_switch_connected(sw)
            self.conns[sw].clear_table('MyIngress.ipv4_lpm')

        n_rules = 0
        for idx, sw in enumerate(switch_path):
            next_neighbor = 'h2' if idx == len(switch_path) - 1 else switch_path[idx + 1]
            prev_neighbor = 'h1' if idx == 0 else switch_path[idx - 1]

            forward_port = neighbor_ports[sw][next_neighbor]
            reverse_port = neighbor_ports[sw][prev_neighbor]

            self.install_lpm(sw, '10.0.2.1/32', forward_port)
            self.install_lpm(sw, '10.0.1.1/32', reverse_port)
            n_rules += 2

        self.metric_switch = next(
            (sw for sw in switch_path if sw.startswith('sat')),
            'gs1'
        )
        self._programmed_switches = set(switch_path)
        return n_rules

    def update_mininet_topology(self, net, path):
        if net is None or path is None:
            return

        sats = extract_satellites_from_path(path)
        if not sats:
            return

        sat_id = sats[0]
        gsl_ms, isl_ms = get_delay_for_satellite(sat_id)

        print(f'  Dynamic delays: satellite={sat_id} '
              f'GSL={gsl_ms}ms '
              f'ISL={isl_ms}ms '
              f'(orbit {sat_id//13}, logical_sats={len(sats)})')

        from topo import activate_path_links, update_link_delay
        switch_path = activate_path_links(net, path)
        link_delays = []
        for idx, (left, right) in enumerate(zip(switch_path, switch_path[1:])):
            delay = gsl_ms if idx == 0 or idx == len(switch_path) - 2 else isl_ms
            link_delays.append((left, right, delay))

        for node_a, node_b, delay in link_delays:
            update_link_delay(net, node_a, node_b, delay)

        return gsl_ms, isl_ms

    def policy_shortest_path(self, snap):
        return snap['path']

    def policy_load_aware(self, snap):
        utilisation = {}
        for sw, conn in self.conns.items():
            for port in [1, 2]:
                try:
                    b = conn.read_counter_bytes('MyIngress.port_bytes', port)
                    utilisation[f'{sw}_p{port}'] = b
                except Exception:
                    utilisation[f'{sw}_p{port}'] = 0

        print('  [load_aware] utilisation:')
        for k, v in utilisation.items():
            if v > 0:
                print(f'    {k}: {v:,} bytes')
        return snap['path']

    def policy_predictive(self, snap_now, snap_future):
        path_now = snap_now['path']
        path_next = snap_future['path']

        if path_now is None:
            return path_next
        if path_next is None:
            return path_now

        sats_now = [n for n in path_now if n < NUM_SATS]
        sats_next = [n for n in path_next if n < NUM_SATS]

        if sats_now != sats_next:
            print(f'  [predictive] *** HANDOVER AHEAD ***')
            print(f'    Current sats : {sats_now}')
            print(f'    Future  sats : {sats_next}')
            print(f'    Installing future path NOW (proactive handover)')
            return path_next

        return path_now

    def read_counter_throughput(self, port=2, window_s=2):
        self.ensure_switch_connected(self.metric_switch)
        conn = self.conns[self.metric_switch]
        b1 = conn.read_counter_bytes('MyIngress.port_bytes', port)
        time.sleep(window_s)
        b2 = conn.read_counter_bytes('MyIngress.port_bytes', port)
        return max(0, b2 - b1) // window_s

    def run(self, snapshots, traffic_gen=None, net=None):
        print(f'\n=== Dynamic Control Loop ===')
        print(f'Policy  : {self.policy}')
        print(f'Duration: {DURATION}s')
        print(f'Step    : {STEP_S}s')
        print(f'GS pair : {self.gs_labels["gs1"]}'
              f'({self.gs_node_ids["gs1"]}) -> '
              f'{self.gs_labels["gs2"]}'
              f'({self.gs_node_ids["gs2"]})\n')

        if traffic_gen is not None:
            traffic_gen.start_iperf_server()
            time.sleep(1)

        start_wall = time.time()

        for t_s in range(0, DURATION - STEP_S + 1, STEP_S):
            snap_now = snapshots[t_s]
            snap_future = snapshots[min(t_s + STEP_S, len(snapshots) - 1)]

            print(f'\n{"─"*55}')
            print(f'  t = {t_s}s')

            if self.policy == 'shortest_path':
                path = self.policy_shortest_path(snap_now)
            elif self.policy == 'load_aware':
                path = self.policy_load_aware(snap_now)
            elif self.policy == 'predictive':
                path = self.policy_predictive(snap_now, snap_future)
            else:
                raise ValueError(f'Unknown policy: {self.policy}')

            is_handover = (
                self._prev_path is not None and
                path != self._prev_path
            )
            if is_handover:
                prev_sats = [n for n in self._prev_path if n < NUM_SATS]
                new_sats = ([n for n in path if n < NUM_SATS] if path else [])
                print(f'  *** HANDOVER: {prev_sats} -> {new_sats}')
            self._prev_path = path

            if path:
                sats = extract_satellites_from_path(path)
                n_isl = max(0, len(sats) - 1)
                est_delay = snap_now['est_delay']
                sat_id = sats[0] if sats else 0
                gsl_ms, isl_ms = get_delay_for_satellite(sat_id)

                print(f'  Path      : {path}')
                print(f'  Satellites: {sats} (orbit {sat_id//13})')
                print(f'  ISL hops  : {n_isl}')
                print(f'  Est delay : {est_delay} ms '
                      f'(GSL={gsl_ms}ms ISL={isl_ms}ms)')
                print(f'  Handover  : {"YES ⚡" if is_handover else "no"}')
            else:
                n_isl = 0
                est_delay = 0.0
                sat_id = 0
                gsl_ms = isl_ms = 0.0
                print(f'  WARNING: No path at t={t_s}s')

            if path and net is not None:
                neighbor_ports = net.leo_config['neighbor_ports']
                n_rules = self.apply_rules(path, neighbor_ports)
                print(f'  P4 rules  : {n_rules} pushed via gRPC')
            elif path:
                print('  WARNING: No Mininet metadata for path-specific port programming')
                n_rules = 0
            else:
                n_rules = 0

            if path and net is not None:
                self.update_mininet_topology(net, path)
            elif path:
                gsl, isl = get_delay_for_satellite(sat_id if sat_id else 14)
                print(f'  Delays    : GSL={gsl}ms ISL={isl}ms '
                      f'(Mininet not connected)')

            real_rtt = None
            loss_pct = 100.0
            throughput = 0

            if traffic_gen is not None:
                self.ensure_switch_connected(self.metric_switch)
                conn = self.conns[self.metric_switch]
                b1 = conn.read_counter_bytes('MyIngress.port_bytes', 2)

                rtt_results = []
                loss_results = []

                def ping_thread():
                    for _ in range(3):
                        r, l = traffic_gen.ping_once(count=3, timeout=2)
                        if r:
                            rtt_results.append(r)
                        loss_results.append(l)

                pt = threading.Thread(target=ping_thread, daemon=True)
                pt.start()
                time.sleep(4)
                pt.join(timeout=8)

                b2 = conn.read_counter_bytes('MyIngress.port_bytes', 2)

                real_rtt = (sum(rtt_results) / len(rtt_results)
                            if rtt_results else None)
                loss_pct = (sum(loss_results) / len(loss_results)
                            if loss_results else 100.0)
                throughput = max(0, b2 - b1) // 4

                if real_rtt:
                    print(f'  Real RTT  : {real_rtt:.1f} ms '
                          f'(loss={loss_pct:.0f}%)')
                else:
                    print(f'  Real RTT  : TIMEOUT '
                          f'(loss={loss_pct:.0f}%)')
                print(f'  Throughput: {throughput:,} bytes/s')
            else:
                time.sleep(2)

            self.measurements['time_s'].append(t_s)
            self.measurements['est_delay_ms'].append(est_delay)
            self.measurements['real_rtt_ms'].append(real_rtt or 0)
            self.measurements['loss_pct'].append(loss_pct)
            self.measurements['isl_hops'].append(n_isl)
            self.measurements['n_updates'].append(n_rules)
            self.measurements['satellite_id'].append(sat_id)
            self.measurements['gsl_delay_ms'].append(gsl_ms)
            self.measurements['isl_delay_ms'].append(isl_ms)
            self.measurements['handover'].append(1 if is_handover else 0)

            elapsed = time.time() - start_wall
            if elapsed >= DURATION:
                print(f'\nDuration reached ({DURATION}s).')
                break

            step_elapsed = time.time() - start_wall - t_s
            remaining = STEP_S - step_elapsed - 4
            if remaining > 0:
                time.sleep(remaining)

        print('\n=== Control loop complete ===')
        self._print_summary()
        return self.measurements

    def _print_summary(self):
        m = self.measurements
        rtts = [r for r in m['real_rtt_ms'] if r > 0]
        print(f'\n{"="*50}')
        print(f'SUMMARY — Policy: {self.policy}')
        print(f'{"="*50}')
        print(f'Steps run    : {len(m["time_s"])}')
        print(f'Handovers    : {sum(m["handover"])}')
        if rtts:
            print(f'Avg RTT      : {statistics.mean(rtts):.1f} ms')
            print(f'Min/Max RTT  : {min(rtts):.1f} / {max(rtts):.1f} ms')
            if len(rtts) > 1:
                print(f'RTT Std Dev  : {statistics.stdev(rtts):.1f} ms')
        sats = set(m['satellite_id'])
        sats.discard(0)
        print(f'Satellites used: {sorted(sats)}')
        print(f'{"="*50}')


def save_results(measurements, policy, gs_labels=None):
    os.makedirs('results', exist_ok=True)
    if gs_labels is not None:
        measurements['gs_labels'] = dict(gs_labels)
    jpath = f'results/{policy}_measurements.json'
    with open(jpath, 'w') as f:
        json.dump(measurements, f, indent=2)
    print(f'Saved: {jpath}')
    _plot_single(measurements, policy)


def _plot_single(m, policy):
    times = m['time_s']
    est_delay = m['est_delay_ms']
    real_rtt = m['real_rtt_ms']
    hops = m['isl_hops']
    loss = m['loss_pct']
    handovers = m['handover']
    sats = m['satellite_id']
    gs_labels = m.get('gs_labels', {
        'gs1': 'Tokyo',
        'gs2': 'Sao Paulo',
    })

    fig, axes = plt.subplots(4, 1, figsize=(12, 12), sharex=True)

    axes[0].plot(times, est_delay, 'o-', color='steelblue',
                 linewidth=2, label='Estimated delay (Hypatia)')
    if any(r > 0 for r in real_rtt):
        axes[0].plot(times, real_rtt, 's--', color='tomato',
                     linewidth=1.5,
                     label='Measured RTT (ping via P4/BMv2)')

    for t, ho in zip(times, handovers):
        if ho:
            axes[0].axvline(t, color='red', alpha=0.4,
                            linestyle=':', linewidth=2)
            axes[0].annotate('HO', xy=(t, max(est_delay)*0.9),
                             fontsize=7, color='red', ha='center')

    axes[0].set_ylabel('Delay (ms)')
    axes[0].set_title(
        f'LEO TE: {policy.replace("_"," ").title()} '
        f'— {gs_labels["gs1"]} → {gs_labels["gs2"]} (200s)')
    axes[0].legend()
    axes[0].grid(True, alpha=0.4)

    axes[1].step(times, sats, where='post',
                 color='purple', linewidth=1.5)
    axes[1].set_ylabel('Active Satellite ID')
    axes[1].set_title('Satellite Handovers over Time')
    axes[1].grid(True, alpha=0.4)

    axes[2].step(times, hops, where='post',
                 color='orange', linewidth=2)
    axes[2].set_ylabel('ISL Hops')
    axes[2].grid(True, alpha=0.4)

    axes[3].bar(times, loss, width=STEP_S * 0.8,
                color='crimson', alpha=0.7)
    axes[3].set_ylabel('Packet Loss (%)')
    axes[3].set_xlabel('Time (s)')
    axes[3].set_ylim(0, 105)
    axes[3].grid(True, alpha=0.4)

    plt.tight_layout()
    ppath = f'results/{policy}_results.png'
    plt.savefig(ppath, dpi=120)
    plt.close()
    print(f'Plot: {ppath}')


def plot_comparison(results_dir='results'):
    policies = ['shortest_path', 'load_aware', 'predictive']
    colors = ['steelblue', 'orange', 'green']
    styles = ['o-', 's--', '^-.']

    fig, axes = plt.subplots(3, 1, figsize=(13, 10), sharex=False)

    print('\n=== Policy Comparison Statistics ===')
    print(f'{"Policy":<20} {"Avg RTT":>9} '
          f'{"Min":>7} {"Max":>7} '
          f'{"Std":>7} {"HO#":>5}')
    print('-' * 58)

    comparison_labels = None
    for pol, col, sty in zip(policies, colors, styles):
        jpath = os.path.join(results_dir, f'{pol}_measurements.json')
        if not os.path.exists(jpath):
            print(f'  Missing: {jpath}')
            continue
        with open(jpath) as f:
            d = json.load(f)
        if comparison_labels is None:
            comparison_labels = d.get('gs_labels')

        label = pol.replace('_', ' ').title()
        ts = d['time_s']
        rtts = d.get('real_rtt_ms', [])
        if any(r > 0 for r in rtts):
            axes[0].plot(ts, rtts, sty, color=col,
                         label=label, linewidth=1.5,
                         markersize=5)

        axes[1].plot(ts, d['est_delay_ms'],
                     sty, color=col, label=label,
                     linewidth=1.5, markersize=5, alpha=0.8)

        axes[2].step(ts, d.get('satellite_id', []),
                     where='post', color=col,
                     label=label, linewidth=1.5)

        valid = [r for r in rtts if r > 0]
        ho = sum(d.get('handover', []))
        if valid:
            avg = statistics.mean(valid)
            mn = min(valid)
            mx = max(valid)
            std = statistics.stdev(valid) if len(valid) > 1 else 0
            print(f'{pol:<20} {avg:>8.1f}ms {mn:>6.1f}ms '
                  f'{mx:>6.1f}ms {std:>6.1f}ms {ho:>5}')

    print('-' * 58)

    axes[0].set_ylabel('Real RTT (ms)')
    axes[0].set_title(
        'LEO TE Policy Comparison — '
        f'{(comparison_labels or {}).get("gs1", "Tokyo")} → '
        f'{(comparison_labels or {}).get("gs2", "São Paulo")}')
    axes[0].legend()
    axes[0].grid(True, alpha=0.4)

    axes[1].set_ylabel('Estimated Propagation Delay (ms)')
    axes[1].legend()
    axes[1].grid(True, alpha=0.4)

    axes[2].set_ylabel('Active Satellite ID')
    axes[2].set_xlabel('Time (s)')
    axes[2].legend()
    axes[2].grid(True, alpha=0.4)

    plt.tight_layout()
    out = os.path.join(results_dir, 'policy_comparison.png')
    plt.savefig(out, dpi=120)
    plt.close()
    print(f'\nComparison plot: {out}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--policy',
        choices=['shortest_path', 'load_aware', 'predictive'],
        default='shortest_path'
    )
    parser.add_argument('--compare', action='store_true')
    parser.add_argument('--no-traffic', action='store_true')
    parser.add_argument(
        '--src-gs',
        help='Source ground station name, index, or node id'
    )
    parser.add_argument(
        '--dst-gs',
        help='Destination ground station name, index, or node id'
    )
    args = parser.parse_args()

    if args.compare:
        plot_comparison()
        sys.exit(0)

    if not os.path.exists(DYNAMIC_DIR):
        print(f'ERROR: {DYNAMIC_DIR}')
        sys.exit(1)

    gs_ctx = build_gs_context(args.src_gs, args.dst_gs)
    snapshots = load_snapshots(
        DYNAMIC_DIR,
        duration=DURATION,
        gs_node_ids=gs_ctx['node_ids'],
        gs_labels=gs_ctx['labels'],
    )

    tgen = None if args.no_traffic else TrafficGenerator()

    ctrl = LEOController(
        P4INFO,
        policy=args.policy,
        gs_node_ids=gs_ctx['node_ids'],
        gs_labels=gs_ctx['labels'],
    )
    ctrl.connect_all()

    measurements = ctrl.run(snapshots,
                            traffic_gen=tgen,
                            net=None)
    save_results(measurements, args.policy,
                 gs_labels=gs_ctx['labels'])
    print('\nDone.')
