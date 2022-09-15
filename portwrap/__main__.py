# vim: set et sw=4 ts=4:
import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time


logging.basicConfig(level=logging.INFO)


def temp_socket_name():
    return os.path.join(tempfile.mkdtemp(), "slirp4netns.sock")


def bwrap():
    """
    Create a new network and user namespace with bwrap, and
    return the PID of the wrapped process, not of bwrap itself.
    """
    # We cannot wait for the process to terminate to collect stdout,
    # so we write the PID to a temporary file.
    f = tempfile.NamedTemporaryFile()
    pid_file = f.name
    bwrap_cmd = [
        "bwrap",
        "--dev-bind",
        "/",
        "/",
        "--unshare-net",
        "--die-with-parent",
        "sh",
        "-c",
        f"echo $$ > {pid_file}; while true ; do sleep 600 ; done",
    ]
    p = subprocess.Popen(bwrap_cmd)
    while True:
        lines = open(pid_file).readlines()
        # wait for pid file to contain pid
        if len(lines) == 0:
            time.sleep(0.1)
            continue
        bwrapped_pid = lines[0].strip()
        break
    logging.info(f"Wrapping PID: {bwrapped_pid}")
    return bwrapped_pid


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
    logging.debug('Running: {" ".join(cmd)}')
    p = subprocess.Popen(cmd)
    while True:
        if os.path.exists(sock):
            break
        time.sleep(0.1)
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
    logging.debug(rule)
    # Communicate the rule to slirp4netns
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(slirp_sock)
        n = client.send(json.dumps(rule).encode())
        # slirp4netns needs us to read the response, otherwise its return code
        # will be -1 and the forwarding won't work. (despite what the rules
        # list says)
        recv = client.recv(1024)
        client.close()
    logging.debug(recv)


def usage():
    return """Usage: portwrap [-h] -p HOST_PORT -P GUEST_PORT COMMAND [COMMAND ...]
    """


def portwrap(host_port, guest_port, command):
    """
    Run a command in a user and network namespace, forwarding traffic from
    a host port to a port in the namespace.
    """
    namespaced_cmd = []
    for arg in command:
        if "{guest-port}" in arg:
            arg = arg.replace("{guest-port}", str(guest_port))
        namespaced_cmd.append(arg)

    bwrapped_pid = bwrap()

    slirp_p, slirp_sock = slirp4netns(bwrapped_pid)

    forward(host_port, guest_port, slirp_sock)

    # Start jupyter in a namespace
    nsenter_cmd = [
        "nsenter",
        "--preserve-credentials",
        "-t",
        str(bwrapped_pid),
        "--net",
        "--user",
    ]
    cmd = nsenter_cmd + namespaced_cmd
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt as e:
        pass


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


if __name__ == '__main__':
    main()
