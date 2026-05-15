# SDN-Based Traffic Engineering over Low-Earth-Orbit Satellite Networks

## CS6045 SDN Course Project — IIT Madras

**Authors:** Anupam Chanda (CS25D013), Naveen Babu D (CS25S033)

**Repository:** https://github.com/naveenbabudevarakonda/sdn_leo

---

# Overview

This project implements **SDN-based traffic engineering** over **Low-Earth-Orbit (LEO) satellite networks** using:

- Hypatia-generated satellite topology snapshots
- BMv2 P4 software switches
- P4Runtime gRPC controller
- Mininet emulation
- Dynamic propagation delay updates

The system evaluates three routing policies:

1. **Shortest Path**
2. **Load Aware**
3. **Predictive Handover**

The experiment runs over a **200-second** satellite network simulation and performs:

- Dynamic forwarding rule updates
- Satellite handover detection
- Delay-aware traffic engineering
- RTT and throughput measurements

Observed results include:

- 4 satellite handovers detected
- 0% packet loss
- RTT range: **61.2–113.3 ms**
- 20 measurement steps per policy

---

# Repository Structure

```text
.
├── leo_switch.p4                # P4 program (data plane)
├── topo.py                      # Mininet topology + BMv2 launch
├── controller.py                # P4Runtime gRPC controller
├── run_experiment.py            # Run the complete experiment
├── hypatia_driver.py            # Standalone Hypatia parser
├── measure.py                   # RTT/iPerf3 evaluation
├── build/
│   ├── leo_switch.json          # Compiled BMv2 program (generated)
│   └── leo_switch.p4info.txt    # P4Runtime descriptors (generated)
├── results/
│   ├── shortest_path_measurements.json
│   ├── load_aware_measurements.json
│   ├── predictive_measurements.json
│   ├── shortest_path_results.png
│   ├── load_aware_results.png
│   ├── predictive_results.png
│   └── policy_comparison.png
├── README.md
└── output.txt                   # Sample output
```

---

# Dependencies

## System Packages

Install required system packages:

```bash
sudo apt-get update

sudo apt-get install -y \
    mininet \
    iperf3 \
    tcpdump \
    python3-pip \
    python3-venv
```

---

## BMv2 and p4c

Install BMv2 and the P4 compiler from the P4Lang repositories.

Verify installation:

```bash
which simple_switch
which simple_switch_grpc
which p4c
```

Expected:

```text
/usr/local/bin/simple_switch
/usr/local/bin/simple_switch_grpc
/usr/local/bin/p4c
```

Useful references:

- https://github.com/jafingerhut/p4-guide/blob/master/bin/README-install-troubleshooting.md
- https://github.com/p4lang/behavioral-model
- https://github.com/p4lang/p4c

---

## Python Packages

Install required Python dependencies:

```bash
pip install \
    grpcio \
    grpcio-tools \
    p4runtime \
    networkx \
    matplotlib \
    numpy
```

Verify:

```bash
python3 -c "import grpc; print('gRPC OK')"

python3 -c "import p4runtime_lib; print('P4Runtime OK')"

python3 -c "import mininet; print('Mininet OK')"
```

---

## Hypatia

Clone and install Hypatia:

```bash
git clone https://github.com/snkas/hypatia

cd hypatia/satgenpy

pip install -e .

pip install \
    numpy \
    astropy \
    ephem \
    networkx \
    sgp4 \
    geopy \
    matplotlib
```

---

# Generating Hypatia Topology Data

This project uses the **Telesat-1015** constellation:

- 27 orbits
- 13 satellites per orbit
- 351 satellites total
- 100 ground stations

Generate forwarding-state snapshots:

```bash
cd hypatia/paper/satellite_networks_state

sudo $(which python3) main_telesat.py \
    200 \
    1000 \
    isls_plus_grid \
    ground_stations_top_100 \
    algorithm_free_one_only_over_isls \
    1
```

Expected output directory:

```text
gen_data/
└── telesat_1015_isls_plus_grid_ground_stations_top_100
    └── _algorithm_free_one_only_over_isls/
        └── dynamic_state_1000ms_for_200s/
            ├── fstate_0.txt
            ├── fstate_1000000000.txt
            ├── fstate_2000000000.txt
            ├── ...
            ├── description.txt
            ├── ground_stations.txt
            ├── isls.txt
            └── tles.txt
```

---

## Forwarding-State File Format

```text
current_node, dst_node, next_hop, num_hops, isl_hops
```

### Node Numbering

- `0–350` → Satellites
- `351–450` → Ground stations

Ground stations used in this project:

- Tokyo → `351`
- São Paulo → `354`

---

# Compiling the P4 Program

Create the build directory:

```bash
mkdir -p build
```

Compile the P4 program:

```bash
p4c \
    --target bmv2 \
    --arch v1model \
    --p4runtime-files build/leo_switch.p4info.txt \
    -o build \
    leo_switch.p4
```

Expected output:

```text
build/leo_switch.json
build/leo_switch.p4info.txt
```

Notes:

- `PROTO_ICMP unused` warning is harmless
- `.txt` P4Info format deprecation warning can be ignored

---

# Running the Experiments

## Recommended: Run All Policies

```bash
sudo $(which python3) run_experiment.py --policy all
```

---

## Run Individual Policies

### Shortest Path

```bash
sudo $(which python3) run_experiment.py \
    --policy shortest_path
```

### Load Aware

```bash
sudo $(which python3) run_experiment.py \
    --policy load_aware
```

### Predictive

```bash
sudo $(which python3) run_experiment.py \
    --policy predictive
```

---

## Generate Comparison Plot

```bash
sudo $(which python3) run_experiment.py --compare
```

---

# Estimated Runtime

| Task | Time |
|---|---|
| Single policy | ~3.5 minutes |
| All three policies | ~12 minutes |

---

# Expected Runtime Output

## Example: shortest_path

```text
=== Loading Hypatia snapshots (cumulative) ===

t=0s:
  path=[351,42,41,40,39,354]
  sats=[42,41,40,39]
  delay~41.0ms

*** HANDOVER at t=20s:
  [42,41,40,39] -> [237,224,225,226]

Loaded 200 snapshots.
Total handovers detected: 4
```

---

## Example Measurement Output

```text
-- t=0s

Path      : [351, 42, 41, 40, 39, 354]
Satellites: [42, 41, 40, 39]
ISL hops  : 3
Est delay : 41.0 ms
Handover  : no

P4 rules  : 6 pushed via gRPC

Dynamic delays:
  satellite=42
  GSL=7.0ms
  ISL=9.0ms

Real RTT  : 82.9 ms
Loss      : 0%
Throughput: 220 bytes/s
```

---

# Final Experimental Results

| Policy | Avg RTT | Min RTT | Max RTT | Std Dev | Handovers | Loss |
|---|---|---|---|---|---|---|
| Shortest Path | 91.7 ms | 65.6 ms | 124.8 ms | 16.2 ms | 3 | 0% |
| Load Aware | 98.5 ms | 77.2 ms | 115.6 ms | 11.3 ms | 3 | 0% |
| Predictive | 101.2 ms | 83.3 ms | 131.3 ms | 11.8 ms | 4 | 0% |

---

# Generated Results

After successful completion:

```text
results/
├── shortest_path_measurements.json
├── load_aware_measurements.json
├── predictive_measurements.json
├── shortest_path_results.png
├── load_aware_results.png
├── predictive_results.png
└── policy_comparison.png
```

---

# Verification Checklist

After running all policies:

- [ ] All `*_measurements.json` files exist
- [ ] All `loss_pct` values are zero
- [ ] Satellite transitions appear correctly
- [ ] Delay values include:
  - 41.0 ms
  - 54.0 ms
  - 66.0 ms
- [ ] `policy_comparison.png` contains distinct policy behaviors
- [ ] Predictive policy triggers early handovers

---

# Troubleshooting

| Error | Fix |
|---|---|
| `Error creating interface pair: File exists` | Run `sudo mn -c` |
| `No forwarding pipeline config set` | Ensure `set_pipeline_config()` is called |
| `No forward path at this step` | Verify cumulative snapshot loading |
| `simple_switch_grpc not found` | Check BMv2 installation |
| Ping shows 100% loss | Verify `/16` subnet and static ARP entries |
| Counter always 0 | Ensure traffic exists during measurement |

---

# GitHub Repository

https://github.com/naveenbabudevarakonda/sdn_leo
