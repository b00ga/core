"""
Defines network nodes used within core.
"""

import logging
import threading
import time
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Type

import netaddr

from core import utils
from core.constants import EBTABLES_BIN, TC_BIN
from core.emulator.data import LinkData, NodeData
from core.emulator.emudata import LinkOptions
from core.emulator.enumerations import (
    LinkTypes,
    MessageFlags,
    NetworkPolicy,
    NodeTypes,
    RegisterTlvs,
)
from core.errors import CoreCommandError, CoreError
from core.nodes.base import CoreNetworkBase
from core.nodes.interface import CoreInterface, GreTap, Veth
from core.nodes.netclient import get_net_client

if TYPE_CHECKING:
    from core.emulator.distributed import DistributedServer
    from core.emulator.session import Session
    from core.location.mobility import WirelessModel, WayPointMobility

    WirelessModelType = Type[WirelessModel]

ebtables_lock = threading.Lock()


class EbtablesQueue:
    """
    Helper class for queuing up ebtables commands into rate-limited
    atomic commits. This improves performance and reliability when there are
    many WLAN link updates.
    """

    # update rate is every 300ms
    rate: float = 0.3
    # ebtables
    atomic_file: str = "/tmp/pycore.ebtables.atomic"

    def __init__(self) -> None:
        """
        Initialize the helper class, but don't start the update thread
        until a WLAN is instantiated.
        """
        self.doupdateloop: bool = False
        self.updatethread: Optional[threading.Thread] = None
        # this lock protects cmds and updates lists
        self.updatelock: threading.Lock = threading.Lock()
        # list of pending ebtables commands
        self.cmds: List[str] = []
        # list of WLANs requiring update
        self.updates: List["CoreNetwork"] = []
        # timestamps of last WLAN update; this keeps track of WLANs that are
        # using this queue
        self.last_update_time: Dict["CoreNetwork", float] = {}

    def startupdateloop(self, wlan: "CoreNetwork") -> None:
        """
        Kick off the update loop; only needs to be invoked once.

        :return: nothing
        """
        with self.updatelock:
            self.last_update_time[wlan] = time.monotonic()
        if self.doupdateloop:
            return
        self.doupdateloop = True
        self.updatethread = threading.Thread(target=self.updateloop, daemon=True)
        self.updatethread.start()

    def stopupdateloop(self, wlan: "CoreNetwork") -> None:
        """
        Kill the update loop thread if there are no more WLANs using it.

        :return: nothing
        """
        with self.updatelock:
            try:
                del self.last_update_time[wlan]
            except KeyError:
                logging.exception(
                    "error deleting last update time for wlan, ignored before: %s", wlan
                )
        if len(self.last_update_time) > 0:
            return
        self.doupdateloop = False
        if self.updatethread:
            self.updatethread.join()
            self.updatethread = None

    def ebatomiccmd(self, cmd: str) -> str:
        """
        Helper for building ebtables atomic file command list.

        :param cmd: ebtable command
        :return: ebtable atomic command
        """
        return f"{EBTABLES_BIN} --atomic-file {self.atomic_file} {cmd}"

    def lastupdate(self, wlan: "CoreNetwork") -> float:
        """
        Return the time elapsed since this WLAN was last updated.

        :param wlan: wlan entity
        :return: elpased time
        """
        try:
            elapsed = time.monotonic() - self.last_update_time[wlan]
        except KeyError:
            self.last_update_time[wlan] = time.monotonic()
            elapsed = 0.0

        return elapsed

    def updated(self, wlan: "CoreNetwork") -> None:
        """
        Keep track of when this WLAN was last updated.

        :param wlan: wlan entity
        :return: nothing
        """
        self.last_update_time[wlan] = time.monotonic()
        self.updates.remove(wlan)

    def updateloop(self) -> None:
        """
        Thread target that looks for WLANs needing update, and
        rate limits the amount of ebtables activity. Only one userspace program
        should use ebtables at any given time, or results can be unpredictable.

        :return: nothing
        """
        while self.doupdateloop:
            with self.updatelock:
                for wlan in self.updates:
                    # Check if wlan is from a previously closed session. Because of the
                    # rate limiting scheme employed here, this may happen if a new session
                    # is started soon after closing a previous session.
                    # TODO: if these are WlanNodes, this will never throw an exception
                    try:
                        wlan.session
                    except Exception:
                        # Just mark as updated to remove from self.updates.
                        self.updated(wlan)
                        continue

                    if self.lastupdate(wlan) > self.rate:
                        self.buildcmds(wlan)
                        self.ebcommit(wlan)
                        self.updated(wlan)

            time.sleep(self.rate)

    def ebcommit(self, wlan: "CoreNetwork") -> None:
        """
        Perform ebtables atomic commit using commands built in the self.cmds list.

        :return: nothing
        """
        # save kernel ebtables snapshot to a file
        args = self.ebatomiccmd("--atomic-save")
        wlan.host_cmd(args)

        # modify the table file using queued ebtables commands
        for c in self.cmds:
            args = self.ebatomiccmd(c)
            wlan.host_cmd(args)
        self.cmds = []

        # commit the table file to the kernel
        args = self.ebatomiccmd("--atomic-commit")
        wlan.host_cmd(args)

        try:
            wlan.host_cmd(f"rm -f {self.atomic_file}")
        except CoreCommandError:
            logging.exception("error removing atomic file: %s", self.atomic_file)

    def ebchange(self, wlan: "CoreNetwork") -> None:
        """
        Flag a change to the given WLAN's _linked dict, so the ebtables
        chain will be rebuilt at the next interval.

        :return: nothing
        """
        with self.updatelock:
            if wlan not in self.updates:
                self.updates.append(wlan)

    def buildcmds(self, wlan: "CoreNetwork") -> None:
        """
        Inspect a _linked dict from a wlan, and rebuild the ebtables chain for that WLAN.

        :return: nothing
        """
        with wlan._linked_lock:
            if wlan.has_ebtables_chain:
                # flush the chain
                self.cmds.append(f"-F {wlan.brname}")
            else:
                wlan.has_ebtables_chain = True
                self.cmds.extend(
                    [
                        f"-N {wlan.brname} -P {wlan.policy.value}",
                        f"-A FORWARD --logical-in {wlan.brname} -j {wlan.brname}",
                    ]
                )
            # rebuild the chain
            for netif1, v in wlan._linked.items():
                for netif2, linked in v.items():
                    if wlan.policy == NetworkPolicy.DROP and linked:
                        self.cmds.extend(
                            [
                                f"-A {wlan.brname} -i {netif1.localname} -o {netif2.localname} -j ACCEPT",
                                f"-A {wlan.brname} -o {netif1.localname} -i {netif2.localname} -j ACCEPT",
                            ]
                        )
                    elif wlan.policy == NetworkPolicy.ACCEPT and not linked:
                        self.cmds.extend(
                            [
                                f"-A {wlan.brname} -i {netif1.localname} -o {netif2.localname} -j DROP",
                                f"-A {wlan.brname} -o {netif1.localname} -i {netif2.localname} -j DROP",
                            ]
                        )


# a global object because all WLANs share the same queue
# cannot have multiple threads invoking the ebtables commnd
ebq: EbtablesQueue = EbtablesQueue()


def ebtablescmds(call: Callable[..., str], cmds: List[str]) -> None:
    """
    Run ebtable commands.

    :param call: function to call commands
    :param cmds: commands to call
    :return: nothing
    """
    with ebtables_lock:
        for args in cmds:
            call(args)


class CoreNetwork(CoreNetworkBase):
    """
    Provides linux bridge network functionality for core nodes.
    """

    policy: NetworkPolicy = NetworkPolicy.DROP

    def __init__(
        self,
        session: "Session",
        _id: int = None,
        name: str = None,
        start: bool = True,
        server: "DistributedServer" = None,
        policy: NetworkPolicy = None,
    ) -> None:
        """
        Creates a LxBrNet instance.

        :param session: core session instance
        :param _id: object id
        :param name: object name
        :param start: start flag
        :param server: remote server node
            will run on, default is None for localhost
        :param policy: network policy
        """
        super().__init__(session, _id, name, start, server)
        if name is None:
            name = str(self.id)
        if policy is not None:
            self.policy = policy
        self.name: Optional[str] = name
        sessionid = self.session.short_session_id()
        self.brname: str = f"b.{self.id}.{sessionid}"
        self.has_ebtables_chain: bool = False
        if start:
            self.startup()
            ebq.startupdateloop(self)

    def host_cmd(
        self,
        args: str,
        env: Dict[str, str] = None,
        cwd: str = None,
        wait: bool = True,
        shell: bool = False,
    ) -> str:
        """
        Runs a command that is used to configure and setup the network on the host
        system and all configured distributed servers.

        :param args: command to run
        :param env: environment to run command with
        :param cwd: directory to run command in
        :param wait: True to wait for status, False otherwise
        :param shell: True to use shell, False otherwise
        :return: combined stdout and stderr
        :raises CoreCommandError: when a non-zero exit status occurs
        """
        logging.debug("network node(%s) cmd", self.name)
        output = utils.cmd(args, env, cwd, wait, shell)
        self.session.distributed.execute(lambda x: x.remote_cmd(args, env, cwd, wait))
        return output

    def startup(self) -> None:
        """
        Linux bridge starup logic.

        :return: nothing
        :raises CoreCommandError: when there is a command exception
        """
        self.net_client.create_bridge(self.brname)
        self.has_ebtables_chain = False
        self.up = True

    def shutdown(self) -> None:
        """
        Linux bridge shutdown logic.

        :return: nothing
        """
        if not self.up:
            return

        ebq.stopupdateloop(self)

        try:
            self.net_client.delete_bridge(self.brname)
            if self.has_ebtables_chain:
                cmds = [
                    f"{EBTABLES_BIN} -D FORWARD --logical-in {self.brname} -j {self.brname}",
                    f"{EBTABLES_BIN} -X {self.brname}",
                ]
                ebtablescmds(self.host_cmd, cmds)
        except CoreCommandError:
            logging.exception("error during shutdown")

        # removes veth pairs used for bridge-to-bridge connections
        for netif in self.netifs():
            netif.shutdown()

        self._netif.clear()
        self._linked.clear()
        del self.session
        self.up = False

    def attach(self, netif: CoreInterface) -> None:
        """
        Attach a network interface.

        :param netif: network interface to attach
        :return: nothing
        """
        if self.up:
            netif.net_client.set_interface_master(self.brname, netif.localname)
        super().attach(netif)

    def detach(self, netif: CoreInterface) -> None:
        """
        Detach a network interface.

        :param netif: network interface to detach
        :return: nothing
        """
        if self.up:
            netif.net_client.delete_interface(self.brname, netif.localname)
        super().detach(netif)

    def linked(self, netif1: CoreInterface, netif2: CoreInterface) -> bool:
        """
        Determine if the provided network interfaces are linked.

        :param netif1: interface one
        :param netif2: interface two
        :return: True if interfaces are linked, False otherwise
        """
        # check if the network interfaces are attached to this network
        if self._netif[netif1.netifi] != netif1:
            raise ValueError(f"inconsistency for netif {netif1.name}")

        if self._netif[netif2.netifi] != netif2:
            raise ValueError(f"inconsistency for netif {netif2.name}")

        try:
            linked = self._linked[netif1][netif2]
        except KeyError:
            if self.policy == NetworkPolicy.ACCEPT:
                linked = True
            elif self.policy == NetworkPolicy.DROP:
                linked = False
            else:
                raise Exception(f"unknown policy: {self.policy.value}")
            self._linked[netif1][netif2] = linked

        return linked

    def unlink(self, netif1: CoreInterface, netif2: CoreInterface) -> None:
        """
        Unlink two interfaces, resulting in adding or removing ebtables
        filtering rules.

        :param netif1: interface one
        :param netif2: interface two
        :return: nothing
        """
        with self._linked_lock:
            if not self.linked(netif1, netif2):
                return
            self._linked[netif1][netif2] = False

        ebq.ebchange(self)

    def link(self, netif1: CoreInterface, netif2: CoreInterface) -> None:
        """
        Link two interfaces together, resulting in adding or removing
        ebtables filtering rules.

        :param netif1: interface one
        :param netif2: interface two
        :return: nothing
        """
        with self._linked_lock:
            if self.linked(netif1, netif2):
                return
            self._linked[netif1][netif2] = True

        ebq.ebchange(self)

    def linkconfig(
        self, netif: CoreInterface, options: LinkOptions, netif2: CoreInterface = None
    ) -> None:
        """
        Configure link parameters by applying tc queuing disciplines on the interface.

        :param netif: interface one
        :param options: options for configuring link
        :param netif2: interface two
        :return: nothing
        """
        devname = netif.localname
        tc = f"{TC_BIN} qdisc replace dev {devname}"
        parent = "root"
        changed = False
        bw = options.bandwidth
        if netif.setparam("bw", bw):
            # from tc-tbf(8): minimum value for burst is rate / kernel_hz
            burst = max(2 * netif.mtu, int(bw / 1000))
            # max IP payload
            limit = 0xFFFF
            tbf = f"tbf rate {bw} burst {burst} limit {limit}"
            if bw > 0:
                if self.up:
                    cmd = f"{tc} {parent} handle 1: {tbf}"
                    netif.host_cmd(cmd)
                netif.setparam("has_tbf", True)
                changed = True
            elif netif.getparam("has_tbf") and bw <= 0:
                if self.up:
                    cmd = f"{TC_BIN} qdisc delete dev {devname} {parent}"
                    netif.host_cmd(cmd)
                netif.setparam("has_tbf", False)
                # removing the parent removes the child
                netif.setparam("has_netem", False)
                changed = True
        if netif.getparam("has_tbf"):
            parent = "parent 1:1"
        netem = "netem"
        delay = options.delay
        changed = max(changed, netif.setparam("delay", delay))
        loss = options.per
        if loss is not None:
            loss = float(loss)
        changed = max(changed, netif.setparam("loss", loss))
        duplicate = options.dup
        if duplicate is not None:
            duplicate = int(duplicate)
        changed = max(changed, netif.setparam("duplicate", duplicate))
        jitter = options.jitter
        changed = max(changed, netif.setparam("jitter", jitter))
        if not changed:
            return
        # jitter and delay use the same delay statement
        if delay is not None:
            netem += f" delay {delay}us"
        if jitter is not None:
            if delay is None:
                netem += f" delay 0us {jitter}us 25%"
            else:
                netem += f" {jitter}us 25%"

        if loss is not None and loss > 0:
            netem += f" loss {min(loss, 100)}%"
        if duplicate is not None and duplicate > 0:
            netem += f" duplicate {min(duplicate, 100)}%"

        delay_check = delay is None or delay <= 0
        jitter_check = jitter is None or jitter <= 0
        loss_check = loss is None or loss <= 0
        duplicate_check = duplicate is None or duplicate <= 0
        if all([delay_check, jitter_check, loss_check, duplicate_check]):
            # possibly remove netem if it exists and parent queue wasn't removed
            if not netif.getparam("has_netem"):
                return
            if self.up:
                cmd = f"{TC_BIN} qdisc delete dev {devname} {parent} handle 10:"
                netif.host_cmd(cmd)
            netif.setparam("has_netem", False)
        elif len(netem) > 1:
            if self.up:
                cmd = (
                    f"{TC_BIN} qdisc replace dev {devname} {parent} handle 10: {netem}"
                )
                netif.host_cmd(cmd)
            netif.setparam("has_netem", True)

    def linknet(self, net: CoreNetworkBase) -> CoreInterface:
        """
        Link this bridge with another by creating a veth pair and installing
        each device into each bridge.

        :param net: network to link with
        :return: created interface
        """
        sessionid = self.session.short_session_id()
        try:
            _id = f"{self.id:x}"
        except TypeError:
            _id = str(self.id)

        try:
            net_id = f"{net.id:x}"
        except TypeError:
            net_id = str(net.id)

        localname = f"veth{_id}.{net_id}.{sessionid}"
        if len(localname) >= 16:
            raise ValueError(f"interface local name {localname} too long")

        name = f"veth{net_id}.{_id}.{sessionid}"
        if len(name) >= 16:
            raise ValueError(f"interface name {name} too long")

        netif = Veth(self.session, None, name, localname, start=self.up)
        self.attach(netif)
        if net.up and net.brname:
            netif.net_client.set_interface_master(net.brname, netif.name)
        i = net.newifindex()
        net._netif[i] = netif
        with net._linked_lock:
            net._linked[netif] = {}
        netif.net = self
        netif.othernet = net
        return netif

    def getlinknetif(self, net: CoreNetworkBase) -> Optional[CoreInterface]:
        """
        Return the interface of that links this net with another net
        (that were linked using linknet()).

        :param net: interface to get link for
        :return: interface the provided network is linked to
        """
        for netif in self.netifs():
            if netif.othernet == net:
                return netif
        return None

    def addrconfig(self, addrlist: List[str]) -> None:
        """
        Set addresses on the bridge.

        :param addrlist: address list
        :return: nothing
        """
        if not self.up:
            return

        for addr in addrlist:
            self.net_client.create_address(self.brname, str(addr))


class GreTapBridge(CoreNetwork):
    """
    A network consisting of a bridge with a gretap device for tunneling to
    another system.
    """

    def __init__(
        self,
        session: "Session",
        remoteip: str = None,
        _id: int = None,
        name: str = None,
        policy: NetworkPolicy = NetworkPolicy.ACCEPT,
        localip: str = None,
        ttl: int = 255,
        key: int = None,
        start: bool = True,
        server: "DistributedServer" = None,
    ) -> None:
        """
        Create a GreTapBridge instance.

        :param session: core session instance
        :param remoteip: remote address
        :param _id: object id
        :param name: object name
        :param policy: network policy
        :param localip: local address
        :param ttl: ttl value
        :param key: gre tap key
        :param start: start flag
        :param server: remote server node
            will run on, default is None for localhost
        """
        CoreNetwork.__init__(self, session, _id, name, False, server, policy)
        if key is None:
            key = self.session.id ^ self.id
        self.grekey: int = key
        self.localnum: Optional[int] = None
        self.remotenum: Optional[int] = None
        self.remoteip: Optional[str] = remoteip
        self.localip: Optional[str] = localip
        self.ttl: int = ttl
        self.gretap: Optional[GreTap] = None
        if remoteip is not None:
            self.gretap = GreTap(
                node=self,
                session=session,
                remoteip=remoteip,
                localip=localip,
                ttl=ttl,
                key=self.grekey,
            )
        if start:
            self.startup()

    def startup(self) -> None:
        """
        Creates a bridge and adds the gretap device to it.

        :return: nothing
        """
        super().startup()
        if self.gretap:
            self.attach(self.gretap)

    def shutdown(self) -> None:
        """
        Detach the gretap device and remove the bridge.

        :return: nothing
        """
        if self.gretap:
            self.detach(self.gretap)
            self.gretap.shutdown()
            self.gretap = None
        super().shutdown()

    def addrconfig(self, addrlist: List[str]) -> None:
        """
        Set the remote tunnel endpoint. This is a one-time method for
        creating the GreTap device, which requires the remoteip at startup.
        The 1st address in the provided list is remoteip, 2nd optionally
        specifies localip.

        :param addrlist: address list
        :return: nothing
        """
        if self.gretap:
            raise ValueError(f"gretap already exists for {self.name}")
        remoteip = addrlist[0].split("/")[0]
        localip = None
        if len(addrlist) > 1:
            localip = addrlist[1].split("/")[0]
        self.gretap = GreTap(
            session=self.session,
            remoteip=remoteip,
            localip=localip,
            ttl=self.ttl,
            key=self.grekey,
        )
        self.attach(self.gretap)

    def setkey(self, key: int) -> None:
        """
        Set the GRE key used for the GreTap device. This needs to be set
        prior to instantiating the GreTap device (before addrconfig).

        :param key: gre key
        :return: nothing
        """
        self.grekey = key


class CtrlNet(CoreNetwork):
    """
    Control network functionality.
    """

    policy: NetworkPolicy = NetworkPolicy.ACCEPT
    # base control interface index
    CTRLIF_IDX_BASE: int = 99
    DEFAULT_PREFIX_LIST: List[str] = [
        "172.16.0.0/24 172.16.1.0/24 172.16.2.0/24 172.16.3.0/24 172.16.4.0/24",
        "172.17.0.0/24 172.17.1.0/24 172.17.2.0/24 172.17.3.0/24 172.17.4.0/24",
        "172.18.0.0/24 172.18.1.0/24 172.18.2.0/24 172.18.3.0/24 172.18.4.0/24",
        "172.19.0.0/24 172.19.1.0/24 172.19.2.0/24 172.19.3.0/24 172.19.4.0/24",
    ]

    def __init__(
        self,
        session: "Session",
        prefix: str,
        _id: int = None,
        name: str = None,
        hostid: int = None,
        start: bool = True,
        server: "DistributedServer" = None,
        assign_address: bool = True,
        updown_script: str = None,
        serverintf: str = None,
    ) -> None:
        """
        Creates a CtrlNet instance.

        :param session: core session instance
        :param _id: node id
        :param name: node namee
        :param prefix: control network ipv4 prefix
        :param hostid: host id
        :param start: start flag
        :param server: remote server node
            will run on, default is None for localhost
        :param assign_address: assigned address
        :param updown_script: updown script
        :param serverintf: server interface
        :return:
        """
        self.prefix: netaddr.IPNetwork = netaddr.IPNetwork(prefix).cidr
        self.hostid: Optional[int] = hostid
        self.assign_address: bool = assign_address
        self.updown_script: Optional[str] = updown_script
        self.serverintf: Optional[str] = serverintf
        super().__init__(session, _id, name, start, server)

    def add_addresses(self, index: int) -> None:
        """
        Add addresses used for created control networks,

        :param index: starting address index
        :return: nothing
        """
        use_ovs = self.session.options.get_config("ovs") == "True"
        address = self.prefix[index]
        current = f"{address}/{self.prefix.prefixlen}"
        net_client = get_net_client(use_ovs, utils.cmd)
        net_client.create_address(self.brname, current)
        servers = self.session.distributed.servers
        for name in servers:
            server = servers[name]
            index -= 1
            address = self.prefix[index]
            current = f"{address}/{self.prefix.prefixlen}"
            net_client = get_net_client(use_ovs, server.remote_cmd)
            net_client.create_address(self.brname, current)

    def startup(self) -> None:
        """
        Startup functionality for the control network.

        :return: nothing
        :raises CoreCommandError: when there is a command exception
        """
        if self.net_client.existing_bridges(self.id):
            raise CoreError(f"old bridges exist for node: {self.id}")

        super().startup()
        logging.info("added control network bridge: %s %s", self.brname, self.prefix)

        if self.hostid and self.assign_address:
            self.add_addresses(self.hostid)
        elif self.assign_address:
            self.add_addresses(-2)

        if self.updown_script:
            logging.info(
                "interface %s updown script (%s startup) called",
                self.brname,
                self.updown_script,
            )
            self.host_cmd(f"{self.updown_script} {self.brname} startup")

        if self.serverintf:
            self.net_client.set_interface_master(self.brname, self.serverintf)

    def shutdown(self) -> None:
        """
        Control network shutdown.

        :return: nothing
        """
        if self.serverintf is not None:
            try:
                self.net_client.delete_interface(self.brname, self.serverintf)
            except CoreCommandError:
                logging.exception(
                    "error deleting server interface %s from bridge %s",
                    self.serverintf,
                    self.brname,
                )

        if self.updown_script is not None:
            try:
                logging.info(
                    "interface %s updown script (%s shutdown) called",
                    self.brname,
                    self.updown_script,
                )
                self.host_cmd(f"{self.updown_script} {self.brname} shutdown")
            except CoreCommandError:
                logging.exception("error issuing shutdown script shutdown")

        super().shutdown()

    def all_link_data(self, flags: MessageFlags = MessageFlags.NONE) -> List[LinkData]:
        """
        Do not include CtrlNet in link messages describing this session.

        :param flags: message flags
        :return: list of link data
        """
        return []


class PtpNet(CoreNetwork):
    """
    Peer to peer network node.
    """

    policy: NetworkPolicy = NetworkPolicy.ACCEPT

    def attach(self, netif: CoreInterface) -> None:
        """
        Attach a network interface, but limit attachment to two interfaces.

        :param netif: network interface
        :return: nothing
        """
        if len(self._netif) >= 2:
            raise ValueError(
                "Point-to-point links support at most 2 network interfaces"
            )
        super().attach(netif)

    def data(
        self, message_type: MessageFlags = MessageFlags.NONE, source: str = None
    ) -> Optional[NodeData]:
        """
        Do not generate a Node Message for point-to-point links. They are
        built using a link message instead.

        :param message_type: purpose for the data object we are creating
        :param source: source of node data
        :return: node data object
        """
        return None

    def all_link_data(self, flags: MessageFlags = MessageFlags.NONE) -> List[LinkData]:
        """
        Build CORE API TLVs for a point-to-point link. One Link message
        describes this network.

        :param flags: message flags
        :return: list of link data
        """
        all_links = []

        if len(self._netif) != 2:
            return all_links

        if1, if2 = self._netif.values()
        unidirectional = 0
        if if1.getparams() != if2.getparams():
            unidirectional = 1

        interface1_ip4 = None
        interface1_ip4_mask = None
        interface1_ip6 = None
        interface1_ip6_mask = None
        for address in if1.addrlist:
            ip, _sep, mask = address.partition("/")
            mask = int(mask)
            if netaddr.valid_ipv4(ip):
                interface1_ip4 = ip
                interface1_ip4_mask = mask
            else:
                interface1_ip6 = ip
                interface1_ip6_mask = mask

        interface2_ip4 = None
        interface2_ip4_mask = None
        interface2_ip6 = None
        interface2_ip6_mask = None
        for address in if2.addrlist:
            ip, _sep, mask = address.partition("/")
            mask = int(mask)
            if netaddr.valid_ipv4(ip):
                interface2_ip4 = ip
                interface2_ip4_mask = mask
            else:
                interface2_ip6 = ip
                interface2_ip6_mask = mask

        link_data = LinkData(
            message_type=flags,
            node1_id=if1.node.id,
            node2_id=if2.node.id,
            link_type=self.linktype,
            unidirectional=unidirectional,
            delay=if1.getparam("delay"),
            bandwidth=if1.getparam("bw"),
            per=if1.getparam("loss"),
            dup=if1.getparam("duplicate"),
            jitter=if1.getparam("jitter"),
            interface1_id=if1.node.getifindex(if1),
            interface1_name=if1.name,
            interface1_mac=if1.hwaddr,
            interface1_ip4=interface1_ip4,
            interface1_ip4_mask=interface1_ip4_mask,
            interface1_ip6=interface1_ip6,
            interface1_ip6_mask=interface1_ip6_mask,
            interface2_id=if2.node.getifindex(if2),
            interface2_name=if2.name,
            interface2_mac=if2.hwaddr,
            interface2_ip4=interface2_ip4,
            interface2_ip4_mask=interface2_ip4_mask,
            interface2_ip6=interface2_ip6,
            interface2_ip6_mask=interface2_ip6_mask,
        )

        all_links.append(link_data)

        # build a 2nd link message for the upstream link parameters
        # (swap if1 and if2)
        if unidirectional:
            link_data = LinkData(
                message_type=MessageFlags.NONE,
                link_type=self.linktype,
                node1_id=if2.node.id,
                node2_id=if1.node.id,
                delay=if2.getparam("delay"),
                bandwidth=if2.getparam("bw"),
                per=if2.getparam("loss"),
                dup=if2.getparam("duplicate"),
                jitter=if2.getparam("jitter"),
                unidirectional=1,
                interface1_id=if2.node.getifindex(if2),
                interface2_id=if1.node.getifindex(if1),
            )
            all_links.append(link_data)

        return all_links


class SwitchNode(CoreNetwork):
    """
    Provides switch functionality within a core node.
    """

    apitype: NodeTypes = NodeTypes.SWITCH
    policy: NetworkPolicy = NetworkPolicy.ACCEPT
    type: str = "lanswitch"


class HubNode(CoreNetwork):
    """
    Provides hub functionality within a core node, forwards packets to all bridge
    ports by turning off MAC address learning.
    """

    apitype: NodeTypes = NodeTypes.HUB
    policy: NetworkPolicy = NetworkPolicy.ACCEPT
    type: str = "hub"

    def startup(self) -> None:
        """
        Startup for a hub node, that disables mac learning after normal startup.

        :return: nothing
        """
        super().startup()
        self.net_client.disable_mac_learning(self.brname)


class WlanNode(CoreNetwork):
    """
    Provides wireless lan functionality within a core node.
    """

    apitype: NodeTypes = NodeTypes.WIRELESS_LAN
    linktype: LinkTypes = LinkTypes.WIRED
    policy: NetworkPolicy = NetworkPolicy.DROP
    type: str = "wlan"

    def __init__(
        self,
        session: "Session",
        _id: int = None,
        name: str = None,
        start: bool = True,
        server: "DistributedServer" = None,
        policy: NetworkPolicy = None,
    ) -> None:
        """
        Create a WlanNode instance.

        :param session: core session instance
        :param _id: node id
        :param name: node name
        :param start: start flag
        :param server: remote server node
            will run on, default is None for localhost
        :param policy: wlan policy
        """
        super().__init__(session, _id, name, start, server, policy)
        # wireless and mobility models (BasicRangeModel, Ns2WaypointMobility)
        self.model: Optional[WirelessModel] = None
        self.mobility: Optional[WayPointMobility] = None

    def startup(self) -> None:
        """
        Startup for a wlan node, that disables mac learning after normal startup.

        :return: nothing
        """
        super().startup()
        self.net_client.disable_mac_learning(self.brname)
        ebq.ebchange(self)

    def attach(self, netif: CoreInterface) -> None:
        """
        Attach a network interface.

        :param netif: network interface
        :return: nothing
        """
        super().attach(netif)
        if self.model:
            netif.poshook = self.model.position_callback
            netif.setposition()

    def setmodel(self, model: "WirelessModelType", config: Dict[str, str]):
        """
        Sets the mobility and wireless model.

        :param model: wireless model to set to
        :param config: configuration for model being set
        :return: nothing
        """
        logging.debug("node(%s) setting model: %s", self.name, model.name)
        if model.config_type == RegisterTlvs.WIRELESS:
            self.model = model(session=self.session, _id=self.id)
            for netif in self.netifs():
                netif.poshook = self.model.position_callback
                netif.setposition()
            self.updatemodel(config)
        elif model.config_type == RegisterTlvs.MOBILITY:
            self.mobility = model(session=self.session, _id=self.id)
            self.mobility.update_config(config)

    def update_mobility(self, config: Dict[str, str]) -> None:
        if not self.mobility:
            raise ValueError(f"no mobility set to update for node({self.id})")
        self.mobility.update_config(config)

    def updatemodel(self, config: Dict[str, str]) -> None:
        if not self.model:
            raise ValueError(f"no model set to update for node({self.id})")
        logging.debug(
            "node(%s) updating model(%s): %s", self.id, self.model.name, config
        )
        self.model.update_config(config)
        for netif in self.netifs():
            netif.setposition()

    def all_link_data(self, flags: MessageFlags = MessageFlags.NONE) -> List[LinkData]:
        """
        Retrieve all link data.

        :param flags: message flags
        :return: list of link data
        """
        all_links = super().all_link_data(flags)
        if self.model:
            all_links.extend(self.model.all_link_data(flags))
        return all_links


class TunnelNode(GreTapBridge):
    """
    Provides tunnel functionality in a core node.
    """

    apitype: NodeTypes = NodeTypes.TUNNEL
    policy: NetworkPolicy = NetworkPolicy.ACCEPT
    type: str = "tunnel"
