#!/usr/bin/env python3
# hypatia_driver.py
# Reads Hypatia Telesat output and builds networkx topology snapshots
# File format confirmed from actual generated data

import os
import sys
import math
import networkx as nx
import matplotlib
matplotlib.use('Agg')   # non-interactive backend, works without display
import matplotlib.pyplot as plt

# ── Configuration (confirmed paths) ─────────────────────────
BASE_DIR = (
    "/home/p4/Naveen/SDN_courseProject/hypatia/paper/"
    "satellite_networks_state/gen_data/"
    "telesat_1015_isls_plus_grid_ground_stations_top_100"
    "_algorithm_free_one_only_over_isls"
)
DYNAMIC_DIR  = os.path.join(BASE_DIR, "dynamic_state_1000ms_for_200s")
GS_FILE      = os.path.join(BASE_DIR, "ground_stations.txt")
ISLS_FILE    = os.path.join(BASE_DIR, "isls.txt")
DESC_FILE    = os.path.join(BASE_DIR, "description.txt")
TLES_FILE    = os.path.join(BASE_DIR, "tles.txt")

NUM_ORBITS   = 27
SATS_PER_ORB = 13
NUM_SATS     = NUM_ORBITS * SATS_PER_ORB  # = 351
NUM_GS       = 100

# Speed of light in m/ms (for delay calculation)
SPEED_OF_LIGHT_M_PER_MS = 299792.458

# Time step in nanoseconds
STEP_NS   = 1_000_000_000  # 1 second
DURATION  = 200            # total seconds

# ── Ground stations we care about (small subset for project) ─
# Using top 5 by population from ground_stations.txt
# Tokyo=0, Delhi=1, Shanghai=2, Sao Paulo=3, Mumbai=4
OUR_GS_INDICES = [0, 1, 2, 3, 4]

# ── Parse description.txt for max distances ──────────────────
def load_description(desc_file):
    desc = {}
    if not os.path.exists(desc_file):
        desc['max_gsl_m'] = 5845152.0
        desc['max_isl_m'] = 7197482.0
        return desc
    with open(desc_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith('max_gsl_length_m'):
                desc['max_gsl_m'] = float(line.split('=')[1])
            elif line.startswith('max_isl_length_m'):
                desc['max_isl_m'] = float(line.split('=')[1])
    return desc

# ── Parse ground_stations.txt ────────────────────────────────
def load_ground_stations(gs_file):
    """
    Returns dict: {gs_index: {'name': str, 'x': float, 'y': float, 'z': float}}
    Format: index,name,lat,lon,alt,x,y,z
    """
    gs_info = {}
    if not os.path.exists(gs_file):
        print(f"ERROR: {gs_file} not found")
        return gs_info
    with open(gs_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(',')
            if len(parts) < 8:
                continue
            idx  = int(parts[0])
            name = parts[1]
            lat  = float(parts[2])
            lon  = float(parts[3])
            x    = float(parts[5])
            y    = float(parts[6])
            z    = float(parts[7])
            gs_info[idx] = {
                'name': name, 'lat': lat, 'lon': lon,
                'x': x, 'y': y, 'z': z
            }
    print(f"Loaded {len(gs_info)} ground stations")
    return gs_info

# ── Parse isls.txt ───────────────────────────────────────────
def load_isls(isls_file):
    """
    Returns list of (sat_a, sat_b) tuples — static ISL pairs.
    Format: sat_a sat_b (one pair per line)
    """
    isls = []
    if not os.path.exists(isls_file):
        print(f"WARNING: {isls_file} not found")
        return isls
    with open(isls_file) as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) == 2:
                isls.append((int(parts[0]), int(parts[1])))
    print(f"Loaded {len(isls)} ISLs")
    return isls

# ── Distance between two ECEF points (metres) ───────────────
def ecef_distance_m(x1, y1, z1, x2, y2, z2):
    return math.sqrt((x2-x1)**2 + (y2-y1)**2 + (z2-z1)**2)

# ── Delay from distance (ms) ─────────────────────────────────
def delay_ms(distance_m):
    return distance_m / SPEED_OF_LIGHT_M_PER_MS

# ── Parse one fstate file ────────────────────────────────────
def parse_fstate(fstate_path):
    """
    Parses one fstate_*.txt file.

    File format (5 columns):
        current_node_id, dst_node_id, next_hop_node_id, num_hops, num_isl_hops

    Returns:
        fwd_table: dict  {current_node: {dst_node: next_hop_node}}
        active_edges: set of (current_node, next_hop_node) tuples
    """
    fwd_table    = {}   # {src: {dst: next_hop}}
    active_edges = set()

    if not os.path.exists(fstate_path):
        return fwd_table, active_edges

    with open(fstate_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(',')
            if len(parts) < 3:
                continue

            current  = int(parts[0])
            dst      = int(parts[1])
            next_hop = int(parts[2])
            # parts[3] = num_hops, parts[4] = num_isl_hops (not used here)

            # Build forwarding table
            if current not in fwd_table:
                fwd_table[current] = {}
            fwd_table[current][dst] = next_hop

            # Record this as an active edge
            active_edges.add((current, next_hop))

    return fwd_table, active_edges

# ── Build networkx graph for one snapshot ───────────────────
def build_graph(fwd_table, active_edges, isls,
                gs_info, our_gs_indices,
                desc, num_sats=NUM_SATS):
    """
    Builds a networkx DiGraph for one time step.

    Nodes: satellites (0..350) and our selected ground stations
    Edges: ISLs + GSLs, weighted by propagation delay in ms

    We estimate delays using:
      ISL delay  = 50% of max ISL distance (average assumption)
      GSL delay  = 50% of max GSL distance (average assumption)
    These are conservative estimates since actual positions
    change per time step. For more accuracy, integrate sgp4.
    """
    G = nx.DiGraph()

    # Average delays (ms) based on max distances in description.txt
    avg_isl_delay = delay_ms(desc['max_isl_m'] * 0.5)
    avg_gsl_delay = delay_ms(desc['max_gsl_m'] * 0.5)

    our_gs_node_ids = {
        num_sats + gs_idx
        for gs_idx in our_gs_indices
    }

    # Add our ground station nodes
    for gs_idx in our_gs_indices:
        node_id = num_sats + gs_idx
        name    = gs_info.get(gs_idx, {}).get('name', f'GS{gs_idx}')
        G.add_node(node_id,
                   node_type='ground_station',
                   gs_index=gs_idx,
                   label=name)

    # Add ISL edges between satellites
    # Only add satellites that appear in active edges to keep graph small
    active_sats = set()
    for (src, dst) in active_edges:
        if src < num_sats:
            active_sats.add(src)
        if dst < num_sats:
            active_sats.add(dst)

    for (sat_a, sat_b) in isls:
        if (sat_a, sat_b) in active_edges or (sat_b, sat_a) in active_edges:
            if sat_a not in G:
                G.add_node(sat_a, node_type='satellite', label=f'SAT{sat_a}')
            if sat_b not in G:
                G.add_node(sat_b, node_type='satellite', label=f'SAT{sat_b}')
            # Bidirectional ISL
            G.add_edge(sat_a, sat_b,
                       link_type='ISL', delay_ms=avg_isl_delay)
            G.add_edge(sat_b, sat_a,
                       link_type='ISL', delay_ms=avg_isl_delay)

    # Add GSL edges (ground station <-> satellite)
    # Derive from fwd_table: if a GS forwards to a satellite,
    # that satellite is the uplink satellite for this GS
    for gs_node_id in our_gs_node_ids:
        if gs_node_id in fwd_table:
            for dst, next_hop in fwd_table[gs_node_id].items():
                if next_hop < num_sats:  # next hop is a satellite
                    if next_hop not in G:
                        G.add_node(next_hop, node_type='satellite',
                                   label=f'SAT{next_hop}')
                    G.add_edge(gs_node_id, next_hop,
                               link_type='GSL', delay_ms=avg_gsl_delay)
                    G.add_edge(next_hop, gs_node_id,
                               link_type='GSL', delay_ms=avg_gsl_delay)
                    break  # one uplink sat per GS is enough

    return G

# ── Main: load all snapshots ─────────────────────────────────
def load_all_snapshots():
    """
    Returns list of snapshot dicts, one per second:
    {
      'time_s'      : int,
      'graph'       : nx.DiGraph,
      'fwd_table'   : {current_node: {dst_node: next_hop}},
      'gs_node_ids' : {node_id: gs_index}
    }
    """
    print("=== Loading Hypatia Telesat topology snapshots ===\n")

    # Load static files
    desc    = load_description(DESC_FILE)
    gs_info = load_ground_stations(GS_FILE)
    isls    = load_isls(ISLS_FILE)

    print(f"\nMax ISL delay (avg): "
          f"{delay_ms(desc['max_isl_m']*0.5):.1f} ms")
    print(f"Max GSL delay (avg): "
          f"{delay_ms(desc['max_gsl_m']*0.5):.1f} ms")

    gs_node_ids = {
        NUM_SATS + gs_idx: gs_idx
        for gs_idx in OUR_GS_INDICES
    }

    snapshots = []

    for t_s in range(DURATION):
        timestamp_ns = t_s * STEP_NS
        fstate_file  = os.path.join(
            DYNAMIC_DIR, f"fstate_{timestamp_ns}.txt"
        )

        fwd_table, active_edges = parse_fstate(fstate_file)
        G = build_graph(fwd_table, active_edges, isls,
                        gs_info, OUR_GS_INDICES, desc)

        snapshots.append({
            'time_s'      : t_s,
            'graph'       : G,
            'fwd_table'   : fwd_table,
            'gs_node_ids' : gs_node_ids,
        })

        if t_s % 25 == 0:
            print(f"  t={t_s:3d}s -> "
                  f"{G.number_of_nodes()} nodes, "
                  f"{G.number_of_edges()} edges")

    print(f"\nLoaded {len(snapshots)} snapshots total.")
    return snapshots, gs_info

# ── Verification: test routing between GS pairs ──────────────
def verify_routing(snapshots, gs_info):
    print("\n=== Verifying routing at t=0 ===")
    snap      = snapshots[0]
    G         = snap['graph']
    fwd_table = snap['fwd_table']
    gs_ids    = list(snap['gs_node_ids'].keys())

    print(f"Graph: {G.number_of_nodes()} nodes, "
          f"{G.number_of_edges()} edges")
    print(f"Our GS node IDs: {gs_ids}")

    # Test path between first two ground stations
    if len(gs_ids) >= 2:
        src = gs_ids[0]
        dst = gs_ids[1]
        src_name = gs_info.get(src - NUM_SATS, {}).get('name', f'GS{src}')
        dst_name = gs_info.get(dst - NUM_SATS, {}).get('name', f'GS{dst}')

        try:
            path = nx.shortest_path(G, src, dst, weight='delay_ms')
            total_delay = sum(
                G[path[i]][path[i+1]]['delay_ms']
                for i in range(len(path)-1)
            )
            print(f"\nPath {src_name}(node {src}) -> "
                  f"{dst_name}(node {dst}):")
            print(f"  Hops: {path}")
            print(f"  Total delay: {total_delay:.1f} ms")
        except (nx.NetworkXNoPath, nx.NodeNotFound) as e:
            print(f"No graph path found: {e}")

        # Also show from forwarding table directly
        print(f"\nForwarding table entry for node {src}:")
        if src in fwd_table and dst in fwd_table[src]:
            print(f"  Next hop to {dst}: {fwd_table[src][dst]}")
        else:
            print(f"  No direct entry (may route through intermediate)")

# ── Plot delay over time between two GS ──────────────────────
def plot_delay_over_time(snapshots, gs_info,
                         src_gs_idx=0, dst_gs_idx=1):
    src_node = NUM_SATS + src_gs_idx
    dst_node = NUM_SATS + dst_gs_idx
    src_name = gs_info.get(src_gs_idx, {}).get('name', f'GS{src_gs_idx}')
    dst_name = gs_info.get(dst_gs_idx, {}).get('name', f'GS{dst_gs_idx}')

    times  = []
    delays = []
    last_valid = 40.0

    for snap in snapshots:
        G = snap['graph']
        try:
            path = nx.shortest_path(G, src_node, dst_node,
                                    weight='delay_ms')
            total = sum(
                G[path[i]][path[i+1]]['delay_ms']
                for i in range(len(path)-1)
            )
            last_valid = total
            delays.append(total)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            delays.append(last_valid)   # hold last value during gaps
        times.append(snap['time_s'])

    plt.figure(figsize=(12, 4))
    plt.plot(times, delays, linewidth=1.5, color='steelblue')
    plt.xlabel('Time (s)')
    plt.ylabel('Path Delay (ms)')
    plt.title(f'Propagation delay: {src_name} -> {dst_name} over 200s')
    plt.grid(True, alpha=0.4)
    plt.tight_layout()
    plt.savefig('delay_over_time.png', dpi=120)
    print(f"\nSaved delay_over_time.png "
          f"({src_name} -> {dst_name})")

# ── Print summary table of all GS pairs at t=0 ───────────────
def print_gs_pair_summary(snapshots, gs_info):
    print("\n=== GS pair delays at t=0 ===")
    snap  = snapshots[0]
    G     = snap['graph']
    gs_ids = list(snap['gs_node_ids'].keys())

    print(f"{'Source':<20} {'Destination':<20} {'Delay(ms)':>12} {'Hops':>6}")
    print("-" * 62)
    for src in gs_ids:
        for dst in gs_ids:
            if src == dst:
                continue
            sname = gs_info.get(src-NUM_SATS, {}).get('name', f'N{src}')
            dname = gs_info.get(dst-NUM_SATS, {}).get('name', f'N{dst}')
            try:
                path  = nx.shortest_path(G, src, dst, weight='delay_ms')
                delay = sum(G[path[i]][path[i+1]]['delay_ms']
                            for i in range(len(path)-1))
                hops  = len(path) - 1
                print(f"{sname:<20} {dname:<20} {delay:>12.1f} {hops:>6}")
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                print(f"{sname:<20} {dname:<20} {'NO PATH':>12} {'-':>6}")

# ── Entry point ──────────────────────────────────────────────
if __name__ == '__main__':

    # Verify required directories exist
    for path, label in [(DYNAMIC_DIR, 'dynamic_state dir'),
                        (GS_FILE,     'ground_stations.txt'),
                        (ISLS_FILE,   'isls.txt')]:
        if not os.path.exists(path):
            print(f"ERROR: Cannot find {label}: {path}")
            sys.exit(1)

    # Count available fstate files
    available = [f for f in os.listdir(DYNAMIC_DIR)
                 if f.startswith('fstate_') and f.endswith('.txt')]
    print(f"Found {len(available)} fstate files in {DYNAMIC_DIR}")

    # Load everything
    snapshots, gs_info = load_all_snapshots()

    # Run verification checks
    verify_routing(snapshots, gs_info)
    print_gs_pair_summary(snapshots, gs_info)
    plot_delay_over_time(snapshots, gs_info, src_gs_idx=0, dst_gs_idx=1)

    print("\n=== Step 2 COMPLETE ===")
    print("snapshots list is ready to pass to the controller in Step 6.")
