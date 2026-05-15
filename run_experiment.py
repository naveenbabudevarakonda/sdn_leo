#!/usr/bin/env python3
# run_experiment.py
# Runs topo + controller together in one process
# This gives the controller direct access to the Mininet
# net object so it can update link delays dynamically.
#
# Usage:
#   sudo $(which python3) run_experiment.py --policy shortest_path
#   sudo $(which python3) run_experiment.py --policy load_aware
#   sudo $(which python3) run_experiment.py --policy predictive
#   python3 run_experiment.py --compare

import os
import sys
import time
import argparse
import signal
import subprocess

from mininet.log import setLogLevel

from topo import (
    build_network,
    collect_neighbor_ports,
    initialize_link_states,
    install_all_rules,
    verify_connectivity,
)
from controller import (
    build_gs_context,
    load_snapshots,
    LEOController,
    TrafficGenerator,
    save_results,
    plot_comparison,
    DYNAMIC_DIR,
    P4INFO,
    DURATION,
    derive_potential_gsl_sat_ids,
    satellite_count_bounds,
)


def run_single_policy(policy, no_traffic=False,
                      src_gs=None, dst_gs=None):
    """
    Build Mininet topology, connect controller,
    run dynamic experiment, save results.
    """
    print(f'\n{"="*60}')
    print(f'EXPERIMENT: {policy.upper()}')
    print(f'{"="*60}')
    gs_ctx = build_gs_context(src_gs, dst_gs)
    gs_labels = gs_ctx['labels']
    gs_node_ids = gs_ctx['node_ids']

    print(f'{gs_labels["gs1"]} (GS{gs_node_ids["gs1"]}) '
          f'-> {gs_labels["gs2"]} (GS{gs_node_ids["gs2"]})')
    print(f'Duration: {DURATION}s, Dynamic delay updates: YES')
    print(f'{"="*60}\n')

    setLogLevel('warning')

    print('Cleaning up any previous Mininet state...')
    subprocess.run(['mn', '-c'], capture_output=True)
    subprocess.run(['pkill', '-f', 'simple_switch_grpc'],
                   capture_output=True)
    time.sleep(2)

    print('=== Loading Hypatia topology snapshots ===')
    if not os.path.exists(DYNAMIC_DIR):
        print(f'ERROR: {DYNAMIC_DIR}')
        sys.exit(1)

    snapshots = load_snapshots(
        DYNAMIC_DIR,
        duration=DURATION,
        gs_node_ids=gs_node_ids,
        gs_labels=gs_labels,
    )
    min_sats, max_sats = satellite_count_bounds(snapshots)
    potential_gsl_sat_ids = derive_potential_gsl_sat_ids(
        snapshots,
        gs_node_ids=gs_node_ids,
    )

    print('=== Building Mininet topology ===')
    print(f'Logical satellite-count range: '
          f'{min_sats}..{max_sats}')
    print(f'Ground stations               : '
          f'{gs_labels["gs1"]} -> {gs_labels["gs2"]}')
    print('Emulated satellite switches   : 351')
    print(f'Potential GSL satellites      : '
          f'{sorted(potential_gsl_sat_ids)}')

    net, switches = build_network(
        potential_gsl_sat_ids=potential_gsl_sat_ids,
        ground_node_ids=gs_node_ids,
        ground_labels=gs_labels,
    )
    net.start()

    print('\nWaiting for BMv2 switches to initialise...')
    time.sleep(4)

    all_ok = True
    for name, sw in switches.items():
        if not sw.is_running():
            print(f'ERROR: {name} failed to start. '
                  f'Check /tmp/bmv2_{name}.log')
            all_ok = False

    if not all_ok:
        net.stop()
        sys.exit(1)

    print('All switches running.\n')
    collect_neighbor_ports(net, switches)
    initialize_link_states(net)

    install_all_rules(switches)
    time.sleep(1)

    if no_traffic:
        tgen = None
        print('Traffic generation disabled (--no-traffic).\n')
    else:
        print('=== Setting up traffic generator ===')
        tgen = TrafficGenerator()
        if tgen.h1_pid is None:
            print('WARNING: Could not find h1 PID. '
                  'RTT measurement may fail.')
        print()

    ctrl = LEOController(
        P4INFO,
        policy=policy,
        switch_addresses={
            name: ('127.0.0.1', port)
            for name, port in net.leo_config['grpc_ports'].items()
        },
        switch_device_ids=net.leo_config['node_ids'],
        gs_node_ids=gs_node_ids,
        gs_labels=gs_labels,
    )
    ctrl.connect_all()

    initial_path = snapshots[0]['path']
    if initial_path:
        ctrl.apply_rules(initial_path,
                         net.leo_config['neighbor_ports'])
        ctrl.update_mininet_topology(net, initial_path)
        time.sleep(1)

    loss = verify_connectivity(net)
    if loss > 0:
        print(f'\nWARNING: Initial connectivity check failed '
              f'({loss}% loss).')
        print('Continuing anyway — dynamic control loop '
              'will keep updating the active path.\n')
    else:
        print('Connectivity OK.\n')

    print('=== Starting dynamic control loop ===\n')
    measurements = ctrl.run(
        snapshots,
        traffic_gen=tgen,
        net=net
    )

    save_results(measurements, policy, gs_labels=gs_labels)

    print('\nStopping Mininet network...')
    net.stop()
    print('Network stopped.')

    return measurements


def run_all_policies(src_gs=None, dst_gs=None):
    """Run all three policies back to back."""
    policies = ['shortest_path', 'load_aware', 'predictive']

    print('\n' + '='*60)
    print('RUNNING ALL THREE POLICIES')
    print('Total time: ~3 x 200s = ~10 minutes')
    print('='*60)

    for i, policy in enumerate(policies, 1):
        print(f'\n[{i}/3] Starting {policy}...')
        try:
            run_single_policy(policy, src_gs=src_gs,
                              dst_gs=dst_gs)
        except Exception as e:
            print(f'ERROR in {policy}: {e}')
            print('Continuing to next policy...')
        finally:
            print('Waiting 10s before next experiment...')
            time.sleep(10)

    print('\n=== Generating comparison plots ===')
    plot_comparison()
    print('\nAll experiments complete.')
    print('Results saved in results/')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=(
            'LEO SDN Dynamic Experiment Runner\n'
            'Combines Mininet topology + P4Runtime controller\n'
            'for fully dynamic satellite routing.'
        )
    )
    parser.add_argument(
        '--policy',
        choices=['shortest_path', 'load_aware',
                 'predictive', 'all'],
        default='shortest_path',
        help='TE policy to run (or "all" for all three)'
    )
    parser.add_argument(
        '--no-traffic',
        action='store_true',
        help='Disable ping traffic generation'
    )
    parser.add_argument(
        '--compare',
        action='store_true',
        help='Only generate comparison plot from saved results'
    )
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

    if args.policy == 'all':
        run_all_policies(args.src_gs, args.dst_gs)
    else:
        run_single_policy(args.policy, args.no_traffic,
                          args.src_gs, args.dst_gs)
