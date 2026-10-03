# vim: set et sw=4 ts=4:
import argparse
import ipaddress
import json
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time


logging.basicConfig(level=logging.INFO)

# The network slirp4netns creates in the namespace. It provides a DNS
# forwarder at .3 and assigns the guest .100.
SLIRP_CIDR = ipaddress.ip_network("10.0.2.0/24")
SLIRP_DNS = str(SLIRP_CIDR.network_address + 3)
SLIRP_GUEST = str(SLIRP_CIDR.network_address + 100)
RESOLV_CONF = "/etc/resolv.conf"


def temp_socket_name():
    return os.path.join(tempfile.mkdtemp(), "slirp4netns.sock")


def is_loopback(addr):
    """Return True if addr, e.g. from a resolv.conf nameserver line, is loopback."""
    try:
        # strip any IPv6 zone, e.g. fe80::1%eth0
        return ipaddress.ip_address(addr.split("%")[0]).is_loopback
    except ValueError:
        return False


def make_resolv_conf():
    """
    If every nameserver in the host's resolv.conf is a loopback address
    (e.g. systemd-resolved's 127.0.0.53), it will be unreachable inside the
    network namespace. In that case write a copy that uses slirp4netns's DNS
    forwarder instead, preserving search, domain, and options lines.

    Return the path of the new file, or None if the host's file can be used
    as is.
    """
    try:
        with open(RESOLV_CONF) as f:
            lines = f.readlines()
    except OSError:
        return None

    nameservers = [
        line.split()[1]
        for line in lines
        if line.split()[:1] == ["nameserver"] and len(line.split()) > 1
    ]
    if nameservers and not all(is_loopback(ns) for ns in nameservers):
        return None

    new_lines = [f"nameserver {SLIRP_DNS}\n"]
    new_lines += [line for line in lines if line.split()[:1] != ["nameserver"]]

    path = os.path.join(tempfile.mkdtemp(), "resolv.conf")
    with open(path, "w") as f:
        f.writelines(new_lines)
    logging.info(f"using {path} for {RESOLV_CONF}: host nameservers {nameservers}")
    return path


def build_bwrap_cmd(namespaced_cmd, fd_info_w, fd_block_r, resolv_conf):
    """
    Create a new network and user namespace with bwrap, and
    return the PID of the wrapped process.
    """
    bwrap_prefix = [
        "bwrap",
        "--dev-bind",
        "/",
        "/",
        "--unshare-net",
        "--unshare-user",
        "--die-with-parent",
        "--info-fd",
        str(fd_info_w),
        # don't run the command until the network is ready
        "--block-fd",
        str(fd_block_r),
    ]
    if resolv_conf is not None:
        # /etc/resolv.conf is often a symlink, e.g. into /run/systemd/resolve
        bwrap_prefix += ["--ro-bind", resolv_conf, os.path.realpath(RESOLV_CONF)]
    bwrap_cmd = ["bwrap"] + bwrap_prefix + namespaced_cmd

    return bwrap_cmd


def slirp4netns(bwrapped_pid):
    """Launch slirp4netns."""
    sock = temp_socket_name()
    # slirp4netns writes to this fd once the interface is configured
    fd_ready_r, fd_ready_w = os.pipe()
    cmd = [
        "slirp4netns",
        "--configure",
        "--mtu=65520",
        "--disable-host-loopback",
        "--cidr",
        str(SLIRP_CIDR),
        "--api-socket",
        sock,
        "--ready-fd",
        str(fd_ready_w),
        str(bwrapped_pid),
        "tap0",
    ]
    logging.info(f'Running: {" ".join(cmd)}')
    p = subprocess.Popen(cmd, pass_fds=[fd_ready_w])
    os.close(fd_ready_w)
    # Blocks until slirp4netns is ready, or returns b"" if it exits first
    ready = os.read(fd_ready_r, 1)
    os.close(fd_ready_r)
    if not ready:
        raise RuntimeError(f"slirp4netns exited with {p.wait()}")
    while not os.path.exists(sock):
        time.sleep(0.1)
    # Return the process to keep it running
    return p, sock


def forward(host_addr, host_port, guest_port, slirp_sock):
    """
    Create a slirp4netns forwarding rule from the host to the jupyter server.
    """
    rule = {
        "execute": "add_hostfwd",
        "arguments": {
            "proto": "tcp",
            "host_addr": host_addr,
            "host_port": host_port,
            "guest_addr": SLIRP_GUEST,
            "guest_port": guest_port,
        },
    }
    logging.info(rule)
    # Communicate the rule to slirp4netns
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(slirp_sock)
        n = client.send(json.dumps(rule).encode())
        # slirp4netns needs us to read the response, otherwise its return code
        # will be -1 and the forwarding won't work. (despite what the rules
        # list says)
        recv = client.recv(1024)
        client.close()
    logging.info(recv)


def ip_address(addr):
    """argparse type for an IP address."""
    try:
        return str(ipaddress.ip_address(addr))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an IP address: {addr!r}")


def usage():
    return "portwrap [-h] [--host-addr HOST_ADDR] -p HOST_PORT -P GUEST_PORT COMMAND [COMMAND_ARG ...]"


def build_namespaced_cmd(command, guest_port):
    """
    Use specified command as a template to construct a new command.
    """
    namespaced_cmd = []
    for arg in command:
        if "{guest-port}" in arg:
            arg = arg.replace("{guest-port}", str(guest_port))
        namespaced_cmd.append(arg)
    return namespaced_cmd


def read_bwrap_info_fd(fd):
    """Return bwrap's child pid."""
    select.select([fd], [], [])
    data = json.load(os.fdopen(fd))
    return str(data["child-pid"])


def stop_slirp4netns(proc):
    """Shutdown slirp4netns."""
    if proc is not None:
        proc.kill()


def portwrap(host_addr, host_port, guest_port, command):
    """
    Run a command in a user and network namespace, forwarding traffic from
    a host port to a port in the namespace.
    """
    slirp_p = None

    def handler(signum, frame):
        """Catch SIGINT (e.g. keyboard interrupt) and kill slirp4netns."""
        logging.info(f"Signal handler called with signal {signum}")
        stop_slirp4netns(slirp_p)

    signal.signal(signal.SIGINT, handler)

    namespaced_cmd = build_namespaced_cmd(command, guest_port)

    resolv_conf = make_resolv_conf()

    # to receive information about the running container
    fd_info_r, fd_info_w = os.pipe()
    # to tell bwrap to run the command once the network is ready
    fd_block_r, fd_block_w = os.pipe()

    pid = os.fork()

    if pid != 0:  # Parent
        logging.info("parent starting")
        # We don't write to this fd or read from this one
        os.close(fd_info_w)
        os.close(fd_block_r)

        # Read the wrapped process's pid
        child_pid = read_bwrap_info_fd(fd_info_r)

        # Run slirp4netns
        logging.info(f"parent starting slirp4netns with {child_pid=}")
        slirp_p, slirp_sock = slirp4netns(child_pid)

        # Forward traffic from host to guest
        logging.info(
            f"parent forwarding from {host_addr=} {host_port=} to {guest_port=}"
        )
        forward(host_addr, host_port, guest_port, slirp_sock)

        # Let bwrap run the command
        os.write(fd_block_w, b"1")
        os.close(fd_block_w)

        logging.info("parent finished")
    else:  # Child
        # Ignore info's read fd and block's write fd
        logging.info(f"child starting")
        os.close(fd_info_r)
        os.close(fd_block_w)

        os.set_inheritable(fd_info_w, True)
        os.set_inheritable(fd_block_r, True)

        bwrap_cmd = build_bwrap_cmd(
            namespaced_cmd, fd_info_w, fd_block_r, resolv_conf
        )
        logging.info(f"child execlp: {bwrap_cmd}")
        os.execlp(*bwrap_cmd)

    # attempt to wait on bwrap to finish
    logging.info(f"calling waitpid {pid}")
    os.waitpid(pid, 0)
    stop_slirp4netns(slirp_p)


def main():
    parser = argparse.ArgumentParser(usage=usage())
    parser.add_argument(
        "--host-addr",
        dest="host_addr",
        default="0.0.0.0",
        type=ip_address,
        help="Host address to listen on (default: 0.0.0.0, all interfaces)",
    )
    parser.add_argument(
        "-p",
        "--host-port",
        dest="host_port",
        required=True,
        type=int,
        help="Host-accessible port",
    )
    parser.add_argument(
        "-P",
        "--guest-port",
        dest="guest_port",
        required=True,
        type=int,
        help="Namespace-accessible port",
    )
    args, remainder = parser.parse_known_args()

    if len(remainder) == 0:
        parser.print_usage()
        sys.exit(1)

    portwrap(args.host_addr, args.host_port, args.guest_port, remainder)


if __name__ == "__main__":
    main()
