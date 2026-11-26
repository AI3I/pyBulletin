# Kantronics KPC-9612 classic forwarding lab

The KPC-9612 on `termie` runs ROM 8.2. On 2026-11-25, its serial terminal
was used to connect internally to PBBS `AI3I-1`. This requires no radio and
does not exercise AX.25 over RF. The unit remained in `INTFACE TERMINAL`.

Its SID is `[KPC9612-8.2-HM$]`. After receiving
`[AI3I-14-pyBulletin1.0-HM$]`, it changed to the short BBS prompt `>`.
The following exchange was observed with one test private message:

```text
caller: SP AI3I @ AI3I-14 < AI3I $KPCREV02
KPC:    OK
caller: KPC reverse capture
caller: Local no-RF test only
caller: ^Z
KPC:    >
caller: F>
KPC:    SP AI3I @ AI3I-14 < AI3I $KPCREV02
caller: OK
KPC:    KPC reverse capture
KPC:    R:261126/0122 2@AI3I.AI3I.#WPA.PA.USA.NOAM
KPC:    Local no-RF test only
KPC:    ^Z
caller: F>
KPC:    *** DONE
KPC:    >
```

The message was held by `PBHOLD ON`, then released with `E 2 H` before the
reverse request. `PBKILLFW ON` removed it after the completed exchange.
The mailbox was empty when the session ended. A separate duplicate BID
test produced `NO` followed by `>`. Since `NO` can also mean a general
rejection, pyBulletin leaves that message pending for a later attempt.

Set `protocol = "w0rli"` on a neighbor for the classic HM$ text exchange.
The pyBulletin implementation is covered by `tests/test_w0rli.py` for
outbound, reverse, duplicate, inbound, and interrupted transfer. It has not
yet connected to the KPC through pyBulletin's AX.25 transport. A separate
radio/modem endpoint is needed for that test.

The [1993 BBS Interface Specification working draft](https://www.ir3ip.net/iw3fqg/doc/bbsspec.htm)
describes the SID, send command, response, body, and reverse-forward sequence.
The local *Kantronics KPC-9612 Manual* and ROM 8.2 addendum describe the
mailbox controls. The [PK-232MBX manual](https://www.dl0bn.de/dc7xj/Anleitungen/AEA%20PK-232MBX_Manual.pdf)
documents a related W0RLI/WA7MBL-style MailDrop, but its prompt and
forwarding flag behavior differ. AEA interoperability remains untested.
