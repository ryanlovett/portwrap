portwrap
========
portwrap launches a specified program into a user and network namespace. It
routes traffic to the program from a host port to the namespace's guest port.
If other programs are started in the namespace, their open ports will only be
accessible to the namespace and not to the host.

portwrap is a python script that calls nsenter, slirp4netns, and bubblewrap
(bwrap).

Usage
-----
```console
% ~/.local/bin/portwrap --help
usage: Usage: portwrap [-h] -p HOST_PORT -P GUEST_PORT COMMAND [COMMAND ...]


optional arguments:
  -h, --help            show this help message and exit
  -p HOST_PORT, --host-port HOST_PORT
                        Host-accessible port
  -P GUEST_PORT, --guest-port GUEST_PORT
                        Namespace-accessible port
```
