# BPQ and RF forwarding validation

The automated TCP and simulated KISS TCP tests in `tests/test_forward_integration.py`
exchange mail between two pyBulletin nodes. They verify session ordering, BID duplicate
handling, per-neighbor delivery state, schema upgrade, and backup restoration.
The KISS test carries AX.25 frames in both directions and triggers the outbound
session through the same local control socket used by the SYSOP Connect RF action.
The local TCP lab has also exchanged a bulletin and private messages in both
directions with LinBPQ 6.0.26.1 using compressed B0. Run the remaining checks
below before treating a BPQ link or an RF path as production-ready.

For a local TCP-only LinBPQ instance, see the [LinBPQ lab](linbpq-lab.md).

## Prepare

1. Back up the live database with `python scripts/backup.py --config config/pybulletin.toml --dest /path/to/backups`.
2. Configure both ends with distinct callsigns. Add the BPQ callsign to
   `[[forward.neighbor]]` and enable FBB Blocked, Allow Binary, and B1 in
   BPQ's per-BBS forwarding record. Set
   `protocol = "b0"`, TCP login credentials when using BPQ FBBPORT, and
   `inbound_host` to BPQ's source IP when BPQ's SID
   omits the BBS callsign.
   Restart `pybulletin.service` after changing neighbor configuration in the
   web console so the core listener, scheduler, and Connect RF action use the
   new settings.
   Use a reachable
   `host:port` for TCP, or a callsign for outbound AX.25 from `serve-core`.
3. Keep `allow_unknown_peers = false`; add each inbound peer explicitly.
   Callsign matching is an identity check, not cryptographic authentication.
   Restrict the forwarding TCP port to trusted peers at the network boundary.
4. Record pyBulletin and BPQ versions, callsigns, route, modem/TNC, radio,
   PACLEN, and transport settings alongside the results.

## Exchange matrix

For each direction, send one private message and one bulletin with known BIDs.
Confirm subject, body, sender, recipient, and BID on the receiving node.
After the receiver's reverse proposal or `FF`, the sender's pending count
should fall. Repeat the session to confirm it does not resend. Add a second
neighbor and verify the
bulletin reaches both. A private message should go only to its addressed BBS.

Repeat with an empty batch, a rejected proposal, a disconnect during
the body, and a modem restart. After a partial transfer the message must remain
pending and succeed on a later attempt. Inspect `/api/neighbors` as sysop for
pending count, last attempt, last success, and last error. Check that the web
console shows the same information.

## RF and recovery

Use Dire Wolf or a hardware KISS TNC for the first RF test. Check connect,
login, read, send, disconnect, and forwarding in both directions. Restart the
modem and the BBS while mail is pending; verify automatic recovery and no
lost or duplicate delivered messages. Repeat after an audio device disappears
and returns. Run unattended for one week, reviewing pending counts and errors
daily. Native AFSK requires separate noisy-channel and DCD testing.

Restore a backup into a separate directory and open it with pyBulletin. Verify
message count, a sample body, neighbor statistics, and delivery state. Do not
replace the live database during this check.

The acceptance record should include a timestamped log from each side and a
table of expected and observed BIDs. A successful connection alone is not a
successful forwarding test.
