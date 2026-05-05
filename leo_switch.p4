/* leo_switch.p4
 * LEO Satellite SDN project
 * P4_16 / v1model for BMv2
 *
 * Features:
 *   1. IPv4 LPM forwarding table  (controller populates via P4Runtime)
 *   2. Per-port byte counters      (controller reads for load-aware TE)
 *   3. ISL path-trace header       (novel feature: tracks satellite hops)
 *   4. Digest to controller        (notifies on path-trace at egress GS)
 */

#include <core.p4>
#include <v1model.p4>

/* ============================================================
 * CONSTANTS
 * ============================================================ */
const bit<16> ETHERTYPE_IPV4    = 0x0800;
const bit<16> ETHERTYPE_TRACED  = 0x0900;  // our custom ethertype
const bit<8>  PROTO_ICMP        = 1;
const bit<8>  PROTO_TCP         = 6;
const bit<8>  PROTO_UDP         = 17;

/* ============================================================
 * HEADERS
 * ============================================================ */

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

/* Custom ISL path-trace header.
 * Inserted by ingress ground-station, stamped by each satellite,
 * stripped and digested by egress ground-station.
 * This is the feature that P4 enables and OpenFlow cannot do.
 */
header isl_trace_t {
    bit<8>  hopCount;   // how many satellite hops so far
    bit<16> sat0;       // first  satellite node ID
    bit<16> sat1;       // second satellite node ID
    bit<16> sat2;       // third  satellite node ID
    bit<16> reserved;   // padding to 64-bit boundary
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<8>  diffserv;
    bit<16> totalLen;
    bit<16> identification;
    bit<3>  flags;
    bit<13> fragOffset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdrChecksum;
    bit<32> srcAddr;
    bit<32> dstAddr;
}

header tcp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<32> seqNo;
    bit<32> ackNo;
    bit<4>  dataOffset;
    bit<4>  res;
    bit<8>  flags;
    bit<16> window;
    bit<16> checksum;
    bit<16> urgentPtr;
}

header udp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<16> length;
    bit<16> checksum;
}

/* ============================================================
 * STRUCTS
 * ============================================================ */

struct headers_t {
    ethernet_t  ethernet;
    isl_trace_t isl_trace;
    ipv4_t      ipv4;
    tcp_t       tcp;
    udp_t       udp;
}

/* Metadata carried between pipeline stages */
struct metadata_t {
    bit<16> node_id;       // this switch's satellite/GS node ID
    bit<8>  node_role;     // 0=satellite, 1=GS-ingress, 2=GS-egress
    bit<1>  do_trace;      // 1 = packet has ISL trace header
    bit<9>  egress_port;
}

/* Digest message sent to controller when a traced packet
 * arrives at egress ground station.
 * Controller logs which satellites the packet traversed.
 */
struct path_digest_t {
    bit<32> src_ip;
    bit<32> dst_ip;
    bit<8>  hop_count;
    bit<16> sat0;
    bit<16> sat1;
    bit<16> sat2;
    bit<9>  ingress_port;
}

/* ============================================================
 * PARSER
 * ============================================================ */

parser MyParser(
    packet_in             pkt,
    out   headers_t       hdr,
    inout metadata_t      meta,
    inout standard_metadata_t std_meta)
{
    state start {
        pkt.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            ETHERTYPE_IPV4   : parse_ipv4;
            ETHERTYPE_TRACED : parse_isl_trace;
            default          : accept;
        }
    }

    /* Packet already has our ISL trace header */
    state parse_isl_trace {
        pkt.extract(hdr.isl_trace);
        meta.do_trace = 1;
        transition parse_ipv4;
    }

    state parse_ipv4 {
        pkt.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) {
            PROTO_TCP : parse_tcp;
            PROTO_UDP : parse_udp;
            default   : accept;
        }
    }

    state parse_tcp {
        pkt.extract(hdr.tcp);
        transition accept;
    }

    state parse_udp {
        pkt.extract(hdr.udp);
        transition accept;
    }
}

/* ============================================================
 * CHECKSUM VERIFICATION (not needed for BMv2 experiments)
 * ============================================================ */

control MyVerifyChecksum(
    inout headers_t  hdr,
    inout metadata_t meta)
{
    apply { }
}

/* ============================================================
 * INGRESS PIPELINE
 * ============================================================ */

control MyIngress(
    inout headers_t       hdr,
    inout metadata_t      meta,
    inout standard_metadata_t std_meta)
{
    /* ── Per-port byte counter ─────────────────────────────
     * Controller reads this via P4Runtime CounterEntry
     * to implement load-aware routing.
     * 512 slots = one per possible port number.
     */
    counter(512, CounterType.bytes) port_bytes;

    /* ── Action: forward packet out a specific port ──────── */
    action do_forward(bit<9> port) {
        std_meta.egress_spec = port;
        port_bytes.count((bit<32>)(bit<9>)port);
    }

    /* ── Action: drop packet ─────────────────────────────── */
    action do_drop() {
        mark_to_drop(std_meta);
    }

    /* ── Action: insert ISL trace header ─────────────────────
     * Called when a packet enters the constellation at
     * an ingress ground-station.
     * Sets etherType = 0x0900 so satellites know to stamp it.
     */
    action insert_trace(bit<9> port) {
        hdr.isl_trace.setValid();
        hdr.isl_trace.hopCount  = 0;
        hdr.isl_trace.sat0      = 0;
        hdr.isl_trace.sat1      = 0;
        hdr.isl_trace.sat2      = 0;
        hdr.isl_trace.reserved  = 0;
        hdr.ethernet.etherType  = ETHERTYPE_TRACED;
        meta.do_trace           = 1;
        std_meta.egress_spec    = port;
        port_bytes.count((bit<32>)(bit<9>)port);
    }

    /* ── Action: stamp satellite ID into trace header ────────
     * Called at each satellite hop.
     * Shifts existing stamps and writes this satellite's ID.
     */
    action stamp_sat(bit<16> sat_id, bit<9> port) {
        hdr.isl_trace.sat2     = hdr.isl_trace.sat1;
        hdr.isl_trace.sat1     = hdr.isl_trace.sat0;
        hdr.isl_trace.sat0     = sat_id;
        hdr.isl_trace.hopCount = hdr.isl_trace.hopCount + 1;
        std_meta.egress_spec   = port;
        port_bytes.count((bit<32>)(bit<9>)port);
    }

    /* ── Action: strip trace header at egress GS ─────────────
     * Removes the ISL trace header and restores etherType.
     * Controller will receive a digest with path info.
     */
    action strip_trace(bit<9> port) {
        hdr.isl_trace.setInvalid();
        hdr.ethernet.etherType = ETHERTYPE_IPV4;
        meta.do_trace          = 0;
        std_meta.egress_spec   = port;
        port_bytes.count((bit<32>)(bit<9>)port);
    }

    /* ── Table 1: IPv4 LPM forwarding ────────────────────────
     * Primary forwarding table.
     * Controller populates this on every topology update.
     *
     * Actions:
     *   do_forward  : normal satellite-to-satellite hop
     *   insert_trace: used at ingress GS (adds trace header)
     *   stamp_sat   : used at satellite nodes
     *   strip_trace : used at egress GS (removes trace header)
     *   do_drop     : default for unknown destinations
     */
    table ipv4_lpm {
        key = {
            hdr.ipv4.dstAddr : lpm;
        }
        actions = {
            do_forward;
            insert_trace;
            stamp_sat;
            strip_trace;
            do_drop;
        }
        default_action = do_drop();
        size = 1024;
    }

    /* ── Table 2: node role ───────────────────────────────────
     * Maps ingress port to node role.
     * This tells the switch which action class to apply.
     * Controller sets this once at startup.
     *
     * role values:
     *   0 = satellite (stamp ISL trace)
     *   1 = ingress ground station (insert trace)
     *   2 = egress ground station (strip trace)
     */
    action set_role(bit<8> role, bit<16> node_id) {
        meta.node_role = role;
        meta.node_id   = node_id;
    }

    table node_config {
        key = {
            std_meta.ingress_port : exact;
        }
        actions = {
            set_role;
            NoAction;
        }
        default_action = NoAction();
        size = 64;
    }

    apply {
        /* Only process IPv4 and traced packets */
        if (hdr.ipv4.isValid() || hdr.isl_trace.isValid()) {

            /* Step 1: determine this node's role */
            node_config.apply();

            /* Step 2: apply forwarding based on destination */
            if (hdr.ipv4.isValid()) {
                ipv4_lpm.apply();
            }

            /* Step 3: decrement TTL to prevent routing loops */
            if (hdr.ipv4.isValid() && hdr.ipv4.ttl > 0) {
                hdr.ipv4.ttl = hdr.ipv4.ttl - 1;
            }
        }
    }
}

/* ============================================================
 * EGRESS PIPELINE
 * Sends path digest to controller when a traced packet
 * exits at a ground station.
 * ============================================================ */

control MyEgress(
    inout headers_t       hdr,
    inout metadata_t      meta,
    inout standard_metadata_t std_meta)
{
    apply {
        /* If this is a traced packet that has had its header stripped,
         * send a digest to the controller with the observed satellite path.
         * The controller logs this for path verification.
         */
        if (meta.do_trace == 0 && hdr.ipv4.isValid()
                && hdr.ethernet.etherType == ETHERTYPE_IPV4
                && meta.node_role == 2) {

            /* Note: digest() is the correct v1model extern.
             * digest_receiver_id = 1 identifies this digest type
             * to the controller.
             */
            digest<path_digest_t>(1, {
                hdr.ipv4.srcAddr,
                hdr.ipv4.dstAddr,
                hdr.isl_trace.hopCount,
                hdr.isl_trace.sat0,
                hdr.isl_trace.sat1,
                hdr.isl_trace.sat2,
                std_meta.ingress_port
            });
        }
    }
}

/* ============================================================
 * CHECKSUM UPDATE
 * Recalculate IPv4 header checksum after TTL decrement.
 * ============================================================ */

control MyComputeChecksum(
    inout headers_t  hdr,
    inout metadata_t meta)
{
    apply {
        update_checksum(
            hdr.ipv4.isValid(),
            {
                hdr.ipv4.version,
                hdr.ipv4.ihl,
                hdr.ipv4.diffserv,
                hdr.ipv4.totalLen,
                hdr.ipv4.identification,
                hdr.ipv4.flags,
                hdr.ipv4.fragOffset,
                hdr.ipv4.ttl,
                hdr.ipv4.protocol,
                hdr.ipv4.srcAddr,
                hdr.ipv4.dstAddr
            },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16
        );
    }
}

/* ============================================================
 * DEPARSER
 * Emit headers in wire order.
 * isl_trace is only emitted if setValid() was called.
 * ============================================================ */

control MyDeparser(
    packet_out pkt,
    in headers_t hdr)
{
    apply {
        pkt.emit(hdr.ethernet);
        pkt.emit(hdr.isl_trace);   // emitted only when valid
        pkt.emit(hdr.ipv4);
        pkt.emit(hdr.tcp);
        pkt.emit(hdr.udp);
    }
}

/* ============================================================
 * MAIN SWITCH INSTANTIATION
 * ============================================================ */

V1Switch(
    MyParser(),
    MyVerifyChecksum(),
    MyIngress(),
    MyEgress(),
    MyComputeChecksum(),
    MyDeparser()
) main;

