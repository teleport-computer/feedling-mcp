"""
Minimal TDX v4 quote parser — reference implementation for the Feedling
iOS auditor (see docs/DESIGN_E2E.md §5.2).

SCOPE: structural parsing only. Extract the measured values (MRTD,
RTMR0-3, REPORT_DATA) from a TDX quote so we can cross-check them
against claims in the attestation bundle. NO signature-chain
verification yet — that's the Phase 1E implementation step. For now,
users of this parser should already trust the quote came from a
trustworthy source (the dstack simulator or Phala Cloud), and use
this module to read out what the TEE measured.

The format: Intel TDX DCAP Attestation Quote v4 as published at
https://cdrdv2-public.intel.com/726790 (referred to throughout as
"the spec"). The parser intentionally does no dynamic library loading
so it works from any Python 3.8+ environment with zero deps; the
iOS port in Swift follows the same structure 1:1.

Layout of a TDX v4 quote:
    Header                  (48 bytes)   — version, tee_type, vendor, user_data
    Report Body             (584 bytes)  — measurements + report_data
    Signature Data Length   (4 bytes)    — little-endian u32
    Signature Data          (variable)   — ECDSA sig + QE report + cert data

This module reads Header + Report Body (that's where all the measured
values live) and returns everything as hex strings for easy comparison.
Signature data is captured but not parsed.
"""

from __future__ import annotations

from dataclasses import dataclass


# Constants from the TDX v4 spec
HEADER_SIZE = 48
REPORT_BODY_SIZE = 584
MIN_QUOTE_SIZE = HEADER_SIZE + REPORT_BODY_SIZE + 4  # +4 for sig-data-length u32


class DCAPParseError(Exception):
    """Raised when a quote is malformed or doesn't match the v4 spec."""


@dataclass(frozen=True)
class TDXQuoteHeader:
    version: int              # u16, expected == 4 for TDX v4
    att_key_type: int         # u16
    tee_type: int             # u32, expected == 0x81 for TDX
    reserved: bytes           # 4
    vendor_id: bytes          # 16
    user_data: bytes          # 20


@dataclass(frozen=True)
class TDXReportBody:
    """The 584-byte TEE report embedded in the quote.

    Field sizes from the spec. All returned as raw bytes; callers typically
    render as hex.
    """
    tee_tcb_svn: bytes        # 16
    mrseam: bytes             # 48
    mrsignerseam: bytes       # 48
    seam_attr: bytes          # 8
    td_attr: bytes            # 8
    xfam: bytes               # 8
    mrtd: bytes               # 48 — the MRTD we care about
    mrconfig_id: bytes        # 48
    mrowner: bytes            # 48
    mrownerconfig: bytes      # 48
    rtmr0: bytes              # 48
    rtmr1: bytes              # 48
    rtmr2: bytes              # 48
    rtmr3: bytes              # 48 — contains compose_hash for dstack apps
    report_data: bytes        # 64 — our custom binding payload


@dataclass(frozen=True)
class TDXQuote:
    header: TDXQuoteHeader
    body: TDXReportBody
    signature_data: bytes     # raw bytes, sig-chain verification is layered later

    # Convenience: the compose_hash stored by dstack in the first 32 bytes
    # of RTMR3. (The remaining 16 bytes of RTMR3 are the running hash chain
    # of any post-boot events — we only use the raw RTMR3 for the DevProof
    # check; dstack publishes compose_hash separately too.)
    @property
    def rtmr3_hex(self) -> str:
        return self.body.rtmr3.hex()

    @property
    def mrtd_hex(self) -> str:
        return self.body.mrtd.hex()

    @property
    def report_data_hex(self) -> str:
        return self.body.report_data.hex()


def _read_u16_le(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 2], "little")


def _read_u32_le(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 4], "little")


def parse_quote(quote_bytes: bytes) -> TDXQuote:
    """Parse raw TDX quote bytes into a structured TDXQuote.

    Raises DCAPParseError if the buffer is shorter than the minimum or the
    version/tee_type fields don't look like a TDX v4 quote.
    """
    if len(quote_bytes) < MIN_QUOTE_SIZE:
        raise DCAPParseError(
            f"quote too short: {len(quote_bytes)} bytes, need at least {MIN_QUOTE_SIZE}"
        )

    # --- Header (0..48) ---
    version = _read_u16_le(quote_bytes, 0)
    if version != 4:
        raise DCAPParseError(f"unexpected quote version {version}, expected 4 (TDX v4)")

    att_key_type = _read_u16_le(quote_bytes, 2)
    tee_type = _read_u32_le(quote_bytes, 4)
    if tee_type != 0x81:
        # 0x81 = TDX, 0x00 = SGX. Fail loud if we're reading an SGX quote.
        raise DCAPParseError(
            f"tee_type {hex(tee_type)} is not TDX (0x81). Wrong quote format?"
        )

    reserved = quote_bytes[8:12]
    vendor_id = quote_bytes[12:28]
    user_data = quote_bytes[28:48]

    header = TDXQuoteHeader(
        version=version,
        att_key_type=att_key_type,
        tee_type=tee_type,
        reserved=reserved,
        vendor_id=vendor_id,
        user_data=user_data,
    )

    # --- Report Body (48..632) ---
    b = HEADER_SIZE
    body = TDXReportBody(
        tee_tcb_svn=quote_bytes[b:b + 16],
        mrseam=quote_bytes[b + 16:b + 64],
        mrsignerseam=quote_bytes[b + 64:b + 112],
        seam_attr=quote_bytes[b + 112:b + 120],
        td_attr=quote_bytes[b + 120:b + 128],
        xfam=quote_bytes[b + 128:b + 136],
        mrtd=quote_bytes[b + 136:b + 184],
        mrconfig_id=quote_bytes[b + 184:b + 232],
        mrowner=quote_bytes[b + 232:b + 280],
        mrownerconfig=quote_bytes[b + 280:b + 328],
        rtmr0=quote_bytes[b + 328:b + 376],
        rtmr1=quote_bytes[b + 376:b + 424],
        rtmr2=quote_bytes[b + 424:b + 472],
        rtmr3=quote_bytes[b + 472:b + 520],
        report_data=quote_bytes[b + 520:b + 584],
    )

    # --- Signature Data ---
    sig_off = HEADER_SIZE + REPORT_BODY_SIZE
    sig_len = _read_u32_le(quote_bytes, sig_off)
    sig_start = sig_off + 4
    sig_end = sig_start + sig_len
    if sig_end > len(quote_bytes):
        raise DCAPParseError(
            f"sig_len {sig_len} overruns quote buffer (buffer {len(quote_bytes)}, sig_end {sig_end})"
        )
    signature_data = quote_bytes[sig_start:sig_end]

    return TDXQuote(header=header, body=body, signature_data=signature_data)


def parse_quote_hex(quote_hex: str) -> TDXQuote:
    """Convenience wrapper: hex-string in, parsed quote out."""
    return parse_quote(bytes.fromhex(quote_hex))


# Marker: Phase 1E Swift port will implement the same two entry points
# (`parse_quote` over raw bytes, and a thin helper for hex). Signature
# verification (Intel DCAP PCK cert chain + ECDSA-P256 sig over body) is
# Phase 1E scope, not this reference.
