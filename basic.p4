/* basic.p4 - minimal L2 forwarding for verification */
#include <core.p4>
#include <v1model.p4>

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

struct headers_t { ethernet_t ethernet; }
struct metadata_t {}

parser MyParser(packet_in pkt, out headers_t hdr,
                inout metadata_t meta,
                inout standard_metadata_t std_meta) {
    state start {
        pkt.extract(hdr.ethernet);
        transition accept;
    }
}

control MyVerifyChecksum(inout headers_t hdr,
                         inout metadata_t meta) {
    apply {}
}

control MyIngress(inout headers_t hdr,
                  inout metadata_t meta,
                  inout standard_metadata_t std_meta) {

    action forward(bit<9> port) {
        std_meta.egress_spec = port;
    }
    action drop() {
        mark_to_drop(std_meta);
    }

    table l2_forward {
        key = { hdr.ethernet.dstAddr : exact; }
        actions = { forward; drop; }
        default_action = drop();
        size = 16;
    }

    apply { l2_forward.apply(); }
}

control MyEgress(inout headers_t hdr,
                 inout metadata_t meta,
                 inout standard_metadata_t std_meta) {
    apply {}
}

control MyComputeChecksum(inout headers_t hdr,
                          inout metadata_t meta) {
    apply {}
}

control MyDeparser(packet_out pkt, in headers_t hdr) {
    apply { pkt.emit(hdr.ethernet); }
}

V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(),
         MyEgress(), MyComputeChecksum(), MyDeparser()) main;
