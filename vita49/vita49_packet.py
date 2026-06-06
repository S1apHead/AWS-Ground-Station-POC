"""
VITA 49 (VRT - VITA Radio Transport) Packet Generator & Parser
---------------------------------------------------------------
AWS Ground Station delivers raw digitised IF samples as VITA 49 packets
over UDP to the customer dataflow endpoint inside the VPC.

Standard: VITA-49.2 (ANSI/VITA 49.2-2017)

Packet Types:
  - Signal Data Packet (type 0001) — contains I/Q samples from antenna
  - Context Packet      (type 0100) — contains RF metadata (freq, BW, gain)
  - Command Packet      (type 0110) — control commands

Frame Structure:
  ┌─────────────────────────────────────┐
  │  Header (32 bits)                   │
  │    [31:28] Packet Type              │
  │    [27]    Class ID present         │
  │    [26]    Trailer present          │
  │    [25:24] TSI (timestamp integer)  │
  │    [23:22] TSF (timestamp frac)     │
  │    [21:16] Packet Count (mod 16)    │
  │    [15:0]  Packet Size (32-bit words│
  ├─────────────────────────────────────┤
  │  Stream ID (32 bits)                │
  ├─────────────────────────────────────┤
  │  Class ID (64 bits, if present)     │
  ├─────────────────────────────────────┤
  │  Integer Timestamp (32 bits)        │
  ├─────────────────────────────────────┤
  │  Fractional Timestamp (64 bits)     │
  ├─────────────────────────────────────┤
  │  Payload — I/Q samples              │
  │  (16-bit signed int pairs)          │
  ├─────────────────────────────────────┤
  │  Trailer (32 bits, if present)      │
  └─────────────────────────────────────┘
"""

import struct
import time
import random
import math
import json
from dataclasses import dataclass, field
from typing import List, Tuple, Optional


# ── Packet Type Constants ─────────────────────────────────────────────────────
PKT_TYPE_SIGNAL_DATA    = 0b0001   # IF signal data (I/Q samples)
PKT_TYPE_CONTEXT        = 0b0100   # RF context / metadata
PKT_TYPE_COMMAND        = 0b0110   # Control command

# ── Timestamp Source Indicator ────────────────────────────────────────────────
TSI_UTC   = 0b01   # UTC time
TSI_GPS   = 0b10   # GPS time

# ── Timestamp Fractional ─────────────────────────────────────────────────────
TSF_REALTIME = 0b10  # picoseconds


@dataclass
class VITA49Header:
    packet_type:   int   # 4 bits
    class_id_present: bool
    trailer_present: bool
    tsi: int             # 2 bits — timestamp integer type
    tsf: int             # 2 bits — timestamp fractional type
    packet_count: int    # 4 bits — rolling counter mod 16
    packet_size: int     # 16 bits — size in 32-bit words


@dataclass
class VITA49ContextFields:
    """RF metadata carried in Context Packets"""
    rf_freq_hz:       float   # Centre frequency (Hz)
    bandwidth_hz:     float   # Instantaneous bandwidth (Hz)
    sample_rate_hz:   float   # Sample rate (Hz)
    gain_db:          float   # Antenna gain (dB)
    ref_level_dbm:    float   # Reference level (dBm)
    polarisation:     str     # "RHCP" | "LHCP" | "LINEAR_H" | "LINEAR_V"
    satellite_id:     str     # Satellite identifier
    ground_station:   str     # Ground station name
    contact_id:       str     # Unique contact session ID


@dataclass
class VITA49SignalPacket:
    stream_id:   int
    timestamp_s: int          # Integer seconds (UTC)
    timestamp_ps: int         # Fractional picoseconds
    iq_samples:  List[Tuple[int, int]]  # (I, Q) pairs — 16-bit signed
    packet_count: int = 0


# ── Encoder ───────────────────────────────────────────────────────────────────

class VITA49Encoder:
    """Encode VITA 49 signal data and context packets for transmission"""

    def __init__(self, stream_id: int = 0x00000001):
        self.stream_id = stream_id
        self._packet_count = 0

    def _next_count(self) -> int:
        c = self._packet_count % 16
        self._packet_count += 1
        return c

    def encode_signal_packet(self, iq_samples: List[Tuple[int, int]],
                              timestamp_s: Optional[int] = None,
                              timestamp_ps: Optional[int] = None) -> bytes:
        """
        Encode I/Q samples into a VITA 49 Signal Data packet.
        Each sample is a pair of 16-bit signed integers (I, Q).
        """
        ts_s  = timestamp_s  or int(time.time())
        ts_ps = timestamp_ps or int((time.time() % 1) * 1e12)

        # Payload: pack I/Q as big-endian 16-bit signed ints
        payload = b""
        for i_val, q_val in iq_samples:
            payload += struct.pack(">hh", i_val, q_val)

        # Pad to 32-bit word boundary
        if len(payload) % 4:
            payload += b"\x00" * (4 - len(payload) % 4)

        # Header fields
        # words: 1 (header) + 1 (stream_id) + 1 (ts_int) + 2 (ts_frac) + payload_words
        payload_words = len(payload) // 4
        packet_size   = 1 + 1 + 1 + 2 + payload_words

        pkt_count = self._next_count()

        # Build 32-bit header word
        header = (
            (PKT_TYPE_SIGNAL_DATA << 28) |
            (0 << 27) |          # no class ID
            (0 << 26) |          # no trailer
            (TSI_UTC << 24) |
            (TSF_REALTIME << 22) |
            (pkt_count << 16) |
            (packet_size & 0xFFFF)
        )

        return struct.pack(">IIII", header, self.stream_id, ts_s,
                           (ts_ps >> 32) & 0xFFFFFFFF) + \
               struct.pack(">I", ts_ps & 0xFFFFFFFF) + payload

    def encode_context_packet(self, ctx: VITA49ContextFields,
                               timestamp_s: Optional[int] = None) -> bytes:
        """
        Encode RF metadata as a VITA 49 Context packet.
        Carries frequency, bandwidth, gain, satellite ID etc.
        """
        ts_s = timestamp_s or int(time.time())

        # Encode context as structured payload
        ctx_payload = json.dumps({
            "rf_freq_hz":     ctx.rf_freq_hz,
            "bandwidth_hz":   ctx.bandwidth_hz,
            "sample_rate_hz": ctx.sample_rate_hz,
            "gain_db":        ctx.gain_db,
            "ref_level_dbm":  ctx.ref_level_dbm,
            "polarisation":   ctx.polarisation,
            "satellite_id":   ctx.satellite_id,
            "ground_station": ctx.ground_station,
            "contact_id":     ctx.contact_id,
        }).encode("utf-8")

        # Pad to 32-bit boundary
        if len(ctx_payload) % 4:
            ctx_payload += b"\x00" * (4 - len(ctx_payload) % 4)

        payload_words = len(ctx_payload) // 4
        packet_size   = 1 + 1 + 1 + 2 + payload_words
        pkt_count     = self._next_count()

        header = (
            (PKT_TYPE_CONTEXT << 28) |
            (0 << 27) |
            (0 << 26) |
            (TSI_UTC << 24) |
            (TSF_REALTIME << 22) |
            (pkt_count << 16) |
            (packet_size & 0xFFFF)
        )

        ts_ps = 0
        return struct.pack(">IIII", header, self.stream_id, ts_s,
                           (ts_ps >> 32) & 0xFFFFFFFF) + \
               struct.pack(">I", ts_ps & 0xFFFFFFFF) + ctx_payload


# ── Decoder ───────────────────────────────────────────────────────────────────

class VITA49Decoder:
    """Decode received VITA 49 UDP packets"""

    def decode(self, data: bytes) -> dict:
        if len(data) < 20:
            raise ValueError(f"Packet too short: {len(data)} bytes")

        header_word = struct.unpack(">I", data[0:4])[0]
        pkt_type    = (header_word >> 28) & 0xF
        has_class   = bool((header_word >> 27) & 0x1)
        has_trailer = bool((header_word >> 26) & 0x1)
        tsi         = (header_word >> 24) & 0x3
        tsf         = (header_word >> 22) & 0x3
        pkt_count   = (header_word >> 16) & 0xF
        pkt_size    = header_word & 0xFFFF

        stream_id   = struct.unpack(">I", data[4:8])[0]
        ts_s        = struct.unpack(">I", data[8:12])[0]
        ts_ps_hi    = struct.unpack(">I", data[12:16])[0]
        ts_ps_lo    = struct.unpack(">I", data[16:20])[0]
        ts_ps       = (ts_ps_hi << 32) | ts_ps_lo

        payload = data[20:]

        result = {
            "packet_type":  pkt_type,
            "packet_count": pkt_count,
            "stream_id":    f"0x{stream_id:08X}",
            "timestamp_s":  ts_s,
            "timestamp_ps": ts_ps,
            "packet_size_words": pkt_size,
        }

        if pkt_type == PKT_TYPE_SIGNAL_DATA:
            result["type_name"] = "SIGNAL_DATA"
            samples = []
            for i in range(0, len(payload) - 3, 4):
                i_val, q_val = struct.unpack(">hh", payload[i:i+4])
                samples.append({"I": i_val, "Q": q_val})
            result["iq_samples"] = samples
            result["sample_count"] = len(samples)

        elif pkt_type == PKT_TYPE_CONTEXT:
            result["type_name"] = "CONTEXT"
            try:
                clean = payload.rstrip(b"\x00")
                result["context"] = json.loads(clean.decode("utf-8"))
            except Exception:
                result["context"] = {"raw_bytes": payload.hex()}

        else:
            result["type_name"] = f"UNKNOWN_TYPE_{pkt_type}"
            result["payload_hex"] = payload.hex()

        return result


# ── Signal Simulator ──────────────────────────────────────────────────────────

class LEOSignalSimulator:
    """
    Simulate I/Q samples from a LEO satellite downlink.
    Models: carrier + BPSK/QPSK modulated telemetry + noise.
    """

    def __init__(self,
                 carrier_freq_hz: float = 8_100_000,
                 sample_rate_hz: float  = 25_000_000,
                 snr_db: float = 12.0,
                 modulation: str = "QPSK"):
        self.carrier_freq_hz = carrier_freq_hz
        self.sample_rate_hz  = sample_rate_hz
        self.snr_db          = snr_db
        self.modulation      = modulation
        self._phase          = 0.0

    def _qpsk_symbols(self, n_symbols: int) -> List[complex]:
        """Generate random QPSK symbols"""
        const = [complex(1,1), complex(-1,1), complex(1,-1), complex(-1,-1)]
        return [random.choice(const) / math.sqrt(2) for _ in range(n_symbols)]

    def generate_samples(self, n_samples: int = 1024,
                          samples_per_symbol: int = 8) -> List[Tuple[int,int]]:
        """
        Generate n_samples of I/Q data as 16-bit signed integers.
        Returns list of (I, Q) tuples scaled to 16-bit range.
        """
        noise_amplitude = 10 ** (-self.snr_db / 20)
        n_symbols       = max(1, n_samples // samples_per_symbol)
        symbols         = self._qpsk_symbols(n_symbols)

        # Upsample: each symbol repeated samples_per_symbol times
        upsampled = []
        for sym in symbols:
            upsampled.extend([sym] * samples_per_symbol)
        # Trim/pad to n_samples
        upsampled = upsampled[:n_samples]
        while len(upsampled) < n_samples:
            upsampled.append(upsampled[-1])

        result = []
        for k, sym in enumerate(upsampled):
            # Carrier rotation
            phase = 2 * math.pi * self.carrier_freq_hz * k / self.sample_rate_hz
            carrier = complex(math.cos(phase), math.sin(phase))
            # Modulated signal + AWGN noise
            noise = complex(
                random.gauss(0, noise_amplitude),
                random.gauss(0, noise_amplitude)
            )
            sample = sym * carrier + noise
            # Scale to 16-bit signed (-32768 to 32767)
            scale = 16000
            i_int = max(-32768, min(32767, int(sample.real * scale)))
            q_int = max(-32768, min(32767, int(sample.imag * scale)))
            result.append((i_int, q_int))

        return result


# ── Demo ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from rich.console import Console
    from rich.table import Table as RTable
    from rich import box

    console = Console()

    console.print("\n[bold cyan]VITA 49 (VRT) — AWS Ground Station Protocol POC[/bold cyan]")
    console.print("[dim]VITA-49.2 | UDP transport | I/Q signal data[/dim]\n")

    # 1. Create encoder and simulator
    encoder  = VITA49Encoder(stream_id=0xABCD1234)
    decoder  = VITA49Decoder()
    sim      = LEOSignalSimulator(
                   carrier_freq_hz=8_100_000,
                   sample_rate_hz=25_000_000,
                   snr_db=14.0,
                   modulation="QPSK")

    # 2. Encode a context packet
    ctx = VITA49ContextFields(
        rf_freq_hz       = 8_100_000_000.0,   # 8.1 GHz X-band
        bandwidth_hz     = 25_000_000.0,       # 25 MHz
        sample_rate_hz   = 25_000_000.0,       # 25 Msps
        gain_db          = 42.0,
        ref_level_dbm    = -85.0,
        polarisation     = "RHCP",
        satellite_id     = "SPACENET-1A",
        ground_station   = "AWS-GS-SYDNEY",
        contact_id       = "CONTACT-2026-001",
    )
    ctx_bytes = encoder.encode_context_packet(ctx)
    ctx_decoded = decoder.decode(ctx_bytes)

    console.print("[bold yellow]► Context Packet (RF Metadata)[/bold yellow]")
    t = RTable(box=box.ROUNDED, show_header=True, header_style="bold blue")
    t.add_column("Field", style="cyan")
    t.add_column("Value", style="white")
    t.add_row("Packet Type", ctx_decoded["type_name"])
    t.add_row("Stream ID", ctx_decoded["stream_id"])
    t.add_row("Timestamp (UTC)", str(ctx_decoded["timestamp_s"]))
    for k, v in ctx_decoded.get("context", {}).items():
        t.add_row(k, str(v))
    console.print(t)
    console.print(f"[dim]  Encoded size: {len(ctx_bytes)} bytes[/dim]\n")

    # 3. Encode a signal data packet
    iq_samples  = sim.generate_samples(n_samples=512)
    sig_bytes   = encoder.encode_signal_packet(iq_samples)
    sig_decoded = decoder.decode(sig_bytes)

    console.print("[bold yellow]► Signal Data Packet (I/Q Samples)[/bold yellow]")
    t2 = RTable(box=box.ROUNDED, show_header=True, header_style="bold blue")
    t2.add_column("Field", style="cyan")
    t2.add_column("Value", style="white")
    t2.add_row("Packet Type", sig_decoded["type_name"])
    t2.add_row("Stream ID", sig_decoded["stream_id"])
    t2.add_row("Packet Count", str(sig_decoded["packet_count"]))
    t2.add_row("Sample Count", str(sig_decoded["sample_count"]))
    t2.add_row("Encoded Size", f"{len(sig_bytes)} bytes")
    t2.add_row("Timestamp (UTC)", str(sig_decoded["timestamp_s"]))
    console.print(t2)

    # Show first 8 samples
    console.print("\n[dim]First 8 I/Q samples:[/dim]")
    st = RTable(box=box.SIMPLE, show_header=True, header_style="bold")
    st.add_column("Sample #", justify="right")
    st.add_column("I", justify="right", style="green")
    st.add_column("Q", justify="right", style="magenta")
    for idx, s in enumerate(sig_decoded["iq_samples"][:8]):
        st.add_row(str(idx), str(s["I"]), str(s["Q"]))
    console.print(st)
    console.print()
