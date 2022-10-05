# vim: set et sw=4 ts=4:
import argparse
import json
import logging
import os
import select
import socket
import subprocess
import sys
import tempfile
import time


logging.basicConfig(level=logging.INFO)


def temp_socket_name():
    return os.path.join(tempfile.mkdtemp(), "slirp4netns.sock")


def build_bwrap_cmd(namespaced_cmd, fd_userns_block_r, fd_info_w):
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
        "--userns-block-fd",
        str(fd_userns_block_r),
        "--die-with-parent",
        "--info-fd",
        str(fd_info_w),
    ]
    bwrap_cmd = ["bwrap"] + bwrap_prefix + namespaced_cmd

    return bwrap_cmd


def slirp4netns(bwrapped_pid):
    """Launch slirp4netns."""
    sock = temp_socket_name()
    cmd = [
        "slirp4netns",
        "--configure",
        "--mtu=65520",
        "--disable-host-loopback",
        "--api-socket",
        sock,
        str(bwrapped_pid),
        "tap0",
    ]
    logging.info(f'Running: {" ".join(cmd)}')
    p = subprocess.Popen(cmd)
    while True:
        if os.path.exists(sock):
            break
        time.sleep(0.1)
    time.sleep(2)
    # Return the process to keep it running
    return p, sock


def forward(host_port, guest_port, slirp_sock):
    """
    Create a slirp4netns forwarding rule from the host to the jupyter server.
    """
    rule = {
        "execute": "add_hostfwd",
        "arguments": {
            "proto": "tcp",
            "host_addr": "0.0.0.0",
            "host_port": host_port,
            "guest_addr": "10.0.2.100",
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


def usage():
    return """Usage: portwrap [-h] -p HOST_PORT -P GUEST_PORT COMMAND [COMMAND ...]
    """


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


def portwrap(host_port, guest_port, command):
    """
    Run a command in a user and network namespace, forwarding traffic from
    a host port to a port in the namespace.
    """
    namespaced_cmd = build_namespaced_cmd(command, guest_port)

    # to receive information about the running container
    fd_info_r, fd_info_w = os.pipe()

    # to block until namespace is ready
    fd_userns_block_r, fd_userns_block_w = os.pipe()

    pid = os.fork()

    if pid != 0:  # Parent
        logging.info('parent starting')
        # We don't write to this fd
        os.close(fd_info_w)
        # We don't read from this fd
        os.close(fd_userns_block_r)

        # Read the wrapped process's pid
        select.select([fd_info_r], [], [])
        data = json.load(os.fdopen(fd_info_r))
        child_pid = str(data["child-pid"])

        # Run slirp4netns
        logging.info(f'parent starting slirp4netns with {child_pid=}')
        slirp_p, slirp_sock = slirp4netns(child_pid)

        # Forward traffic from host to guest
        logging.info(f'parent forwarding from {host_port=} to {guest_port=}')
        forward(host_port, guest_port, slirp_sock)

        # Stop blocking
        logging.info(f'parent unblocking')
        os.write(fd_userns_block_w, b"1")

        logging.info('parent finished')
    else:  # Child
        # Ignore info's read fd
        os.close(fd_info_r)

        # Ignore userns_block's write fd
        os.close(fd_userns_block_w)

        os.set_inheritable(fd_info_w, True)
        os.set_inheritable(fd_userns_block_r, True)

        bwrap_cmd = build_bwrap_cmd(namespaced_cmd, fd_userns_block_r, fd_info_w)
        logging.info(f'child execlp: {bwrap_cmd}')
        os.execlp(*bwrap_cmd)


def main():
    parser = argparse.ArgumentParser(usage=usage())
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

    portwrap(args.host_port, args.guest_port, remainder)


if __name__ == "__main__":
    main()
