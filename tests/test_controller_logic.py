import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from os_ken.lib.packet import arp, ethernet, ether_types, ipv4, packet, tcp, udp 
from os_ken.ofproto import ofproto_v1_3, ofproto_v1_3_parser  

import exam_controller as ec 

SERVER = "10.0.3.100"
ROGUE = "10.0.1.66"
STUDENT = "10.0.1.1"
MAC = {"server": "00:00:00:00:00:03", "router": "00:00:00:00:00:fe", "student": "00:00:00:00:00:01"}


class FakeDatapath:
    def __init__(self, dpid):
        self.id = dpid
        self.ofproto = ofproto_v1_3
        self.ofproto_parser = ofproto_v1_3_parser
        self.sent = []

    def send_msg(self, msg):
        self.sent.append(msg)

    def flow_mods(self, priority=None, command=None):
        out = [m for m in self.sent if isinstance(m, ofproto_v1_3_parser.OFPFlowMod)]
        if priority is not None:
            out = [m for m in out if m.priority == priority]
        if command is not None:
            out = [m for m in out if m.command == command]
        return out


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def build_packet(*layers):
    pkt = packet.Packet()
    for layer in layers:
        pkt.add_protocol(layer)
    pkt.serialize()
    return pkt.data


def packet_in(dp, in_port, data):
    msg = SimpleNamespace(datapath=dp, match={"in_port": in_port}, data=data,
                          buffer_id=ofproto_v1_3.OFP_NO_BUFFER)
    return SimpleNamespace(msg=msg)


def tcp_data(src_ip, dst_ip, sport, dport, src_mac, dst_mac):
    return build_packet(ethernet.ethernet(dst=dst_mac, src=src_mac, ethertype=ether_types.ETH_TYPE_IP),
                        ipv4.ipv4(src=src_ip, dst=dst_ip, proto=6),
                        tcp.tcp(src_port=sport, dst_port=dport, bits=0x02))


def udp_data(src_ip, dst_ip, sport, dport, payload, src_mac, dst_mac):
    return build_packet(ethernet.ethernet(dst=dst_mac, src=src_mac, ethertype=ether_types.ETH_TYPE_IP),
                        ipv4.ipv4(src=src_ip, dst=dst_ip, proto=17),
                        udp.udp(src_port=sport, dst_port=dport), payload)


class ControllerTestCase(unittest.TestCase):
    ENV = {}

    def setUp(self):
        self.clock = Clock()
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "events.jsonl")
        env = {"EXAM_EVENT_LOG": self.log, "STATS_INTERVAL": "3600", "EXAM_MODE": "dynamic"}
        env.update(self.ENV)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        mono = mock.patch.object(time, "monotonic", self.clock)
        mono.start()
        self.addCleanup(mono.stop)
        
        spawn = mock.patch.object(ec.hub, "spawn", lambda *a, **k: None)
        spawn.start()
        self.addCleanup(spawn.stop)
        self.c = ec.ExamSDNController()
        self.addCleanup(self.c._event_file.close)
        self.dps = {i: FakeDatapath(i) for i in (1, 2, 3)}
        for dp in self.dps.values():
            self.c.switch_features_handler(SimpleNamespace(msg=SimpleNamespace(datapath=dp)))

    # ---- helpers
    def events(self, name=None):
        with open(self.log) as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        return [r for r in records if name is None or r["event"] == name]

    def signal(self, payload, src_ip=SERVER):
        data = udp_data(src_ip, "10.0.3.254", 40000, 9999, json.dumps(payload).encode(),
                        MAC["server"], MAC["router"])
        self.c.packet_in_handler(packet_in(self.dps[3], 1, data))

    def heartbeat(self, active):
        self.signal({"type": "heartbeat", "active": active, "completed": 0})

    def poll(self, rogue_bytes, rx_bytes, exam_bytes=0, extra_src=None):
        """One statistics round on the bottleneck switch (s3): flow stats first, then port stats."""
        dp = self.dps[3]
        parser = ofproto_v1_3_parser
        body = [SimpleNamespace(priority=0, match=parser.OFPMatch(), byte_count=0, packet_count=0),
                SimpleNamespace(priority=ec.PRIO_EXAM, byte_count=exam_bytes, packet_count=1,
                                match=parser.OFPMatch(in_port=2, ipv4_src=STUDENT, ipv4_dst=SERVER, ip_proto=6,
                                                      eth_type=0x0800, eth_dst=MAC["server"], tcp_dst=5050)),
                SimpleNamespace(priority=ec.PRIO_IP, byte_count=rogue_bytes, packet_count=1,
                                match=parser.OFPMatch(in_port=2, ipv4_src=ROGUE, ipv4_dst=SERVER, ip_proto=17,
                                                      eth_type=0x0800, eth_dst=MAC["server"]))]
        if extra_src:
            body.append(SimpleNamespace(priority=ec.PRIO_IP, byte_count=extra_src, packet_count=1,
                                        match=parser.OFPMatch(in_port=2, ipv4_src="10.0.2.1", ipv4_dst=SERVER,
                                                              ip_proto=17, eth_type=0x0800, eth_dst=MAC["server"])))
        self.c.flow_stats_reply_handler(SimpleNamespace(msg=SimpleNamespace(datapath=dp, body=body)))
        ports = [SimpleNamespace(port_no=2, rx_bytes=rx_bytes, tx_bytes=0)]
        self.c.port_stats_reply_handler(SimpleNamespace(msg=SimpleNamespace(datapath=dp, body=ports)))

    def flood_rounds(self, rounds, start_t=0):
        """Rogue sends 20 Mbit/s, the 10 Mbit/s bottleneck carries 10 Mbit/s (1.25 MB/s)."""
        for i in range(rounds):
            self.clock.t += 1
            self.poll(rogue_bytes=2_500_000 * (start_t + i), rx_bytes=1_250_000 * (start_t + i))

    def quarantine_rules(self, dp):
        return [m for m in dp.flow_mods(ec.PRIO_QUARANTINE, ofproto_v1_3.OFPFC_ADD)]


class TestHandshakeAndForwarding(ControllerTestCase):
    def test_switch_gets_table_miss_and_signalling_rule(self):
        for dp in self.dps.values():
            self.assertEqual(len(dp.flow_mods(ec.PRIO_TABLE_MISS)), 1)
            control = dp.flow_mods(ec.PRIO_CONTROL)
            self.assertEqual(len(control), 1)
            self.assertEqual(control[0].match["udp_dst"], 9999)

    def test_exam_traffic_gets_priority_20_rule_with_port_match(self):
        dp = self.dps[1]
        # learn both MACs first (router on port 4, student on port 1), then send exam traffic
        self.c.packet_in_handler(packet_in(dp, 4, tcp_data(SERVER, STUDENT, 5050, 40000, MAC["router"], MAC["student"])))
        self.c.packet_in_handler(packet_in(dp, 1, tcp_data(STUDENT, SERVER, 40000, 5050, MAC["student"], MAC["router"])))
        # now both directions are known: a reply packet gets the tcp_src rule
        self.c.packet_in_handler(packet_in(dp, 4, tcp_data(SERVER, STUDENT, 5050, 40000, MAC["router"], MAC["student"])))
        rules = dp.flow_mods(ec.PRIO_EXAM)
        self.assertTrue(any(r.match.get("tcp_dst") == 5050 and r.match["ipv4_src"] == STUDENT for r in rules), rules)
        self.assertTrue(any(r.match.get("tcp_src") == 5050 for r in rules), "reverse direction rule missing")

    def test_non_exam_ip_gets_priority_15_rule_only(self):
        dp = self.dps[1]
        self.c.packet_in_handler(packet_in(dp, 4, udp_data(SERVER, ROGUE, 9000, 5555, b"x", MAC["router"], MAC["student"])))
        self.c.packet_in_handler(packet_in(dp, 3, udp_data(ROGUE, SERVER, 5555, 9000, b"x", MAC["student"], MAC["router"])))
        self.assertTrue(dp.flow_mods(ec.PRIO_IP))
        self.assertFalse(dp.flow_mods(ec.PRIO_EXAM), "non-exam traffic must not get an exam rule")

    def test_arp_is_forwarded_without_a_flow_rule(self):
        dp = self.dps[1]
        before = len(dp.flow_mods())
        data = build_packet(ethernet.ethernet(dst="ff:ff:ff:ff:ff:ff", src=MAC["student"], ethertype=ether_types.ETH_TYPE_ARP),
                            arp.arp(opcode=1, src_mac=MAC["student"], src_ip=STUDENT, dst_mac="00:00:00:00:00:00", dst_ip="10.0.1.254"))
        self.c.packet_in_handler(packet_in(dp, 1, data))
        self.assertEqual(len(dp.flow_mods()), before)
        self.assertTrue(any(isinstance(m, ofproto_v1_3_parser.OFPPacketOut) for m in dp.sent))


class TestSignalling(ControllerTestCase):
    def test_heartbeat_from_server_updates_app_state_and_is_consumed(self):
        self.heartbeat(7)
        self.assertEqual(self.c.app_active, 7)
        self.assertTrue(self.events("APP_HEARTBEAT"))
        self.assertFalse([m for m in self.dps[3].sent if isinstance(m, ofproto_v1_3_parser.OFPPacketOut)],
                         "signalling datagrams must not be forwarded")

    def test_signal_from_other_host_is_rejected(self):
        self.signal({"type": "heartbeat", "active": 99}, src_ip=ROGUE)
        self.assertEqual(self.c.app_active, 0)
        self.assertEqual(self.events("CONTROL_REJECTED")[0]["src"], ROGUE)

    def test_malformed_signal_is_rejected(self):
        data = udp_data(SERVER, "10.0.3.254", 1, 9999, b"not json", MAC["server"], MAC["router"])
        self.c.packet_in_handler(packet_in(self.dps[3], 1, data))
        self.assertEqual(self.events("CONTROL_REJECTED")[0]["reason"], "malformed payload")

    def test_start_end_events_are_counted(self):
        self.signal({"type": "exam_start", "student": "a", "ip": STUDENT})
        self.signal({"type": "exam_start", "student": "b", "ip": STUDENT})
        self.signal({"type": "exam_end", "student": "a", "ip": STUDENT})
        self.assertEqual((self.c.app_starts, self.c.app_ends), (2, 1))


class TestDynamicMitigation(ControllerTestCase):
    def test_no_false_positive_on_light_load(self):
        self.heartbeat(5)
        for i in range(6):
            self.clock.t += 1
            self.poll(rogue_bytes=1000 * i, rx_bytes=50_000 * i, exam_bytes=50_000 * i)
        self.assertFalse(self.events("CONGESTION_DETECTED"))
        self.assertFalse(self.c.quarantined)

    def test_congestion_triggers_quarantine_of_top_talker_on_every_switch(self):
        self.heartbeat(5)
        self.flood_rounds(4)
        installed = self.events("MITIGATION_INSTALLED")
        self.assertEqual(len(installed), 1, installed)
        self.assertEqual(installed[0]["offender"], ROGUE)
        for dp in self.dps.values():
            rules = self.quarantine_rules(dp)
            self.assertEqual(len(rules), 1, f"s{dp.id}")
            self.assertEqual(rules[0].match["ipv4_src"], ROGUE)
            self.assertEqual(list(rules[0].instructions), [], "quarantine rule must be a drop rule")
        self.assertLess(ec.PRIO_QUARANTINE, ec.PRIO_EXAM, "exam flows must outrank the quarantine rule")

    def test_detection_needs_consecutive_hot_polls(self):
        self.heartbeat(5)
        self.flood_rounds(2)            
        self.assertFalse(self.events("MITIGATION_INSTALLED"))
        self.flood_rounds(1, start_t=2)
        self.assertTrue(self.events("MITIGATION_INSTALLED"))

    def test_top_talker_is_chosen_among_several_sources(self):
        self.heartbeat(5)
        for i in range(4):
            self.clock.t += 1
            self.poll(rogue_bytes=2_500_000 * i, rx_bytes=1_250_000 * i, extra_src=300_000 * i)
        self.assertEqual(self.events("MITIGATION_INSTALLED")[0]["offender"], ROGUE)

    def test_server_is_never_chosen_as_offender(self):
        self.c.src_rates = {SERVER: 50e6}
        self.assertEqual(self.c.top_talker()[0], SERVER)  
        self.heartbeat(5)
        self.flood_rounds(4)                             
        self.assertNotIn(SERVER, self.c.quarantined)

    def test_app_gate_blocks_mitigation_when_no_exam_is_active(self):
        self.heartbeat(0)
        self.flood_rounds(5)
        self.assertTrue(self.events("CONGESTION_DETECTED"))
        self.assertFalse(self.events("MITIGATION_INSTALLED"), "no exam running -> leave traffic alone")

    def test_gate_falls_back_to_network_only_when_heartbeat_is_stale(self):
        self.heartbeat(0)
        self.clock.t += 10                
        self.flood_rounds(4)
        self.assertTrue(self.events("MITIGATION_INSTALLED"))

    def test_gate_falls_back_when_no_heartbeat_was_ever_seen(self):
        self.flood_rounds(4)
        self.assertTrue(self.events("MITIGATION_INSTALLED"))

    def test_quarantine_is_released_after_the_timer(self):
        self.heartbeat(5)
        self.flood_rounds(4)
        self.assertIn(ROGUE, self.c.quarantined)
        self.clock.t += 5
        self.c._expire_quarantines()
        self.assertIn(ROGUE, self.c.quarantined, "must not be released early")
        self.clock.t += 20
        self.c._expire_quarantines()
        self.assertNotIn(ROGUE, self.c.quarantined)
        self.assertTrue(self.events("MITIGATION_RELEASED"))
        for dp in self.dps.values():
            deletes = dp.flow_mods(ec.PRIO_QUARANTINE, ofproto_v1_3.OFPFC_DELETE_STRICT)
            self.assertEqual(len(deletes), 1)
            self.assertEqual(deletes[0].match["ipv4_src"], ROGUE)

    def test_reset_message_releases_everything(self):
        self.heartbeat(5)
        self.flood_rounds(4)
        self.signal({"type": "reset"})
        self.assertFalse(self.c.quarantined)
        self.assertTrue(self.events("STATE_RESET"))

    def test_stats_records_are_written_each_poll(self):
        self.heartbeat(2)
        self.flood_rounds(3)
        stats = self.events("STATS")
        self.assertGreaterEqual(len(stats), 2)
        self.assertIn("util", stats[-1])
        self.assertEqual(stats[-1]["app_active"], 2)


class TestStaticBaseline(ControllerTestCase):
    ENV = {"EXAM_MODE": "static"}

    def test_static_mode_observes_but_never_acts(self):
        self.heartbeat(5)
        self.flood_rounds(8)
        self.assertTrue(self.events("CONGESTION_DETECTED"))
        self.assertFalse(self.events("MITIGATION_INSTALLED"))
        for dp in self.dps.values():
            self.assertFalse(self.quarantine_rules(dp))


if __name__ == "__main__":
    unittest.main(verbosity=2)
