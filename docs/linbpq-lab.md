# Local LinBPQ test node

The [Modern Ham installation guide](https://themodernham.com/install-linbpq-bbs-packet-node-on-debian-ubuntu-and-raspbian/) is useful for a permanent Debian or Raspberry Pi radio node. For pyBulletin's TCP forwarding tests, a smaller LinBPQ instance is enough. The lab script uses the [official x86-64 LinBPQ download](https://www.cantab.net/users/john.wiseman/Downloads/) and runs it in a private Linux user/network namespace. It does not configure a radio, system service, or public listener.

On a Linux x86-64 machine with `curl`, `unzip`, `unshare`, and `ip` installed:

```sh
bash scripts/linbpq_lab.sh setup
bash scripts/linbpq_lab.sh shell
```

`setup` prepares `/tmp/pybulletin-linbpq-lab`; set `LINBPQ_LAB_DIR` to use a different directory. `shell` starts LinBPQ and opens a shell in the same private network namespace, where you can run pyBulletin or a test client. Exit the shell to stop the node. `run` starts only LinBPQ in the foreground. Inside the namespace, the node exposes Telnet on port 18010, FBB Telnet on 18011, and its web interface on 18080. The sample login is `lab` / `labpass`, mapped to test callsign `N1CALL`. Change those values in `bpq32.cfg` before using any shared network. The private namespace keeps these ports off the host network.

LinBPQ creates `linmail.cfg` on first launch. Configure `N1CALL` as a BBS in its web Users page. In its Forwarding page, give `N1CALL` a BBS record with a BBS HA, enable FBB Blocked, Allow Binary, and B1. Without this record, the lab's SID omits `F` and pyBulletin correctly refuses to forward.

The FBB port expects a user and password before it sends the BBS greeting and SID. In pyBulletin's neighbor settings, use TCP address `127.0.0.1:18011`, login user `lab`, login password `labpass`, and **FBB compressed (B0)**. The connector handles BPQ's login and greeting. Live tests with LinBPQ 6.0.26.1 confirmed a pyBulletin bulletin arrived with the correct subject and body, and two BPQ private messages reached pyBulletin with intact subjects and bodies. The earlier FBB ASCII format remains available for older peers; LinBPQ 6.0.26.1 removed uncompressed blocked forwarding. B1 restart support and RF remain to be validated. See [forwarding validation](forwarding-validation.md) for the remaining checks.

The compressed B0 mode follows the [F6FBB forwarding specification](https://www.f6fbb.org/protocole.html): `BF` SID capability, `FA` proposals, optional proposal checksums, LZHUF message compression, and binary block checksums. A second live test sent a 2.9 KB bulletin across multiple compressed blocks and verified its stored text byte for byte. Compatibility with the LinFBB application itself has not yet been tested; B1 resume and B2 are not advertised.

If `unshare` or `ip link set lo up` fails, the host has disabled unprivileged network namespaces. In that case, use a container or VM with private networking. Do not run the sample configuration directly on a public interface.
