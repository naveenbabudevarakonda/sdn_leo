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

# Import from our project files
from topo import (
    build_network,
    install_all_rules,
    verify_connectivity,
    THRIFT_PORTS,
)
from controller import (
    load_snapshots,
    LEOController,
    TrafficGenerator,
    save_results,
    plot_comparison,
    DYNAMIC_DIR,
    P4INFO,
    DURATION,
)


def run_single_policy(policy, no_traffic=False):
    """
    Build Mininet topology, connect controller,
    run dynamic experiment, save results.
    """
    print(f'\n{"="*60}')
    print(f'EXPERIMENT: {policy.upper()}')
    print(f'{"="*60}')
    print(f'Tokyo (GS351) -> Sao Paulo (GS354)')
    print(f'Duration: {DURATION}s, Dynamic delay updates: YES')
    print(f'{"="*60}\n')

    setLogLevel('warning')  # suppress Mininet noise

    # In run_single_policy(), before build_network()
    print('Cleaning up any previous Mininet state...')
    subprocess.run(['mn', '-c'],
                capture_output=True)
    subprocess.run(['pkill', '-f', 'simple_switch_grpc'],
                capture_output=True)
    time.sleep(2)

    # ── Step 1: Build and start Mininet ──────────────────────
    print('=== Building Mininet topology ===')
    net, switches = build_network()
    net.start()

    print('\nWaiting for BMv2 switches to initialise...')
    time.sleep(4)

    # Check all switches started
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

    # ── Step 2: Install initial rules via Thrift ──────────────
    install_all_rules(switches)
    time.sleep(1)

    # ── Step 3: Verify basic connectivity ─────────────────────
    loss = verify_connectivity(net)
    if loss > 0:
        print(f'\nWARNING: Initial connectivity check failed '
              f'({loss}% loss).')
        print('Continuing anyway — controller will '
              'install P4Runtime rules.\n')
    else:
        print('Connectivity OK.\n')

    # ── Step 4: Load Hypatia snapshots ────────────────────────
    print('=== Loading Hypatia topology snapshots ===')
    if not os.path.exists(DYNAMIC_DIR):
        print(f'ERROR: {DYNAMIC_DIR}')
        net.stop()
        sys.exit(1)

    snapshots = load_snapshots(DYNAMIC_DIR, duration=DURATION)

    # ── Step 5: Set up traffic generator ─────────────────────
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

    # ── Step 6: Connect P4Runtime controller ─────────────────
    ctrl = LEOController(P4INFO, policy=policy)
    ctrl.connect_all()

    # ── Step 7: Run dynamic control loop ──────────────────────
    # Pass `net` so controller can update link delays
    print('=== Starting dynamic control loop ===\n')
    measurements = ctrl.run(
        snapshots,
        traffic_gen=tgen,
        net=net          # ← THIS enables dynamic delay updates
    )

    # ── Step 8: Save results ──────────────────────────────────
    save_results(measurements, policy)

    # ── Step 9: Stop network ──────────────────────────────────
    print('\nStopping Mininet network...')
    net.stop()
    print('Network stopped.')

    return measurements


def run_all_policies():
    """Run all three policies back to back."""
    policies = ['shortest_path', 'load_aware', 'predictive']

    print('\n' + '='*60)
    print('RUNNING ALL THREE POLICIES')
    print('Total time: ~3 x 200s = ~10 minutes')
    print('='*60)

    for i, policy in enumerate(policies, 1):
        print(f'\n[{i}/3] Starting {policy}...')
        try:
            run_single_policy(policy)
        except Exception as e:
            print(f'ERROR in {policy}: {e}')
            print('Continuing to next policy...')
        finally:
            # Give system time to clean up between runs
            print('Waiting 10s before next experiment...')
            time.sleep(10)

    # Generate comparison plot
    print('\n=== Generating comparison plots ===')
    plot_comparison()
    print('\nAll experiments complete.')
    print('Results saved in results/')


# ============================================================
# Main
# ============================================================

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
    args = parser.parse_args()

    if args.compare:
        plot_comparison()
        sys.exit(0)

    if args.policy == 'all':
        run_all_policies()
    else:
        run_single_policy(args.policy, args.no_traffic)
