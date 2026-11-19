import argparse
import os
import sys
import time

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import Node, OVSSwitch, RemoteController
from mininet.topo import Topo

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_IP = "10.0.3.100"
ROGUE_IP = "10.0.1.66"
STUDENTS = ["student1", "student2", "student3", "student4"]
HOOK_ADDR = "10.0.3.254:9999"  


class LinuxRouter(Node):
    """A Mininet node with IP forwarding turned on."""

    def config(self, **params):
        super().config(**params)
        self.cmd("sysctl -w net.ipv4.ip_forward=1")

    def terminate(self):
        self.cmd("sysctl -w net.ipv4.ip_forward=0")
        super().terminate()


class ExamTopo(Topo):
    def build(self, bw=10, delay="2ms", queue=200):
        bottleneck = dict(bw=bw, delay=delay, max_queue_size=queue)

        s1 = self.addSwitch("s1", protocols="OpenFlow13")
        s2 = self.addSwitch("s2", protocols="OpenFlow13")
        s3 = self.addSwitch("s3", protocols="OpenFlow13")

        router = self.addNode("r1", cls=LinuxRouter, ip="10.0.1.254/24")

        student1 = self.addHost("student1", ip="10.0.1.1/24", defaultRoute="via 10.0.1.254")
        student2 = self.addHost("student2", ip="10.0.1.2/24", defaultRoute="via 10.0.1.254")
        rogue = self.addHost("rogue", ip=ROGUE_IP + "/24", defaultRoute="via 10.0.1.254")
        student3 = self.addHost("student3", ip="10.0.2.1/24", defaultRoute="via 10.0.2.254")
        student4 = self.addHost("student4", ip="10.0.2.2/24", defaultRoute="via 10.0.2.254")
        examserver = self.addHost("examserver", ip=SERVER_IP + "/24", defaultRoute="via 10.0.3.254")

        self.addLink(student1, s1, port2=1)
        self.addLink(student2, s1, port2=2)
        self.addLink(rogue, s1, port2=3)
        self.addLink(student3, s2, port2=1)
        self.addLink(student4, s2, port2=2)
        self.addLink(examserver, s3, port2=1)

        self.addLink(s1, router, port1=4, intfName2="r1-eth1", params2={"ip": "10.0.1.254/24"})
        self.addLink(s2, router, port1=3, intfName2="r1-eth2", params2={"ip": "10.0.2.254/24"})
        self.addLink(s3, router, port1=2, intfName2="r1-eth3",
                     params1=dict(bottleneck), params2=dict(bottleneck, ip="10.0.3.254/24"))


topos = {"examtopo": (lambda: ExamTopo())}


def build_network(controller_ip="127.0.0.1", controller_port=6653, bw=10):
    """Create (but do not start) the Mininet network with a remote controller."""
    net = Mininet(topo=ExamTopo(bw=bw), link=TCLink, switch=OVSSwitch, controller=None,
                  build=False, autoSetMacs=True)
    net.addController("c0", controller=RemoteController, ip=controller_ip, port=controller_port)
    net.build()
    return net


def wait_for_switches(net, timeout=20):
    """Wait until every switch is connected to the controller."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(sw.connected() for sw in net.switches):
            return True
        time.sleep(0.5)
    return False


def start_server(net, exam_duration=60, results_file="", hook=True, log="/tmp/examserver.log"):
    """Start server.py on examserver (background). Returns the Popen object."""
    env = f"EXAM_DURATION={exam_duration} "
    if hook:
        env += f"SDN_HOOK_ADDR={HOOK_ADDR} "
    if results_file:
        env += f"EXAM_RESULTS_FILE={results_file} "
    cmd = f"exec env {env} python3 {os.path.join(SRC_DIR, 'server.py')} > {log} 2>&1"
    return net.get("examserver").popen(["sh", "-c", cmd])


def start_sink(net, port=9000, log="/tmp/udpsink.log"):
    """UDP sink on the server: receives the noisy host's bulk traffic."""
    cmd = f"exec python3 {os.path.join(SRC_DIR, 'traffic_gen.py')} sink --port {port} > {log} 2>&1"
    return net.get("examserver").popen(["sh", "-c", cmd])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--controller-port", type=int, default=6653)
    parser.add_argument("--bw", type=float, default=10, help="bottleneck bandwidth in Mbit/s (default 10)")
    parser.add_argument("--exam-duration", type=float, default=60)
    parser.add_argument("--no-server", action="store_true", help="do not auto-start server.py")
    args = parser.parse_args()

    setLogLevel("info")
    net = build_network(args.controller_ip, args.controller_port, args.bw)
    net.start()
    try:
        if not wait_for_switches(net):
            print("WARNING: switches did not all connect to the controller", file=sys.stderr)
        if not args.no_server:
            start_server(net, args.exam_duration)
            start_sink(net)
            time.sleep(1)
            print(f"examserver: server.py on {SERVER_IP}:5050 (log /tmp/examserver.log), UDP sink on :9000")
        print("Try:  pingall | student1 python3 src/client.py | rogue python3 src/traffic_gen.py flood ...")
        CLI(net)
    finally:
        net.stop()


if __name__ == "__main__":
    main()
