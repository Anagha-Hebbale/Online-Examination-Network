import json
import os
import time
from collections import defaultdict

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.lib import hub
from os_ken.lib.packet import ethernet, ether_types, ipv4, packet, tcp, udp
from os_ken.ofproto import ofproto_v1_3

PRIO_TABLE_MISS = 0
PRIO_IP = 15
PRIO_QUARANTINE = 18
PRIO_EXAM = 20
PRIO_CONTROL = 30

IP_PROTO_TCP = 6
IP_PROTO_UDP = 17


def _env(name, default, cast=str):
    value = os.environ.get(name)
    return cast(value) if value not in (None, "") else default


class ExamSDNController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = _env("EXAM_MODE", "dynamic")
        self.stats_interval = _env("STATS_INTERVAL", 1.0, float)
        self.bottleneck_dpid = _env("BOTTLENECK_DPID", 3, int)
        self.bottleneck_port = _env("BOTTLENECK_PORT", 2, int)
        self.bottleneck_bps = _env("BOTTLENECK_MBPS", 10.0, float) * 1e6
        self.congestion_threshold = _env("CONGESTION_THRESHOLD", 0.8, float)
        self.congestion_polls = _env("CONGESTION_POLLS", 2, int)
        self.min_offender_bps = _env("MIN_OFFENDER_MBPS", 1.0, float) * 1e6
        self.quarantine_seconds = _env("QUARANTINE_SECONDS", 15.0, float)
        self.server_ip = _env("EXAM_SERVER_IP", "10.0.3.100")
        self.exam_port = _env("EXAM_PORT", 5050, int)
        self.control_port = _env("CONTROL_UDP_PORT", 9999, int)
        self.event_log_path = _env("EXAM_EVENT_LOG", "controller_events.jsonl")

        self.datapaths = {}
        self.mac_to_port = {}
        self.flow_prev = defaultdict(dict)  
        self.flow_ts = {}                 
        self.port_prev = {}                
        self.src_rates = {}                  
        self.exam_bps = 0.0                  
        self.util = 0.0
        self.hot_polls = 0
        
        self.app_active = 0
        self.app_last_heartbeat = None       
        self.app_starts = 0
        self.app_ends = 0
        
        self.quarantined = {}

        self._event_file = open(self.event_log_path, "a", buffering=1, encoding="utf-8")
        self._emit("CONTROLLER_STARTED", mode=self.mode, bottleneck_dpid=self.bottleneck_dpid,
                   bottleneck_port=self.bottleneck_port, bottleneck_mbps=self.bottleneck_bps / 1e6,
                   threshold=self.congestion_threshold, polls=self.congestion_polls,
                   quarantine_seconds=self.quarantine_seconds)
        hub.spawn(self._stats_loop)

  
    def _emit(self, event, **fields):
        record = {"ts": time.time(), "event": event}
        record.update(fields)
        self._event_file.write(json.dumps(record) + "\n")
        if event != "STATS":
            self.logger.info("%s %s", event, fields)

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0):
        """Install a flow. An empty action list means 'drop'."""
        parser = datapath.ofproto_parser
        instructions = []
        if actions:
            instructions = [parser.OFPInstructionActions(datapath.ofproto.OFPIT_APPLY_ACTIONS, actions)]
        datapath.send_msg(parser.OFPFlowMod(datapath=datapath, priority=priority, match=match,
                                            instructions=instructions, idle_timeout=idle_timeout))

    def delete_flow_strict(self, datapath, priority, match):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        datapath.send_msg(parser.OFPFlowMod(datapath=datapath, command=ofproto.OFPFC_DELETE_STRICT,
                                            out_port=ofproto.OFPP_ANY, out_group=ofproto.OFPG_ANY,
                                            priority=priority, match=match))

  
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        to_controller = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        # Table-miss: unknown packets go to the controller.
        self.add_flow(datapath, PRIO_TABLE_MISS, parser.OFPMatch(), to_controller)
        # Signalling rule: exam-server datagrams are consumed by the controller.
        control_match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=IP_PROTO_UDP,
                                        udp_dst=self.control_port)
        self.add_flow(datapath, PRIO_CONTROL, control_match, to_controller)
        self.datapaths[datapath.id] = datapath
        self._emit("SWITCH_CONNECTED", dpid=datapath.id)

    
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        ip_header = pkt.get_protocol(ipv4.ipv4)
        udp_header = pkt.get_protocol(udp.udp)

        if ip_header and udp_header and udp_header.dst_port == self.control_port:
            payload = pkt.protocols[-1] if isinstance(pkt.protocols[-1], (bytes, bytearray)) else b""
            self.handle_signal(ip_header.src, bytes(payload))
            return

        self.mac_to_port.setdefault(datapath.id, {})[eth.src] = in_port
        out_port = self.mac_to_port[datapath.id].get(eth.dst, ofproto.OFPP_FLOOD)

        if ip_header and out_port != ofproto.OFPP_FLOOD and out_port != in_port:
            self.install_ip_flow(datapath, in_port, out_port, eth, ip_header, pkt.get_protocol(tcp.tcp))

        data = None if msg.buffer_id != ofproto.OFP_NO_BUFFER else msg.data
        datapath.send_msg(parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id, in_port=in_port,
                                              actions=[parser.OFPActionOutput(out_port)], data=data))

    def install_ip_flow(self, datapath, in_port, out_port, eth, ip_header, tcp_header):
        """Exam traffic -> priority 20 (+TCP port match); everything else -> priority 15."""
        parser = datapath.ofproto_parser
        fields = dict(in_port=in_port, eth_dst=eth.dst, eth_type=ether_types.ETH_TYPE_IP,
                      ipv4_src=ip_header.src, ipv4_dst=ip_header.dst, ip_proto=ip_header.proto)
        priority = PRIO_IP
        if tcp_header is not None and self.exam_port in (tcp_header.src_port, tcp_header.dst_port):
            priority = PRIO_EXAM
            if tcp_header.dst_port == self.exam_port:
                fields["tcp_dst"] = self.exam_port
            else:
                fields["tcp_src"] = self.exam_port
        self.add_flow(datapath, priority, parser.OFPMatch(**fields),
                      [parser.OFPActionOutput(out_port)], idle_timeout=30)
        if priority == PRIO_EXAM:
            self._emit("EXAM_FLOW_INSTALLED", dpid=datapath.id, src=ip_header.src, dst=ip_header.dst,
                       in_port=in_port, out_port=out_port)

   
    def handle_signal(self, src_ip, payload):
        if src_ip != self.server_ip:
            self._emit("CONTROL_REJECTED", src=src_ip, reason="not the exam server")
            return
        try:
            msg = json.loads(payload.decode("utf-8"))
            kind = msg["type"]
        except (ValueError, KeyError, UnicodeDecodeError):
            self._emit("CONTROL_REJECTED", src=src_ip, reason="malformed payload")
            return

        if kind == "heartbeat":
            active = int(msg.get("active", 0))
            if self.app_last_heartbeat is None or active != self.app_active:
                self._emit("APP_HEARTBEAT", active=active, completed=msg.get("completed"))
            self.app_active = active
            self.app_last_heartbeat = time.monotonic()
        elif kind == "exam_start":
            self.app_starts += 1
        elif kind == "exam_end":
            self.app_ends += 1
        elif kind == "reset":
            self.reset_state()
        else:
            self._emit("CONTROL_REJECTED", src=src_ip, reason="unknown type %r" % kind)

    def reset_state(self):
        """Release every quarantine and clear congestion state (used between experiments)."""
        for ip in list(self.quarantined):
            self._release(ip, reason="reset")
        self.hot_polls = 0
        self.src_rates = {}
        self._emit("STATE_RESET")

    def _app_gate_open(self):
        if self.app_last_heartbeat is None or time.monotonic() - self.app_last_heartbeat > 5.0:
            return True
        return self.app_active > 0

    # ------------------------------------------------------------ monitoring
    def _stats_loop(self):
        while True:
            for datapath in list(self.datapaths.values()):
                parser = datapath.ofproto_parser
                datapath.send_msg(parser.OFPFlowStatsRequest(datapath))
                datapath.send_msg(parser.OFPPortStatsRequest(datapath, 0, datapath.ofproto.OFPP_ANY))
            hub.sleep(self.stats_interval)
            self._expire_quarantines()

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.monotonic()
        previous_ts = self.flow_ts.get(dpid)
        dt = (now - previous_ts) if previous_ts else None
        self.flow_ts[dpid] = now
        previous = self.flow_prev[dpid]
        current = {}
        src_rates = defaultdict(float)
        exam_bps = 0.0

        for stat in ev.msg.body:
            if stat.priority in (PRIO_TABLE_MISS, PRIO_CONTROL):
                continue
            key = (stat.priority, tuple(sorted(stat.match.items())))
            current[key] = stat.byte_count
            if not dt:
                continue
            count = stat.byte_count
            before = previous.get(key, 0)
            delta = count - before if count >= before else count
            bps = delta * 8.0 / dt
            if stat.priority == PRIO_EXAM:
                exam_bps += bps
            elif stat.priority == PRIO_IP and dpid == self.bottleneck_dpid \
                    and stat.match.get("in_port") == self.bottleneck_port:
                src = stat.match.get("ipv4_src")
                if src and src != self.server_ip:
                    src_rates[src] += bps

        self.flow_prev[dpid] = current
        if dpid == self.bottleneck_dpid:
            self.src_rates = dict(src_rates)
            self.exam_bps = exam_bps

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        datapath = ev.msg.datapath
        if datapath.id != self.bottleneck_dpid:
            return
        now = time.monotonic()
        for stat in ev.msg.body:
            if stat.port_no != self.bottleneck_port:
                continue
            key = (datapath.id, stat.port_no)
            previous = self.port_prev.get(key)
            self.port_prev[key] = (now, stat.rx_bytes, stat.tx_bytes)
            if previous is None:
                return
            dt = now - previous[0]
            if dt <= 0:
                return
            rx_bps = max(stat.rx_bytes - previous[1], 0) * 8.0 / dt
            tx_bps = max(stat.tx_bytes - previous[2], 0) * 8.0 / dt
            self.util = max(rx_bps, tx_bps) / self.bottleneck_bps
            self.evaluate(self.util, rx_bps, tx_bps)


    def top_talker(self):
        if not self.src_rates:
            return None, 0.0
        ip, bps = max(self.src_rates.items(), key=lambda item: item[1])
        if bps < self.min_offender_bps or ip in self.quarantined:
            return None, bps
        return ip, bps

    def evaluate(self, util, rx_bps=0.0, tx_bps=0.0):
        """One control decision per statistics poll of the bottleneck port."""
        self.hot_polls = self.hot_polls + 1 if util >= self.congestion_threshold else 0
        top = sorted(self.src_rates.items(), key=lambda item: -item[1])[:3]
        self._emit("STATS", mode=self.mode, util=round(util, 3), rx_mbps=round(rx_bps / 1e6, 3),
                   tx_mbps=round(tx_bps / 1e6, 3), exam_mbps=round(self.exam_bps / 1e6, 3),
                   top_talkers={ip: round(bps / 1e6, 3) for ip, bps in top},
                   hot_polls=self.hot_polls, app_active=self.app_active,
                   app_starts=self.app_starts, app_ends=self.app_ends,
                   quarantined=sorted(self.quarantined))

        if self.hot_polls < self.congestion_polls:
            return None
        offender, bps = self.top_talker()
        if offender is None:
            return None
        gate_open = self._app_gate_open()
        self._emit("CONGESTION_DETECTED", mode=self.mode, util=round(util, 3), offender=offender,
                   offender_mbps=round(bps / 1e6, 3), exams_active=self.app_active, app_gate_open=gate_open)
        if self.mode != "dynamic" or not gate_open:
            return None
        self._quarantine(offender, bps)
        return offender

    def _quarantine(self, ip, bps):
        now = time.monotonic()
        for datapath in self.datapaths.values():
            parser = datapath.ofproto_parser
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_src=ip)
            self.add_flow(datapath, PRIO_QUARANTINE, match, [])
        self.quarantined[ip] = {"since": now, "until": now + self.quarantine_seconds}
        self._emit("MITIGATION_INSTALLED", offender=ip, offender_mbps=round(bps / 1e6, 3),
                   action="drop non-exam IP traffic", priority=PRIO_QUARANTINE,
                   seconds=self.quarantine_seconds, switches=sorted(self.datapaths))
        self.hot_polls = 0

    def _release(self, ip, reason="expired"):
        for datapath in self.datapaths.values():
            parser = datapath.ofproto_parser
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_src=ip)
            self.delete_flow_strict(datapath, PRIO_QUARANTINE, match)
        self.quarantined.pop(ip, None)
        self._emit("MITIGATION_RELEASED", offender=ip, reason=reason)

    def _expire_quarantines(self):
        now = time.monotonic()
        for ip, info in list(self.quarantined.items()):
            if now >= info["until"]:
                self._release(ip)
