"""
CCSDS Telemetry Transfer Frame Encoder / Decoder
-------------------------------------------------
Standard: CCSDS 132.0-B-2 (TM Space Data Link Protocol)

After VITA 49 I/Q samples are demodulated by the AWS Ground Station
software-defined radio, they produce CCSDS TM Transfer Frames.
These frames carry the satellite's housekeeping telemetry.

TM Transfer Frame Structure (fixed 1115 bytes in this POC):
  ┌────────────────────────────────────────────────────────┐
  │ Primary Header (6 bytes)                               │
  │   [15:14] Transfer Frame Version Number = 00           │
  │   [13:0]  Spacecraft ID (SCID) — 14 bits               │
  │   [2]     Virtual Channel ID (VCID) — 3 bits           │
  │   [2]     OCF Flag                                     │
  │   [7]     Master Channel Frame Count (0-255)           │
  │   [7]     Virtual Channel Frame Count (0-255)          │
  │   Transfer Frame Data Field Status (2 bytes)           │
  ├────────────────────────────────────────────────────────┤
  │ Secondary Header (optional, variable)                  │
  │   Timestamp (CCSDS CDS — 8 bytes)                      │
  ├────────────────────────────────────────────────────────┤
  │ Data Field — Space Packets                             │
  │   CCSDS Space Packets (variable, up to frame boundary) │
  ├────────────────────────────────────────────────────────┤
  │ OCF — Operational Control Field (4 bytes, if present)  │
  ├────────────────────────────────────────────────────────┤
  │ Frame Error Control — CRC-16/CCITT (2 bytes)           │
  └────────────────────────────────────────────────────────┘

Virtual Channels (VCID):
  VC0 — Real-Time Housekeeping (HK)
  VC1 — Stored Telemetry
  VC2 — Payload Science Data
  VC3 — Fill / Idle Frames
"""

import struct
import time
import random
import json
import math
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import IntEnum


# ── Constants ─────────────────────────────────────────────────────────────────
FRAME_VERSION         = 0b00          # CCSDS TM version
FRAME_LENGTH          = 1115          # Standard frame length (bytes)
PRIMARY_HEADER_LEN    = 6
SECONDARY_HEADER_LEN  = 10            # 2 byte ext header id + 8 byte timestamp
FECF_LEN              = 2            # Frame Error Control Field (CRC-16)
OCF_LEN               = 4

class VirtualChannel(IntEnum):
    HOUSEKEEPING  = 0   # Real-time HK telemetry
    STORED_TM     = 1   # Stored/delayed telemetry
    SCIENCE_DATA  = 2   # Payload science
    FILL          = 3   # Idle fill frames


# ── CCSDS Space Packet (inside TM frame data field) ───────────────────────────
@dataclass
class SpacePacket:
    """CCSDS Space Packet — CCSDS 133.0-B-2"""
    apid: int                    # Application Process ID (11 bits)
    sequence_count: int          # 14-bit rolling count
    data: bytes                  # Packet data field
    packet_type: int = 0         # 0=telemetry, 1=telecommand
    secondary_header: bool = True


class SpacePacketEncoder:
    def encode(self, pkt: SpacePacket) -> bytes:
        # Primary header (6 bytes)
        # Word 1: version(3) + type(1) + sec_hdr(1) + apid(11)
        word1 = (0b000 << 13) | (pkt.packet_type << 12) | \
                (int(pkt.secondary_header) << 11) | (pkt.apid & 0x7FF)
        # Word 2: seq_flags(2=standalone) + seq_count(14)
        word2 = (0b11 << 14) | (pkt.sequence_count & 0x3FFF)
        # Word 3: data length - 1
        data_len = len(pkt.data) - 1
        header = struct.pack(">HHH", word1, word2, data_len)
        return header + pkt.data

    def decode(self, raw: bytes) -> SpacePacket:
        if len(raw) < 6:
            raise ValueError("Packet too short")
        w1, w2, data_len = struct.unpack(">HHH", raw[:6])
        apid    = w1 & 0x7FF
        seq_cnt = w2 & 0x3FFF
        pkt_type = (w1 >> 12) & 0x1
        sec_hdr  = bool((w1 >> 11) & 0x1)
        data     = raw[6:6 + data_len + 1]
        return SpacePacket(apid=apid, sequence_count=seq_cnt,
                           data=data, packet_type=pkt_type,
                           secondary_header=sec_hdr)


# ── TM Frame ──────────────────────────────────────────────────────────────────
@dataclass
class TMFrame:
    spacecraft_id: int          # SCID — 14 bits (0-16383)
    virtual_channel: VirtualChannel
    mc_frame_count: int         # Master channel count (0-255)
    vc_frame_count: int         # Virtual channel count (0-255)
    timestamp_s: int            # Seconds since J2000
    timestamp_subsec: int       # Subseconds
    data_field: bytes           # Space packets payload
    has_ocf: bool = False
    ocf: bytes = field(default_factory=lambda: b"\x00\x00\x00\x00")


# ── CRC-16/CCITT ─────────────────────────────────────────────────────────────
def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


# ── Encoder ───────────────────────────────────────────────────────────────────
class CCSDSTMEncoder:
    def encode_frame(self, frame: TMFrame) -> bytes:
        """Encode a TM Transfer Frame to bytes"""

        # ── Primary Header (6 bytes) ──────────────────────────────────────────
        # Byte 0-1: Version(2b) + SCID(14b) — but CCSDS uses 2b version + 10b SCID
        # We use 14-bit SCID split across bytes
        scid_msb = (frame.spacecraft_id >> 4) & 0x3FF   # 10 MSBs
        vcid     = int(frame.virtual_channel) & 0x7
        ocf_flag = int(frame.has_ocf)

        # Byte 0: TFVN(2) + SCID[9:2] (8 bits)
        byte0 = ((FRAME_VERSION & 0x3) << 6) | ((frame.spacecraft_id >> 8) & 0x3F)
        # Byte 1: SCID[1:0](2) + VCID(3) + OCF(1) + rsvd(2)
        byte1 = ((frame.spacecraft_id & 0xFF))
        # Byte 2-3: Master Ch Frame Count + VCID info
        byte2 = frame.mc_frame_count & 0xFF
        byte3 = ((vcid & 0x7) << 5) | (ocf_flag << 4) | 0x00
        # Byte 4-5: VC Frame Count + data field status
        byte4 = frame.vc_frame_count & 0xFF
        byte5 = 0x00  # No first header pointer for simplicity

        primary_header = bytes([byte0, byte1, byte2, byte3, byte4, byte5])

        # ── Secondary Header (10 bytes) ──────────────────────────────────────
        # 2-byte secondary header ID + 8-byte CCSDS CDS timestamp
        sec_hdr_id = struct.pack(">H", 0x0001)  # Version 1, no additional fields
        # CCSDS CDS (Consultative Committee Day Segmented) time
        # Day number since J2000 epoch + ms of day + submillisecond
        j2000_epoch = 946728000  # Unix timestamp of J2000 (2000-01-01 12:00:00 UTC)
        delta_s = max(0, frame.timestamp_s - j2000_epoch)
        day     = delta_s // 86400
        ms_day  = (delta_s % 86400) * 1000
        cds_time = struct.pack(">HI", day & 0xFFFF, ms_day & 0xFFFFFFFF)
        secondary_header = sec_hdr_id + cds_time

        # ── Data Field ───────────────────────────────────────────────────────
        # Pad/trim to fill frame (minus headers, OCF, FECF)
        overhead = PRIMARY_HEADER_LEN + SECONDARY_HEADER_LEN + FECF_LEN
        if frame.has_ocf:
            overhead += OCF_LEN
        data_field_len = FRAME_LENGTH - overhead
        data = frame.data_field[:data_field_len]
        # Pad with idle fill (0xE0) if needed
        data = data.ljust(data_field_len, b"\xE0")

        # ── OCF (if present) ─────────────────────────────────────────────────
        ocf = frame.ocf if frame.has_ocf else b""

        # ── Assemble pre-FECF ────────────────────────────────────────────────
        pre_fecf = primary_header + secondary_header + data + ocf

        # ── FECF (CRC-16) ────────────────────────────────────────────────────
        crc   = crc16_ccitt(pre_fecf)
        fecf  = struct.pack(">H", crc)

        return pre_fecf + fecf

    def decode_frame(self, raw: bytes) -> dict:
        """Decode a raw TM frame back to its fields"""
        if len(raw) < PRIMARY_HEADER_LEN + FECF_LEN:
            raise ValueError("Frame too short")

        # Verify CRC
        received_crc  = struct.unpack(">H", raw[-2:])[0]
        computed_crc  = crc16_ccitt(raw[:-2])
        crc_ok        = received_crc == computed_crc

        b = raw
        # Primary header
        tfvn  = (b[0] >> 6) & 0x3
        scid  = ((b[0] & 0x3F) << 8) | b[1]
        mc_cnt = b[2]
        vcid  = (b[3] >> 5) & 0x7
        ocf_f = (b[3] >> 4) & 0x1
        vc_cnt = b[4]

        # Secondary header
        sh_offset = PRIMARY_HEADER_LEN
        day       = struct.unpack(">H", raw[sh_offset+2:sh_offset+4])[0]
        ms_day    = struct.unpack(">I", raw[sh_offset+4:sh_offset+8])[0]

        # Data field
        data_start = PRIMARY_HEADER_LEN + SECONDARY_HEADER_LEN
        data_end   = len(raw) - FECF_LEN - (OCF_LEN if ocf_f else 0)
        data_field = raw[data_start:data_end]

        return {
            "tfvn":            tfvn,
            "spacecraft_id":   scid,
            "virtual_channel": VirtualChannel(vcid).name,
            "vcid":            vcid,
            "mc_frame_count":  mc_cnt,
            "vc_frame_count":  vc_cnt,
            "has_ocf":         bool(ocf_f),
            "timestamp_day":   day,
            "timestamp_ms":    ms_day,
            "data_field_len":  len(data_field),
            "crc_valid":       crc_ok,
            "frame_len":       len(raw),
        }


# ── Satellite Telemetry Simulator ─────────────────────────────────────────────
class SatelliteTelemetrySimulator:
    """Generate realistic satellite housekeeping telemetry packets"""

    def __init__(self, spacecraft_id: int = 0x1A2B, satellite_name: str = "SPACENET-1A"):
        self.spacecraft_id  = spacecraft_id
        self.satellite_name = satellite_name
        self._mc_count = 0
        self._vc_counts = {vc: 0 for vc in VirtualChannel}
        self._pkt_seq  = 0
        self._orbit_angle = 0.0  # Degrees in orbit

    def _next_mc(self) -> int:
        c = self._mc_count % 256
        self._mc_count += 1
        return c

    def _next_vc(self, vc: VirtualChannel) -> int:
        c = self._vc_counts[vc] % 256
        self._vc_counts[vc] += 1
        return c

    def _next_pkt_seq(self) -> int:
        s = self._pkt_seq % 16384
        self._pkt_seq += 1
        return s

    def _advance_orbit(self, degrees: float = 0.5):
        self._orbit_angle = (self._orbit_angle + degrees) % 360

    def generate_hk_telemetry(self) -> dict:
        """Generate housekeeping telemetry parameter values"""
        self._advance_orbit()
        angle_rad = math.pi * self._orbit_angle / 180

        # Solar panel power varies with orbit (eclipse when behind Earth)
        in_eclipse = self._orbit_angle > 160 and self._orbit_angle < 200
        solar_power = 0.0 if in_eclipse else abs(math.sin(angle_rad)) * 185.0 + random.uniform(-5, 5)

        return {
            # Power subsystem
            "pwr_bus_voltage_v":        round(28.0 + random.uniform(-0.3, 0.3), 3),
            "pwr_bus_current_a":        round(3.2 + random.uniform(-0.2, 0.2), 3),
            "pwr_battery_soc_pct":      round(max(20, min(100, 85 + math.sin(angle_rad)*15)), 1),
            "pwr_solar_power_w":        round(max(0, solar_power), 2),
            "pwr_in_eclipse":           in_eclipse,

            # Thermal subsystem
            "thm_obc_temp_c":           round(22.0 + random.uniform(-3, 3), 2),
            "thm_battery_temp_c":       round(18.0 + random.uniform(-2, 2), 2),
            "thm_solar_panel_temp_c":   round(-40.0 + math.cos(angle_rad)*80 + random.uniform(-5,5), 2),
            "thm_rx_temp_c":            round(25.0 + random.uniform(-2, 2), 2),

            # Attitude Determination & Control
            "adc_mode":                 "NADIR_POINTING",
            "adc_roll_deg":             round(random.uniform(-0.05, 0.05), 4),
            "adc_pitch_deg":            round(random.uniform(-0.05, 0.05), 4),
            "adc_yaw_deg":              round(random.uniform(-0.1, 0.1), 4),
            "adc_roll_rate_dps":        round(random.uniform(-0.001, 0.001), 5),
            "adc_pitch_rate_dps":       round(random.uniform(-0.001, 0.001), 5),

            # On-Board Computer
            "obc_cpu_load_pct":         round(random.uniform(12, 35), 1),
            "obc_mem_used_pct":         round(random.uniform(40, 55), 1),
            "obc_uptime_s":             self._mc_count * 10,
            "obc_mode":                 "NOMINAL",
            "obc_last_cmd_apid":        "0x100",

            # Communications
            "com_rx_rssi_dbm":          round(-85 + random.uniform(-5, 5), 1),
            "com_tx_power_dbm":         round(30.0 + random.uniform(-0.5, 0.5), 2),
            "com_tx_freq_hz":           8_100_000_000,
            "com_rx_freq_hz":           2_025_000_000,
            "com_link_quality_pct":     round(random.uniform(85, 99), 1),

            # Orbital state
            "orb_altitude_km":          round(550 + random.uniform(-0.5, 0.5), 3),
            "orb_velocity_km_s":        round(7.6 + random.uniform(-0.01, 0.01), 4),
            "orb_latitude_deg":         round(math.sin(angle_rad) * 53.0, 4),
            "orb_longitude_deg":        round((self._orbit_angle * 2.7) % 360 - 180, 4),

            # Metadata
            "satellite_id":             self.satellite_name,
            "timestamp_utc":            int(time.time()),
            "frame_sequence":           self._mc_count,
        }

    def generate_tm_frame(self, vc: VirtualChannel = VirtualChannel.HOUSEKEEPING) -> bytes:
        """Generate a complete encoded TM Transfer Frame with HK telemetry"""

        hk_data  = self.generate_hk_telemetry()
        hk_bytes = json.dumps(hk_data).encode("utf-8")

        sp_encoder = SpacePacketEncoder()
        space_pkt  = SpacePacketEncoder().encode(SpacePacket(
            apid=0x100 if vc == VirtualChannel.HOUSEKEEPING else 0x200,
            sequence_count=self._next_pkt_seq(),
            data=hk_bytes,
        ))

        frame = TMFrame(
            spacecraft_id   = self.spacecraft_id,
            virtual_channel = vc,
            mc_frame_count  = self._next_mc(),
            vc_frame_count  = self._next_vc(vc),
            timestamp_s     = int(time.time()),
            timestamp_subsec= 0,
            data_field      = space_pkt,
        )

        encoder = CCSDSTMEncoder()
        return encoder.encode_frame(frame)


# ── Demo ──────────────────────────────────────────────────────────────────────
import math

if __name__ == "__main__":
    from rich.console import Console
    from rich.table import Table as RTable
    from rich import box

    console = Console()
    console.print("\n[bold cyan]CCSDS TM Transfer Frames — Satellite Telemetry POC[/bold cyan]")
    console.print("[dim]CCSDS 132.0-B-2 | Space Packets | CCSDS 133.0-B-2[/dim]\n")

    sim     = SatelliteTelemetrySimulator(spacecraft_id=0x1A2B, satellite_name="SPACENET-1A")
    decoder = CCSDSTMEncoder()

    # Generate 3 frames on different VCs
    for vc in [VirtualChannel.HOUSEKEEPING, VirtualChannel.SCIENCE_DATA, VirtualChannel.FILL]:
        raw   = sim.generate_tm_frame(vc)
        frame = decoder.decode_frame(raw)

        console.print(f"[bold yellow]► TM Frame — VC{vc.value}: {vc.name}[/bold yellow]")
        t = RTable(box=box.ROUNDED, header_style="bold blue")
        t.add_column("Field", style="cyan")
        t.add_column("Value", style="white")
        for k, v in frame.items():
            t.add_row(k, str(v))
        console.print(t)
        console.print()

    # Show HK telemetry parameters
    console.print("[bold yellow]► Housekeeping Telemetry Parameters[/bold yellow]")
    hk = sim.generate_hk_telemetry()
    t2 = RTable(box=box.ROUNDED, header_style="bold blue")
    t2.add_column("Parameter", style="cyan")
    t2.add_column("Value", style="green")
    for k, v in hk.items():
        t2.add_row(k, str(v))
    console.print(t2)
