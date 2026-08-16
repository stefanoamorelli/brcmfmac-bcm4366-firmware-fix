# `BCM4366` firmware crash on Linux

The Broadcom `BCM4366`, as shipped on the ASUS `PCE-AC88` and similar PCIe cards,
crashes its firmware roughly every 40 minutes under load on Linux with
`brcmfmac`. Each crash blacks out the link for 10 to 15 seconds while the
driver reloads the firmware, which is long enough to break long-lived TCP
sessions and anything with a keepalive shorter than the outage.

The bug seems to be in the firmware receive path. It looks like a single
deterministic cause, and a 4-byte change stops it.

| | |
|---|---|
| Affected firmware | [`brcmfmac4366c-pcie.bin`](https://gitlab.com/kernel-firmware/linux-firmware/-/blob/main/brcm/brcmfmac4366c-pcie.bin.xz), `10.28.2 (r769115)`, FWID `01-d2cbb8fd`, built Nov 2018 |
| Driver | [`brcmfmac`](https://wireless.wiki.kernel.org/en/users/drivers/brcm80211) (fullmac, PCIe) |
| Failure | Data abort at `pc 0x23797c`, apparently a wild pointer through an uninitialised context slot |
| Fix | 4 bytes at `0x2378b2`, forcing the context index to slot 0 |
| Ships vendor code | No |

That firmware is the newest build in
[linux-firmware](https://gitlab.com/kernel-firmware/linux-firmware) and has not
changed in years. The chip is end of life, therefore no vendor fix is coming.

> [!CAUTION]
> Unofficial work with no connection to Broadcom, ASUS, or any vendor. Nobody
> has reviewed it. No warranty of any kind, and no liability for damage, data
> loss, downtime, or regulatory trouble arising from its use. You are modifying
> firmware that runs on a radio transmitter. That is your decision and your
> responsibility.
>
> Running modified firmware may damage the device, may leave it operating
> outside the parameters it was designed and certified for, and will in all
> likelihood void any warranty, support entitlement, or return rights you have
> on the card. It may also breach the terms you accepted when you obtained the
> firmware. Assume the card is no longer covered once you do this.
>
> Review the analysis and the tool before you run either of them, and make sure
> you understand what the patch changes and why. Do not run it because a README
> said it worked on someone else's machine. If any part of this does not make
> sense to you, that is a reason to stop rather than a detail to skip.

Specific things to weigh before running anything:

- The analysis rests on one machine and one test workload. Behaviour on other
  hardware, other access points, or other traffic patterns is unknown.
- The patch changes how the firmware selects an internal context. I inferred
  what that context is from the surrounding arithmetic rather than proving it.
  See [what this does and does not establish](#what-this-does-and-does-not-establish).
- Wireless devices are regulated. This patch does not touch the regulatory or
  calibration tables, but you remain responsible for operating your hardware
  within the rules that apply where you are.
- If the card matters to you, keep a backup of the original firmware and read
  [reverting](#reverting-and-how-safe-this-is) first.

## Quick start

```sh
# 1. back up the original
sudo cp /lib/firmware/brcm/brcmfmac4366c-pcie.bin.xz ~/brcmfmac4366c-pcie.bin.xz.orig

# 2. produce a patched image next to it
#    prints the disclaimer and waits for you to type "I accept"
python3 patch_bcm4366.py /lib/firmware/brcm/brcmfmac4366c-pcie.bin.xz

# 3. install it and reload the driver
sudo cp /lib/firmware/brcm/brcmfmac4366c-pcie.bin.xz.patched \
        /lib/firmware/brcm/brcmfmac4366c-pcie.bin.xz
sudo modprobe -r brcmfmac && sudo modprobe brcmfmac

# undo at any time
python3 patch_bcm4366.py --revert-info
```

The tool refuses to touch an image whose instruction signature does not match
exactly once, so it will not scribble on something it does not recognise. It
also refuses to write anything until you have seen the disclaimer and accepted
it. Outside a terminal, where the prompt cannot be answered, pass
`--i-accept-the-risks` instead. Passing that flag means the same thing as
typing the phrase.

## The symptom

```
brcmfmac 0000:XX:00.0: brcmf_pcie_bus_console_read: CONSOLE: c_init: Watchdog reset bit set, clearing
brcmfmac 0000:XX:00.0: brcmf_pcie_bus_console_read: CONSOLE: TRAP 4(2c7a88): pc 23797c, lr 23795b, sp 2c7ae0, cpsr 48000193, spsr 48000033
brcmfmac 0000:XX:00.0: brcmf_pcie_bus_console_read: CONSOLE:   dfsr 5, dfar bf10f2bc
brcmfmac 0000:XX:00.0: brcmf_pcie_bus_console_read: CONSOLE:   r0 1, r1 1, r2 0, r3 bf10f204, r4 41cc0c, ...
ieee80211 phy0: brcmf_fil_cmd_data: bus is down. we have nothing to do.
brcmfmac: brcmf_fw_alloc_request: using brcm/brcmfmac4366c-pcie for chip BCM4366/4
```

Every crash lands on the same `pc 23797c`. That constancy is the useful part.
One code path looks to be failing, rather than flaky hardware, heat, or a
marginal PCIe link.

## Finding the code

The firmware image is a flat Thumb-2 blob loaded at `0x200000`. Two independent
checks agree on that base:

1. Every return address on the crash stack should sit immediately after a `BL`
   or `BLX`. Scoring candidate bases against that test puts `0x200000` on top.
2. The registers say the faulting instruction must reach memory through `r3` at
   a displacement of `dfar - r3`, which is `0xb8`. Brute-forcing the base until
   the instruction at `pc` decodes to exactly that yields `0x200000`, where it
   reads `ldr.w r3, [r3, #0xb8]`. Nothing else fits.

With that base, `pc 0x23797c` sits at file offset `0x3797c`, well inside the
1,120,971-byte image.

> [!NOTE]
> Some sources give `0x180000` as the RAM base for this chip family. Assume that
> and the target lands in a run of zeros, which is the quickest way to tell you
> have the wrong base.

## Root cause

The faulting function starts at `0x2377b4`.

```asm
; where the bad pointer is produced
002378ac  ldrh   r2, [r5, #6]        ; 16-bit field from the RX descriptor
002378ae  ldr.w  r3, [r4, #0x630]    ; base of a 2-entry context array
002378b2  ubfx   r2, r2, #8, #1      ; index := bit 8 of that field  (0 or 1)
002378b6  movs   r1, #0x18           ; entry stride
002378b8  mla    fp, r1, r2, r3      ; fp = base + 0x18 * index
002378be  ldr.w  r8, [fp, #4]        ; r8 = entry->ptr
002378c8  ldr.w  r3, [r8, #0xc]      ; r3 = r8->field_0xc      <-- garbage
002378ce  str    r3, [sp, #0x40]     ; stash in a local

; where it is used, about 0xAE bytes later
00237978  ldr    r3, [sp, #0x40]
0023797a  movs   r2, #0
0023797c  ldr.w  r3, [r3, #0xb8]     ; <-- CRASH, no validity check
00237982  strb   r2, [r3, #6]        ; and then written through
```

Reading that, a single bit lifted out of a received frame appears to select one
of two context slots. The firmware then walks three pointer hops (`slot`, then
`+4`, then `+0xc`, then `+0xb8`) without checking any of them. If that bit
selects a slot that is not initialised in the current configuration, the chain
would produce a wild pointer (`0xbf10f204`, roughly 3 GB past the end of a
device whose RAM tops out near 4.5 MB) and the third hop takes a data abort,
which is what the register dump shows.

The local at `sp+0x40` is explicitly zeroed in the function prologue
(`movs r3, #0` followed by `str r3, [sp, #0x40]`), so the code anticipates it
being unset, yet the use site carries no `cbz` guard.

That would account for the observed behaviour. The faulting PC never moves, the
timing looks random because it would fire whenever a frame with that bit set
arrives, and the rate climbs in dense RF environments where more varied traffic
is received.

## The fix

Force the index to slot 0, so the uninitialised entry is never selected:

```diff
- 0x2378b2:  c2 f3 00 22   ubfx r2, r2, #8, #1
+ 0x2378b2:  00 22 00 bf   movs r2, #0 ; nop
```

Four bytes. The instruction stream stays aligned, and `mla` still sits at
`0x2378b8`. `movs` writes flags where `ubfx` did not, which is harmless here
because the next instruction (`movs r1, #0x18`) sets them again before anything
reads them.

The tool locates the site by a 14-byte instruction signature that occurs
exactly once in the image, rather than by a hardcoded offset, so it survives
repackaging and works on copies that are not byte-identical to the one used
here.

> [!IMPORTANT]
> If you recompress the image yourself, the kernel built-in XZ decoder wants
> `--check=crc32` and a bounded dictionary. Default `xz` settings use CRC64 and
> the firmware loader then fails with `-22` (EINVAL). The tool handles this.
> Plain `xz -9` on the command line does not.

## Validation

Test rig: [Fedora 43](https://fedoramagazine.org/announcing-fedora-linux-43/),
[kernel 7.1.8](https://cdn.kernel.org/pub/linux/kernel/v7.x/ChangeLog-7.1.8),
[`PCE-AC88`](https://www.asus.com/networking-iot-servers/adapters/all-series/pceac88/)
in a desktop, associated to a 5 GHz AP in a dense environment (around 45
visible BSSIDs), driven with a continuous `ping -s 1400 -i 0.2` bound to the
interface.

| | Stock firmware | Patched |
|---|---|---|
| Firmware traps | 13 over 3 days | 0 |
| Mean time between crashes under load | ~40 min | none in 21.5 h |
| Firmware reloads | 18 | 0 |
| Packets exchanged during test | n/a | 384,090 |
| RX PHY rate at -57 dBm | 600 Mbit/s | 600 Mbit/s |
| Median RTT | n/a | 5.3 ms |

That is roughly 32 times the previous mean time between failures with no
recurrence.

## What this does and does not establish

The specific crash is gone, and that part is well supported.

> [!WARNING]
> It does not establish that the firmware is now correct.

- A ping flood exercises one narrow slice of the receive path. Traffic patterns
  this test never produced could still behave differently.
- That slot 1 goes unused was inferred from the arithmetic, not proven. The
  working guess is a band or interface context, and on a station associated to a
  single AP there appears to be only one valid entry, which would explain why
  the other is never initialised. If your setup legitimately uses both, forcing
  slot 0 could misbehave in ways this testing would not reveal.
- Absence of crashes is evidence, not proof.

Treat this as a mitigation that removes one specific fault rather than a repair
of the underlying design.

## Reverting, and how safe this is

The firmware is uploaded to device RAM on every driver load and is not written
to the card, so a bad image should not be able to brick it. Power-cycle and the
next load reads whatever file is on disk. The card persistent NVRAM (MAC
address, RF calibration, regulatory data) is a separate thing that this patch
never touches.

> [!WARNING]
> That reasoning covers persistent storage. It is not a promise that nothing
> can go wrong. Firmware drives the radio, and running a modified build can
> still damage hardware or push it outside its designed operating parameters.
> Reverting the file restores the stock firmware. It does not restore a warranty
> you have already voided.

Keep a backup and you have several ways back:

```sh
sudo cp <your-backup> /lib/firmware/brcm/brcmfmac4366c-pcie.bin.xz
sudo modprobe -r brcmfmac && sudo modprobe brcmfmac

# or, from your distro
sudo dnf reinstall brcmfmac-firmware
sudo apt install --reinstall firmware-brcm80211
```

> [!IMPORTANT]
> Any `linux-firmware` package update silently restores the stock blob, with no
> warning, so the crashes come back. If you rely on the patch, pin the package
> or re-apply after updates.

Enabling the IOMMU is worth doing regardless, because it bounds what a confused
device can DMA into host memory.

## Simpler alternatives worth trying first

Using a different card is the cheapest fix. If the machine has another wireless
interface, `mt7921e` and `iwlwifi` hardware is actively maintained and does not
carry this bug.

Failing that, you could try `roamoff=1`:

```sh
echo "options brcmfmac roamoff=1" | sudo tee /etc/modprobe.d/brcmfmac.conf
sudo modprobe -r brcmfmac && sudo modprobe brcmfmac
```

It is untested against this bug specifically, but it disables the firmware
roaming engine, and the firmware console shows its channel interference monitor
failing continuously (`WLC_CHANIM upd blocked scan/detect`) in the run-up to
crashes. It costs nothing and reverses cleanly, therefore it is worth trying
before patching a blob.

## Licensing

> [!NOTE]
> This repository contains no Broadcom code. No firmware, no patched image, and
> no extracted bytes beyond the short instruction sequences quoted above for the
> purpose of describing the defect. The tool operates on a copy you already
> possess and writes its output only to your own machine.

The firmware is proprietary. The
[Broadcom licence](https://gitlab.com/kernel-firmware/linux-firmware/-/blob/main/LICENCE.broadcom_bcm43xx)
permits redistribution only "complete, unmodified, and as provided by
Broadcom", and clause 2(ii) purports to prohibit modification and disassembly
outright.

Whether you may patch firmware on hardware you own is a question of your local
law rather than of this document. In the EU, the Software Directive provisions
on error correction and interoperability are relevant and are not
straightforwardly waivable by contract. None of this is legal advice.

The tooling in this repository is MIT licensed. See [LICENSE](LICENSE).

## Prior art and credits

The general technique, analysing and patching Broadcom Wi-Fi firmware and
distributing patches rather than blobs, was established by
[nexmon](https://github.com/seemoo-lab/nexmon) at the
[Secure Mobile Networking Lab](https://www.seemoo.tu-darmstadt.de/),
[TU Darmstadt](https://www.tu-darmstadt.de/). Their work is far broader than
this and is worth reading if you want to go further.

The disassembly here was produced with [Capstone](https://www.capstone-engine.org/).

> [!NOTE]
> Credit does not imply anything in the other direction. Nobody named above is
> affiliated with this repository, has reviewed it, or endorses it. The nexmon
> authors in particular had no involvement, and any mistakes here are mine
> rather than theirs. Please do not raise issues with them about this work.
>
> Broadcom, ASUS, and the product names used in this document are trademarks of
> their respective owners, referenced only to identify the hardware concerned.
> This project has no affiliation with, sponsorship from, or endorsement by any
> of them.

I could not find a prior published root cause for this particular crash. If one
exists, please open an issue.
