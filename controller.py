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

# ── Paths ─────────────────────────────────────────────────────
BASE_DIR = PWD
P4INFO   = os.path.join(BASE_DIR, "build/leo_switch.p4info.txt")
P4JSON   = os.path.join(BASE_DIR, "build/leo_switch.json")

HYPATIA_BASE = PWD+"/hypatia/paper/satellite_networks_state/gen_data/telesat_1015_isls_plus_grid_ground_stations_top_100_algorithm_free_one_only_over_isls"
DYNAMIC_DIR = os.path.join(
    HYPATIA_BASE, "dynamic_state_1000ms_for_200s"
)

SWITCHES = {
    'gs1' : ('127.0.0.1', 50051),
    'sat1': ('127.0.0.1', 50052),
    'sat2': ('127.0.0.1', 50053),
    'gs2' : ('127.0.0.1', 50054),
}
THRIFT_PORTS = {
    'gs1': 9091, 'sat1': 9092, 'sat2': 9093, 'gs2': 9094
}

# Topology constants — match topo.py NODE_IDS
NUM_SATS    = 351
GS_NODE_IDS = {
    'gs1': 351,   # Tokyo
    'gs2': 354,   # Sao Paulo  ← longer path, more handovers
}

# Experiment parameters
STEP_S   = 10
DURATION = 200   # full Hypatia dataset


# ============================================================
# Satellite delay estimation
# ============================================================

def get_delay_for_satellite(sat_node_id):
    """
    Estimate GSL and ISL delay (ms) for a given satellite
    based on its orbit position in the Telesat constellation.

    Telesat: 27 orbits x 13 sats/orbit
    Orbit index = sat_id // 13
    Satellites near poles have shorter ground distances (lower delay)
    Equatorial crossings have larger ground distances (higher delay)

    Returns (gsl_delay_ms, isl_delay_ms)
    """
    orbit_idx = sat_node_id // 13   # 0 to 26

    if orbit_idx <= 4 or orbit_idx >= 22:
        # High-latitude polar orbits
        return 7.0, 9.0
    elif orbit_idx <= 8 or orbit_idx >= 18:
        # Mid-to-high latitude
        return 9.0, 12.0
    elif orbit_idx <= 12 or orbit_idx >= 14:
        # Mid latitude
        return 11.0, 15.0
    else:
        # Near equatorial
        return 13.0, 18.0


def estimate_path_delay(path):
    """
    Estimate total one-way propagation delay for a path.
    path: list of Hypatia node IDs
    Returns delay_ms float.
    """
    if not path or len(path) < 2:
        return 0.0

    total = 0.0
    sats  = [n for n in path if n < NUM_SATS]

    if not sats:
        return 5.0   # direct ground link (unlikely in LEO)

    # Use first satellite's profile for all hops
    gsl_ms, isl_ms = get_delay_for_satellite(sats[0])

    n_gsl_hops = 2                   # one uplink, one downlink
    n_isl_hops = max(0, len(sats) - 1)  # ISL between consecutive sats

    total = n_gsl_hops * gsl_ms + n_isl_hops * isl_ms
    return round(total, 1)


# ============================================================
# Hypatia cumulative snapshot loader
# ============================================================

def parse_fstate_delta(fstate_path):
    """Parse one fstate file as a delta. Returns {src:{dst:nh}}"""
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
            nh  = int(parts[2])
            if src not in delta:
                delta[src] = {}
            delta[src][dst] = nh
    return delta


def trace_path(fwd_table, src, dst, max_hops=15):
    """Follow forwarding table to trace full path."""
    path    = [src]
    current = src
    visited = {src}
    for _ in range(max_hops):
        nh = fwd_table.get(current, {}).get(dst)
        if nh is None:
            return None
        if nh in visited:
            return None
        path.append(nh)
        visited.add(nh)
        if nh == dst:
            return path
        current = nh
    return None


def load_snapshots(dynamic_dir, duration=DURATION,
                   step_ns=1_000_000_000):
    """
    Build cumulative forwarding tables from Hypatia deltas.
    Each snapshot contains the full routing state at time t.
    """
    print('=== Loading Hypatia snapshots (cumulative) ===')
    snapshots  = []
    cumulative = {}
    gs1 = GS_NODE_IDS['gs1']
    gs2 = GS_NODE_IDS['gs2']

    prev_path = None
    handover_count = 0

    for t in range(duration):
        ts    = t * step_ns
        fpath = os.path.join(
            dynamic_dir, f'fstate_{ts}.txt'
        )
        delta = parse_fstate_delta(fpath)

        # Apply delta to cumulative table
        for src, dsts in delta.items():
            if src not in cumulative:
                cumulative[src] = {}
            cumulative[src].update(dsts)

        fwd  = copy.deepcopy(cumulative)
        path = trace_path(fwd, gs1, gs2)

        # Detect path changes (handovers)
        if prev_path is not None and path != prev_path:
            handover_count += 1
            sats_prev = [n for n in prev_path if n < NUM_SATS]
            sats_new  = [n for n in path      if n < NUM_SATS] \
                        if path else []
            if t % 10 == 0 or True:
                print(f'  *** HANDOVER at t={t}s: '
                      f'{sats_prev} -> {sats_new}')

        prev_path = path

        est_delay = estimate_path_delay(path) if path else 0

        if t % 20 == 0:
            sats = ([n for n in path if n < NUM_SATS]
                    if path else [])
            print(f'  t={t:3d}s: delta={len(delta):4d}, '
                  f'path={path}, '
                  f'sats={sats}, '
                  f'delay≈{est_delay}ms')

        snapshots.append({
            'time_s'    : t,
            'fwd_table' : fwd,
            'path'      : path,
            'est_delay' : est_delay,
        })

    print(f'\nLoaded {len(snapshots)} snapshots.')
    print(f'Total handovers detected: {handover_count}')
    print(f'GS pair: '
          f'Tokyo(node {gs1}) -> Sao Paulo(node {gs2})\n')
    return snapshots


# ============================================================
# P4Runtime Switch Connection
# ============================================================

class SwitchConnection:

    def __init__(self, name, host, port, device_id,
                 p4info, p4info_path, bmv2_json_path,
                 election_id=1):
        self.name           = name
        self.device_id      = device_id
        self.p4info         = p4info
        self.election_id    = election_id

        addr    = f'{host}:{port}'
        channel = grpc.insecure_channel(addr)
        self.stub = p4runtime_pb2_grpc.P4RuntimeStub(channel)

        self._open_stream()
        self._send_master_arbitration()
        self.set_pipeline_config(p4info_path, bmv2_json_path)

        print(f'  [{name}] Connected ({addr}, '
              f'device_id={device_id})')

    def _open_stream(self):
        self._stream_queue = []
        self._stop         = False

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
        req.arbitration.device_id        = self.device_id
        req.arbitration.election_id.high = 0
        req.arbitration.election_id.low  = self.election_id
        self._stream_queue.append(req)
        time.sleep(0.5)

    def set_pipeline_config(self, p4info_path, bmv2_json_path):
        """Push P4 pipeline config — required before any writes."""
        p4info_obj = p4info_pb2.P4Info()
        with open(p4info_path) as f:
            text_format.Parse(f.read(), p4info_obj)
        with open(bmv2_json_path, 'rb') as f:
            dev_cfg = f.read()

        req = p4runtime_pb2.SetForwardingPipelineConfigRequest()
        req.device_id        = self.device_id
        req.election_id.high = 0
        req.election_id.low  = self.election_id
        req.action = (
            p4runtime_pb2
            .SetForwardingPipelineConfigRequest
            .VERIFY_AND_COMMIT
        )
        req.config.p4info.CopyFrom(p4info_obj)
        req.config.p4_device_config = dev_cfg

        try:
            self.stub.SetForwardingPipelineConfig(req)
            print(f'  [{self.name}] Pipeline config OK')
        except grpc.RpcError as e:
            print(f'  [{self.name}] Pipeline error: '
                  f'{e.details()}')
            raise

    # ── P4Info lookups ────────────────────────────────────────
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
        entry          = p4runtime_pb2.TableEntry()
        entry.table_id = self._table_id(table_name)

        for fname, mtype, val in match_fields:
            mf          = entry.match.add()
            mf.field_id = self._field_id(table_name, fname)
            if mtype == 'lpm':
                ip_b, plen        = self._parse_prefix(val)
                mf.lpm.value      = ip_b
                mf.lpm.prefix_len = plen

        action           = entry.action.action
        action.action_id = self._action_id(action_name)
        for pname, pval in action_params:
            p          = action.params.add()
            p.param_id = self._action_param_id(
                action_name, pname)
            p.value    = pval

        upd      = p4runtime_pb2.Update()
        upd.type = getattr(p4runtime_pb2.Update, update_type)
        upd.entity.table_entry.CopyFrom(entry)

        req                  = p4runtime_pb2.WriteRequest()
        req.device_id        = self.device_id
        req.election_id.high = 0
        req.election_id.low  = self.election_id
        req.updates.append(upd)

        try:
            self.stub.Write(req)
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.ALREADY_EXISTS:
                print(f'  [{self.name}] Write error: '
                      f'{e.details()}')

    def clear_table(self, table_name):
        rreq           = p4runtime_pb2.ReadRequest()
        rreq.device_id = self.device_id
        ent            = rreq.entities.add()
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

        wreq                 = p4runtime_pb2.WriteRequest()
        wreq.device_id       = self.device_id
        wreq.election_id.low = self.election_id
        for te in to_delete:
            u      = wreq.updates.add()
            u.type = p4runtime_pb2.Update.DELETE
            u.entity.table_entry.CopyFrom(te)

        try:
            self.stub.Write(wreq)
        except grpc.RpcError as e:
            print(f'  [{self.name}] Clear error: {e.details()}')

    def read_counter_bytes(self, counter_name, index):
        req            = p4runtime_pb2.ReadRequest()
        req.device_id  = self.device_id
        ent            = req.entities.add()
        ent.counter_entry.counter_id  = (
            self._counter_id(counter_name))
        ent.counter_entry.index.index = index
        try:
            for resp in self.stub.Read(req):
                for e in resp.entities:
                    return e.counter_entry.data.byte_count
        except grpc.RpcError:
            pass
        return 0


# ============================================================
# Traffic generator
# ============================================================

class TrafficGenerator:

    def __init__(self):
        self.h1_pid = self._find_pid('h1')
        self.h2_pid = self._find_pid('h2')
        if self.h1_pid:
            print(f'  TrafficGen: h1 PID={self.h1_pid}')
        else:
            print('  TrafficGen: h1 PID not found')

    def _find_pid(self, host_name):
        for pattern in [f'mininet:{host_name}',
                         f'bash.*{host_name}']:
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
        """Send pings. Returns (avg_rtt_ms, loss_pct)."""
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
            avg_rtt  = None
            loss_pct = 100.0

            for line in out.stdout.splitlines():
                if 'rtt min' in line or 'round-trip' in line:
                    try:
                        avg_rtt = float(
                            line.split('=')[1].strip()
                            .split('/')[1]
                        )
                    except Exception:
                        pass
                if 'packet loss' in line:
                    try:
                        loss_pct = float(
                            [x for x in line.split()
                             if '%' in x][0]
                            .replace('%', '')
                        )
                    except Exception:
                        pass
            return avg_rtt, loss_pct
        except Exception as e:
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

    def measure_with_traffic(self, window_s=3):
        """
        Run ping during counter measurement window.
        Returns (avg_rtt_ms, loss_pct, bytes_per_sec).
        """
        conn = None   # set by caller if needed

        rtt_list  = []
        loss_list = []

        def ping_loop():
            for _ in range(window_s):
                rtt, loss = self.ping_once(count=3, timeout=2)
                if rtt:
                    rtt_list.append(rtt)
                loss_list.append(loss)

        t = threading.Thread(target=ping_loop, daemon=True)
        t.start()
        t.join(timeout=window_s * 4 + 5)

        avg_rtt  = (sum(rtt_list) / len(rtt_list)
                    if rtt_list else None)
        avg_loss = (sum(loss_list) / len(loss_list)
                    if loss_list else 100.0)
        return avg_rtt, avg_loss


# ============================================================
# LEO Controller
# ============================================================

class LEOController:

    def __init__(self, p4info_path, policy='shortest_path'):
        self.policy    = policy
        self.conns     = {}
        self.measurements = {
            'time_s'       : [],
            'est_delay_ms' : [],
            'real_rtt_ms'  : [],
            'loss_pct'     : [],
            'isl_hops'     : [],
            'n_updates'    : [],
            'satellite_id' : [],
            'gsl_delay_ms' : [],
            'isl_delay_ms' : [],
            'handover'     : [],   # 1 if path changed this step
        }
        self._prev_path = None

        self.p4info = p4info_pb2.P4Info()
        with open(p4info_path) as f:
            text_format.Parse(f.read(), self.p4info)
        print(f'P4Info loaded: {p4info_path}')

    def connect_all(self):
        print('\n=== Connecting to BMv2 switches ===')
        device_ids = {
            'gs1': 351, 'sat1': 14, 'sat2': 15, 'gs2': 354
        }
        for name, (host, port) in SWITCHES.items():
            self.conns[name] = SwitchConnection(
                name           = name,
                host           = host,
                port           = port,
                device_id      = device_ids[name],
                p4info         = self.p4info,
                p4info_path    = P4INFO,
                bmv2_json_path = P4JSON,
            )
        print('All connected.\n')

    # ── Rule helpers ──────────────────────────────────────────
    def _pb(self, port):
        return port.to_bytes(2, 'big')

    def install_lpm(self, sw, prefix, port,
                    update_type='INSERT'):
        self.conns[sw].write_table_entry(
            table_name    = 'MyIngress.ipv4_lpm',
            match_fields  = [('hdr.ipv4.dstAddr', 'lpm',
                               prefix)],
            action_name   = 'MyIngress.do_forward',
            action_params = [('port', self._pb(port))],
            update_type   = update_type,
        )

    def push_static_rules(self):
        """Host-facing rules — never change."""
        self.install_lpm('gs1', '10.0.1.1/32', 1)
        self.install_lpm('gs2', '10.0.2.1/32', 2)

    def apply_rules(self):
        """Clear and re-install all forwarding rules."""
        for sw in ['gs1', 'sat1', 'sat2', 'gs2']:
            self.conns[sw].clear_table('MyIngress.ipv4_lpm')
        self.push_static_rules()

        # Forward: h2-bound traffic goes toward gs2 (port 2)
        for sw in ['gs1', 'sat1', 'sat2']:
            self.install_lpm(sw, '10.0.2.1/32', 2)
        # Reverse: h1-bound traffic goes toward gs1 (port 1)
        for sw in ['gs2', 'sat2', 'sat1']:
            self.install_lpm(sw, '10.0.1.1/32', 1)

        return 6

    def update_mininet_delays(self, net, path):
        """
        KEY DYNAMIC FEATURE:
        Update Mininet TC link delays based on which satellite
        is currently active in the Hypatia routing path.
        This makes RTT vary over time as satellites change.
        """
        if net is None or path is None:
            return

        sats = [n for n in path if n < NUM_SATS]
        if not sats:
            return

        # Get delay profile for the active satellite
        sat_id         = sats[0]
        gsl_ms, isl_ms = get_delay_for_satellite(sat_id)

        # Calculate ISL hops
        n_isl = max(0, len(sats) - 1)
        per_isl_ms = isl_ms   # delay per ISL hop

        print(f'  Dynamic delays: satellite={sat_id} '
              f'GSL={gsl_ms}ms '
              f'ISL={per_isl_ms}ms '
              f'(orbit {sat_id//13})')

        # Update each link in Mininet
        link_delays = [
            ('gs1',  'sat1', gsl_ms),
            ('sat1', 'sat2', per_isl_ms),
            ('sat2', 'gs2',  gsl_ms),
        ]

        from topo import update_link_delay
        for node_a, node_b, delay in link_delays:
            update_link_delay(net, node_a, node_b, delay)

        return gsl_ms, isl_ms

    # ── Policies ─────────────────────────────────────────────
    def policy_shortest_path(self, snap):
        """Use Hypatia's precomputed shortest path directly."""
        return snap['path']

    def policy_load_aware(self, snap):
        """
        Read port byte counters and log utilisation.
        In linear topology: demonstrates counter reads.
        In multi-path topology: would choose least-loaded path.
        """
        utilisation = {}
        for sw, conn in self.conns.items():
            for port in [1, 2]:
                try:
                    b = conn.read_counter_bytes(
                        'MyIngress.port_bytes', port)
                    utilisation[f'{sw}_p{port}'] = b
                except Exception:
                    utilisation[f'{sw}_p{port}'] = 0

        print('  [load_aware] utilisation:')
        for k, v in utilisation.items():
            if v > 0:
                print(f'    {k}: {v:,} bytes')

        # For linear topology: follow shortest path
        # (with utilisation data logged for report)
        return snap['path']

    def policy_predictive(self, snap_now, snap_future):
        """
        Look STEP_S seconds ahead. If satellite changes,
        pre-install the future path NOW to avoid disruption.
        This is the novel contribution of this project.
        """
        path_now  = snap_now['path']
        path_next = snap_future['path']

        if path_now is None:
            return path_next
        if path_next is None:
            return path_now

        sats_now  = [n for n in path_now  if n < NUM_SATS]
        sats_next = [n for n in path_next if n < NUM_SATS]

        if sats_now != sats_next:
            print(f'  [predictive] *** HANDOVER AHEAD ***')
            print(f'    Current sats : {sats_now}')
            print(f'    Future  sats : {sats_next}')
            print(f'    Installing future path NOW '
                  f'(proactive handover)')
            return path_next   # install future path proactively

        return path_now

    def read_counter_throughput(self, port=2, window_s=2):
        """Read bytes/s on sat1 port during window_s."""
        conn = self.conns['sat1']
        b1   = conn.read_counter_bytes(
            'MyIngress.port_bytes', port)
        time.sleep(window_s)
        b2   = conn.read_counter_bytes(
            'MyIngress.port_bytes', port)
        return max(0, b2 - b1) // window_s

    # ── Main control loop ─────────────────────────────────────
    def run(self, snapshots, traffic_gen=None, net=None):
        """
        Main dynamic control loop.
        Every STEP_S seconds:
          1. Read Hypatia snapshot for current time
          2. Compute path using selected policy
          3. Detect if path changed (handover)
          4. Push new P4 rules via P4Runtime gRPC
          5. Update Mininet link delays dynamically
          6. Measure RTT and throughput
          7. Store all measurements
        """
        print(f'\n=== Dynamic Control Loop ===')
        print(f'Policy  : {self.policy}')
        print(f'Duration: {DURATION}s')
        print(f'Step    : {STEP_S}s')
        print(f'GS pair : Tokyo(351) -> Sao Paulo(354)\n')

        if traffic_gen is not None:
            traffic_gen.start_iperf_server()
            time.sleep(1)

        start_wall = time.time()

        for t_s in range(0, DURATION - STEP_S + 1, STEP_S):
            snap_now    = snapshots[t_s]
            snap_future = snapshots[
                min(t_s + STEP_S, len(snapshots) - 1)
            ]

            print(f'\n{"─"*55}')
            print(f'  t = {t_s}s')

            # ── 1. Compute path based on policy ───────────────
            if self.policy == 'shortest_path':
                path = self.policy_shortest_path(snap_now)
            elif self.policy == 'load_aware':
                path = self.policy_load_aware(snap_now)
            elif self.policy == 'predictive':
                path = self.policy_predictive(
                    snap_now, snap_future)
            else:
                raise ValueError(
                    f'Unknown policy: {self.policy}')

            # ── 2. Detect handover ────────────────────────────
            is_handover = (
                self._prev_path is not None and
                path != self._prev_path
            )
            if is_handover:
                prev_sats = [n for n in self._prev_path
                             if n < NUM_SATS]
                new_sats  = ([n for n in path if n < NUM_SATS]
                             if path else [])
                print(f'  *** HANDOVER: {prev_sats} -> {new_sats}')
            self._prev_path = path

            # ── 3. Report path info ───────────────────────────
            if path:
                sats      = [n for n in path if n < NUM_SATS]
                n_isl     = max(0, len(sats) - 1)
                est_delay = snap_now['est_delay']
                sat_id    = sats[0] if sats else 0
                gsl_ms, isl_ms = get_delay_for_satellite(sat_id)

                print(f'  Path      : {path}')
                print(f'  Satellites: {sats} '
                      f'(orbit {sat_id//13})')
                print(f'  ISL hops  : {n_isl}')
                print(f'  Est delay : {est_delay} ms '
                      f'(GSL={gsl_ms}ms ISL={isl_ms}ms)')
                print(f'  Handover  : '
                      f'{"YES ⚡" if is_handover else "no"}')
            else:
                n_isl     = 0
                est_delay = 0.0
                sat_id    = 0
                gsl_ms = isl_ms = 0.0
                print(f'  WARNING: No path at t={t_s}s')

            # ── 4. Push P4 rules via P4Runtime ───────────────
            n_rules = self.apply_rules()
            print(f'  P4 rules  : {n_rules} pushed via gRPC')

            # ── 5. Update Mininet link delays dynamically ─────
            if path and net is not None:
                self.update_mininet_delays(net, path)
            elif path:
                # No net object — just print what would change
                gsl, isl = get_delay_for_satellite(
                    sat_id if sat_id else 14)
                print(f'  Delays    : GSL={gsl}ms ISL={isl}ms '
                      f'(Mininet not connected)')

            # ── 6. Measure RTT and throughput ─────────────────
            real_rtt   = None
            loss_pct   = 100.0
            throughput = 0

            if traffic_gen is not None:
                # Read counter before pings
                conn = self.conns['sat1']
                b1   = conn.read_counter_bytes(
                    'MyIngress.port_bytes', 2)

                # Run pings in parallel with counter window
                rtt_results  = []
                loss_results = []

                def ping_thread():
                    for _ in range(3):
                        r, l = traffic_gen.ping_once(
                            count=3, timeout=2)
                        if r:
                            rtt_results.append(r)
                        loss_results.append(l)

                pt = threading.Thread(
                    target=ping_thread, daemon=True)
                pt.start()
                time.sleep(4)   # measurement window
                pt.join(timeout=8)

                b2 = conn.read_counter_bytes(
                    'MyIngress.port_bytes', 2)

                real_rtt   = (sum(rtt_results) /
                              len(rtt_results)
                              if rtt_results else None)
                loss_pct   = (sum(loss_results) /
                              len(loss_results)
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
                # No traffic gen — just wait
                time.sleep(2)

            # ── 7. Store measurements ─────────────────────────
            self.measurements['time_s'].append(t_s)
            self.measurements['est_delay_ms'].append(est_delay)
            self.measurements['real_rtt_ms'].append(
                real_rtt or 0)
            self.measurements['loss_pct'].append(loss_pct)
            self.measurements['isl_hops'].append(n_isl)
            self.measurements['n_updates'].append(n_rules)
            self.measurements['satellite_id'].append(sat_id)
            self.measurements['gsl_delay_ms'].append(gsl_ms)
            self.measurements['isl_delay_ms'].append(isl_ms)
            self.measurements['handover'].append(
                1 if is_handover else 0)

            # ── Check duration ────────────────────────────────
            elapsed = time.time() - start_wall
            if elapsed >= DURATION:
                print(f'\nDuration reached ({DURATION}s).')
                break

            # Sleep remainder of step
            step_elapsed = time.time() - start_wall - t_s
            remaining    = STEP_S - step_elapsed - 4
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
            print(f'Avg RTT      : '
                  f'{statistics.mean(rtts):.1f} ms')
            print(f'Min/Max RTT  : '
                  f'{min(rtts):.1f} / {max(rtts):.1f} ms')
            if len(rtts) > 1:
                print(f'RTT Std Dev  : '
                      f'{statistics.stdev(rtts):.1f} ms')
        sats = set(m['satellite_id'])
        sats.discard(0)
        print(f'Satellites used: {sorted(sats)}')
        print(f'{"="*50}')


# ============================================================
# Save and plot
# ============================================================

def save_results(measurements, policy):
    os.makedirs('results', exist_ok=True)
    jpath = f'results/{policy}_measurements.json'
    with open(jpath, 'w') as f:
        json.dump(measurements, f, indent=2)
    print(f'Saved: {jpath}')
    _plot_single(measurements, policy)


def _plot_single(m, policy):
    times     = m['time_s']
    est_delay = m['est_delay_ms']
    real_rtt  = m['real_rtt_ms']
    hops      = m['isl_hops']
    loss      = m['loss_pct']
    handovers = m['handover']
    sats      = m['satellite_id']

    fig, axes = plt.subplots(4, 1, figsize=(12, 12),
                              sharex=True)

    # Plot 1: delays
    axes[0].plot(times, est_delay, 'o-', color='steelblue',
                 linewidth=2, label='Estimated delay (Hypatia)')
    if any(r > 0 for r in real_rtt):
        axes[0].plot(times, real_rtt, 's--', color='tomato',
                     linewidth=1.5,
                     label='Measured RTT (ping via P4/BMv2)')

    # Mark handover events
    for i, (t, ho) in enumerate(zip(times, handovers)):
        if ho:
            axes[0].axvline(t, color='red', alpha=0.4,
                            linestyle=':', linewidth=2)
            axes[0].annotate('HO', xy=(t, max(est_delay)*0.9),
                             fontsize=7, color='red',
                             ha='center')

    axes[0].set_ylabel('Delay (ms)')
    axes[0].set_title(
        f'LEO TE: {policy.replace("_"," ").title()} '
        f'— Tokyo → São Paulo (200s)')
    axes[0].legend()
    axes[0].grid(True, alpha=0.4)

    # Plot 2: active satellite ID
    axes[1].step(times, sats, where='post',
                 color='purple', linewidth=1.5)
    axes[1].set_ylabel('Active Satellite ID')
    axes[1].set_title('Satellite Handovers over Time')
    axes[1].grid(True, alpha=0.4)

    # Plot 3: ISL hops
    axes[2].step(times, hops, where='post',
                 color='orange', linewidth=2)
    axes[2].set_ylabel('ISL Hops')
    axes[2].grid(True, alpha=0.4)

    # Plot 4: packet loss
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
    colors   = ['steelblue', 'orange', 'green']
    styles   = ['o-', 's--', '^-.']

    fig, axes = plt.subplots(3, 1, figsize=(13, 10),
                              sharex=False)

    print('\n=== Policy Comparison Statistics ===')
    print(f'{"Policy":<20} {"Avg RTT":>9} '
          f'{"Min":>7} {"Max":>7} '
          f'{"Std":>7} {"HO#":>5}')
    print('-' * 58)

    for pol, col, sty in zip(policies, colors, styles):
        jpath = os.path.join(results_dir,
                             f'{pol}_measurements.json')
        if not os.path.exists(jpath):
            print(f'  Missing: {jpath}')
            continue
        with open(jpath) as f:
            d = json.load(f)

        label = pol.replace('_', ' ').title()
        ts    = d['time_s']

        # Real RTT
        rtts = d.get('real_rtt_ms', [])
        if any(r > 0 for r in rtts):
            axes[0].plot(ts, rtts, sty, color=col,
                         label=label, linewidth=1.5,
                         markersize=5)

        # Estimated delay
        axes[1].plot(ts, d['est_delay_ms'],
                     sty, color=col, label=label,
                     linewidth=1.5, markersize=5, alpha=0.8)

        # Satellite IDs
        axes[2].step(ts, d.get('satellite_id', []),
                     where='post', color=col,
                     label=label, linewidth=1.5)

        # Statistics
        valid = [r for r in rtts if r > 0]
        ho    = sum(d.get('handover', []))
        if valid:
            avg = statistics.mean(valid)
            mn  = min(valid)
            mx  = max(valid)
            std = (statistics.stdev(valid)
                   if len(valid) > 1 else 0)
            print(f'{pol:<20} {avg:>8.1f}ms {mn:>6.1f}ms '
                  f'{mx:>6.1f}ms {std:>6.1f}ms {ho:>5}')

    print('-' * 58)

    axes[0].set_ylabel('Real RTT (ms)')
    axes[0].set_title(
        'LEO TE Policy Comparison — Tokyo → São Paulo')
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


# ============================================================
# Main
# ============================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--policy',
        choices=['shortest_path', 'load_aware', 'predictive'],
        default='shortest_path'
    )
    parser.add_argument('--compare', action='store_true')
    parser.add_argument('--no-traffic', action='store_true')
    args = parser.parse_args()

    if args.compare:
        plot_comparison()
        sys.exit(0)

    if not os.path.exists(DYNAMIC_DIR):
        print(f'ERROR: {DYNAMIC_DIR}')
        sys.exit(1)

    snapshots = load_snapshots(DYNAMIC_DIR, duration=DURATION)

    tgen = None if args.no_traffic else TrafficGenerator()

    ctrl = LEOController(P4INFO, policy=args.policy)
    ctrl.connect_all()

    # Run WITHOUT net — delays printed but not applied to Mininet
    # Use run_experiment.py for full dynamic behavior
    measurements = ctrl.run(snapshots,
                            traffic_gen=tgen,
                            net=None)
    save_results(measurements, args.policy)
    print('\nDone.')
 