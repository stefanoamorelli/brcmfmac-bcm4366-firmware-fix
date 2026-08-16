#!/usr/bin/env python3
"""
Patch the BCM4366 brcmfmac firmware to stop a recurring RX-path crash.

This tool ships NO Broadcom code. It operates on a firmware file you already
have on your own machine, and writes a new file next to it. See README.md for
what the patch does and why, and for the licensing caveats.

Usage:
    python3 patch_bcm4366.py /lib/firmware/brcm/brcmfmac4366c-pcie.bin.xz
    python3 patch_bcm4366.py ./brcmfmac4366c-pcie.bin -o patched.bin

The tool refuses to run unless it finds exactly one match for a 14-byte
instruction signature, so it will not blindly scribble on an image it does not
recognise.
"""

import argparse
import hashlib
import lzma
import pathlib
import sys

# Instruction signature around the patch site (Thumb-2, little-endian):
#
#   ldr.w r3, [r4, #0x630]     d4 f8 30 36   <- base of a 2-entry context array
#   ubfx  r2, r2, #8, #1       c2 f3 00 22   <- THE PATCH SITE: index from a frame bit
#   movs  r1, #0x18            18 21         <- entry stride
#   mla   fp, r1, r2, r3       01 fb 02 3b   <- fp = base + 0x18 * index
#
SIGNATURE = bytes.fromhex("d4f83036" "c2f30022" "1821" "01fb023b")
UBFX_OFFSET_IN_SIG = 4
ORIGINAL = bytes.fromhex("c2f30022")           # ubfx r2, r2, #8, #1
REPLACEMENT = bytes.fromhex("0022" "00bf")     # movs r2, #0  ;  nop

# Stock image this was developed against. Other revisions may still work, since
# the signature check is what actually gates the patch, but you get a warning.
KNOWN_STOCK_SHA256 = "a250275c81c626de4379591d367624b7d9da99a665a946738e657217a9d9e53e"
KNOWN_SIZE = 1120971

ACCEPT_PHRASE = "i accept"

DISCLAIMER = """
================================ READ THIS ================================

Unofficial work with no connection to Broadcom, ASUS, or any vendor. Nobody
has reviewed it. No warranty of any kind, and no liability for damage, data
loss, downtime, or regulatory trouble arising from its use. You are modifying
firmware that runs on a radio transmitter. That is your decision and your
responsibility.

Running modified firmware may damage the device, may leave it operating outside
the parameters it was designed and certified for, and will in all likelihood
void any warranty, support entitlement, or return rights you have on the card.
It may also breach the terms you accepted when you obtained the firmware.
Assume the card is no longer covered once you do this.

Review the analysis and the tool before you run either of them, and make sure
you understand what the patch changes and why. Do not run it because a README
said it worked on someone else's machine. If any part of this does not make
sense to you, that is a reason to stop rather than a detail to skip.

===========================================================================
"""


def require_acceptance(preaccepted: bool) -> bool:
    """Show the disclaimer and make the user accept it before anything is written."""
    print(DISCLAIMER)

    if preaccepted:
        print("Risks accepted via --i-accept-the-risks.\n")
        return True

    if not sys.stdin.isatty():
        print("Not running on a terminal, so the prompt cannot be answered.\n"
              "Re-run with --i-accept-the-risks if you have read the above and\n"
              "accept them.", file=sys.stderr)
        return False

    try:
        answer = input(f"Type '{ACCEPT_PHRASE}' to confirm you have read the above "
                       "and accept the risks: ")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        return False

    if answer.strip().lower() != ACCEPT_PHRASE:
        print("Not accepted. Nothing was changed.", file=sys.stderr)
        return False

    print()
    return True


def read_firmware(path: pathlib.Path) -> tuple[bytes, bool]:
    raw = path.read_bytes()
    if path.suffix == ".xz":
        return lzma.decompress(raw), True
    return raw, False


def write_firmware(path: pathlib.Path, data: bytes, compress: bool) -> None:
    if compress:
        # The kernel's built-in XZ decoder requires CRC32 and a bounded
        # dictionary. Default `xz` settings (CRC64) produce EINVAL at load.
        filters = [{"id": lzma.FILTER_LZMA2, "preset": 9, "dict_size": 1 << 20}]
        blob = lzma.compress(data, format=lzma.FORMAT_XZ,
                             check=lzma.CHECK_CRC32, filters=filters)
    else:
        blob = data
    path.write_bytes(blob)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware", type=pathlib.Path, nargs="?",
                    help="path to brcmfmac4366c-pcie.bin or .bin.xz")
    ap.add_argument("-o", "--output", type=pathlib.Path,
                    help="output path (default: <input>.patched)")
    ap.add_argument("--revert-info", action="store_true",
                    help="print how to undo this and exit")
    ap.add_argument("--i-accept-the-risks", action="store_true",
                    dest="accepted",
                    help="skip the interactive prompt, for non-interactive use. "
                         "Passing it means you have read the disclaimer and "
                         "accept the risks.")
    args = ap.parse_args()

    if args.revert_info:
        print("To revert: restore the original file, then reload the driver.\n"
              "  sudo cp <your-backup> /lib/firmware/brcm/brcmfmac4366c-pcie.bin.xz\n"
              "  sudo modprobe -r brcmfmac && sudo modprobe brcmfmac\n"
              "Or reinstall your distro's linux-firmware / brcmfmac-firmware package.\n"
              "The firmware is uploaded to device RAM at every driver load and is\n"
              "never written to the card, so a power cycle always starts clean.")
        return 0

    if args.firmware is None:
        ap.error("a firmware path is required unless --revert-info is given")

    if not args.firmware.is_file():
        print(f"error: no such file: {args.firmware}", file=sys.stderr)
        return 1

    if not require_acceptance(args.accepted):
        return 3

    data, was_compressed = read_firmware(args.firmware)
    digest = hashlib.sha256(data).hexdigest()

    print(f"input      : {args.firmware}")
    print(f"size       : {len(data)} bytes")
    print(f"sha256     : {digest}")
    if digest != KNOWN_STOCK_SHA256:
        print("  ! not the exact image this was developed against.")
        print("  ! continuing on signature match alone. Verify the result yourself.")
    if len(data) != KNOWN_SIZE:
        print(f"  ! unexpected size (expected {KNOWN_SIZE})")

    hits = []
    start = 0
    while (found := data.find(SIGNATURE, start)) >= 0:
        hits.append(found)
        start = found + 1

    if len(hits) != 1:
        print(f"error: expected exactly 1 signature match, found {len(hits)}. "
              "Refusing to patch.", file=sys.stderr)
        return 2

    site = hits[0] + UBFX_OFFSET_IN_SIG
    if data[site:site + 4] != ORIGINAL:
        print(f"error: bytes at 0x{site:x} are not {ORIGINAL.hex()}. Refusing.",
              file=sys.stderr)
        return 2

    print(f"patch site : file offset 0x{site:x}")
    print(f"             {ORIGINAL.hex()}  ubfx r2, r2, #8, #1")
    print(f"          -> {REPLACEMENT.hex()}  movs r2, #0 ; nop")

    patched = bytearray(data)
    patched[site:site + 4] = REPLACEMENT

    out = args.output or args.firmware.with_suffix(args.firmware.suffix + ".patched")
    write_firmware(out, bytes(patched), compress=was_compressed)

    print(f"\nwrote      : {out}")
    print(f"bytes changed: 4")
    print("\nBack up your original before installing this. Run with --revert-info "
          "for the undo steps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
