"""
AWS Ground Station Dataflow Endpoint — UDP Receiver
-----------------------------------------------------
AWS Ground Station delivers VITA 49 packets over UDP to a
"dataflow endpoint" — an EC2 instance or ECS container running
inside the customer VPC.

This module simulates the dataflow endpoint:
  1. Listens on UDP port 55888 (AWS Ground Station default)
  2. Receives VITA 49 packets from the antenna
  3. Parses signal data and context packets
  4. Extracts CCSDS TM frames from signal packet payloads
  5. Decodes housekeeping telemetry
  6. Publishes to Kinesis + IoT Core

AWS Ground Station Dataflow Architecture:
  Antenna
    └► VITA 49 UDP → Dataflow Endpoint (this service)
         ├► Kinesis Data Streams (decoded telemetry)
         ├► IoT Core MQTT (real-time parameters)
         └► S3 (raw frame archive)

Dockerfile for deployment:
  FROM python:3.12-slim
  COPY . /app
  WORKDIR /app
  RUN pip install -r requirements.txt
  EXPOSE 55888/udp
  CMD ["python3", "docker/dataflow_endpoint.py", "--listen"]
"""

import socket
import struct
import threading
import time
import json
import sys
import os
import argparse
from datetime import datetime, timezone
from collections import deque


sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from vita49.vita49_packet  import VITA49Decoder, VITA49Encoder, VITA49ContextFields, LEOSignalSimulator, PKT_TYPE_SIGNAL_DATA, PKT_TYPE_CONTEXT
from ccsds.ccsds_tm_frame  import CCSDSTMEncoder, SatelliteTelemetrySimulator, VirtualChannel
from kinesis.kinesis_producer import TelemetryStreamProducer
from iot.mqtt_publisher    import SatelliteMQTTPublisher, IoTConfig


# ── Config ────────────────────────────────────────────────────────────────────
UDP_HOST    = "0.0.0.0"
UDP_PORT    = 55888          # AWS Ground Station default dataflow port
BUFFER_SIZE = 65536          # Max UDP datagram size
MAX_QUEUE   = 10000          # Internal frame queue depth


class DataflowEndpoint:
    """
    Simulates the AWS Ground Station dataflow endpoint container.
    Receives VITA 49 UDP packets and processes them into the AWS pipeline.
    """

    def __init__(self, local_mode: bool = True):
        self.local_mode  = local_mode
        self.vita_decoder = VITA49Decoder()
        self.tm_decoder   = CCSDSTMEncoder()
        self._frame_queue = deque(maxlen=MAX_QUEUE)
        self._running     = False
        self._stats       = {
            "packets_received": 0,
            "signal_packets":   0,
            "context_packets":  0,
            "tm_frames_decoded":0,
            "hk_records_pub":   0,
            "errors":           0,
            "bytes_received":   0,
            "contacts":         0,
        }

        # AWS pipeline clients
        self.kinesis  = TelemetryStreamProducer(local_mode=local_mode)
        self.mqtt_pub = SatelliteMQTTPublisher(
            "SPACENET-1A",
            IoTConfig(),
            local_mode=local_mode
        )
        self._current_context = None

    def process_vita49_packet(self, data: bytes) -> None:
        """Process a single VITA 49 UDP packet"""
        try:
            pkt = self.vita_decoder.decode(data)
            self._stats["packets_received"] += 1
            self._stats["bytes_received"]   += len(data)

            if pkt.get("type_name") == "CONTEXT":
                self._current_context = pkt.get("context", {})
                self._stats["context_packets"] += 1

            elif pkt.get("type_name") == "SIGNAL_DATA":
                self._stats["signal_packets"] += 1
                self._frame_queue.append({
                    "vita49": pkt,
                    "context": self._current_context,
                    "received_at": time.time(),
                })

        except Exception as e:
            self._stats["errors"] += 1

    def process_ccsds_frame(self, frame_bytes: bytes, context: dict) -> None:
        """Decode CCSDS frame and publish telemetry"""
        try:
            frame_meta = self.tm_decoder.decode_frame(frame_bytes)
            self._stats["tm_frames_decoded"] += 1

            if not frame_meta["crc_valid"]:
                return

            # Extract data field and parse as HK JSON (our POC format)
            data_field = frame_bytes[16:-2]
            # Strip CCSDS space packet header (6 bytes)
            if len(data_field) > 6:
                sp_data = data_field[6:]
                try:
                    hk = json.loads(sp_data.rstrip(b"\xe0\x00").decode("utf-8"))
                    hk["ground_station"] = context.get("ground_station", "UNKNOWN") if context else "UNKNOWN"
                    hk["contact_id"]     = context.get("contact_id", "UNKNOWN") if context else "UNKNOWN"
                    hk["vc"]             = frame_meta["virtual_channel"]
                    hk["mc_count"]       = frame_meta["mc_frame_count"]

                    # Publish to Kinesis
                    self.kinesis.publish_hk_telemetry(hk)
                    self._stats["hk_records_pub"] += 1

                    # Publish to IoT Core MQTT
                    self.mqtt_pub.publish_hk(hk)
                    self.mqtt_pub.publish_power(hk)
                    self.mqtt_pub.publish_adcs(hk)
                    self.mqtt_pub.publish_orbital(hk)

                    # Threshold checks
                    self._check_thresholds(hk)

                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

        except Exception as e:
            self._stats["errors"] += 1

    def _check_thresholds(self, hk: dict) -> None:
        """Check telemetry against alarm thresholds"""
        thresholds = {
            "thm_battery_temp_c":  (None, 45.0),
            "thm_obc_temp_c":      (None, 55.0),
            "pwr_bus_voltage_v":   (26.0, 30.0),
            "pwr_battery_soc_pct": (20.0, None),
            "com_link_quality_pct":(70.0, None),
        }
        for param, (low, high) in thresholds.items():
            val = hk.get(param)
            if val is None:
                continue
            if high and val > high:
                self.kinesis.publish_anomaly_alert(
                    hk.get("satellite_id", "UNKNOWN"), param, val, high, "WARNING")
                self.mqtt_pub.publish_anomaly(param, val, high, "WARNING")
            if low and val < low:
                self.kinesis.publish_anomaly_alert(
                    hk.get("satellite_id", "UNKNOWN"), param, val, low, "WARNING")
                self.mqtt_pub.publish_anomaly(param, val, low, "WARNING")

    def get_stats(self) -> dict:
        return dict(self._stats)


# ── Simulation Mode (no real UDP socket needed) ───────────────────────────────
def run_simulation(n_contacts: int = 1, frames_per_contact: int = 20,
                   verbose: bool = True):
    """
    Simulate a full ground station contact pass end-to-end.
    Generates VITA 49 → CCSDS → Kinesis → MQTT pipeline.
    """
    from rich.console import Console
    from rich.table import Table as RTable
    from rich.panel import Panel
    from rich import box

    console = Console()
    console.print(Panel(
        "[bold cyan]AWS Ground Station — Dataflow Endpoint Simulation[/bold cyan]\n"
        "[dim]VITA 49 UDP → CCSDS TM Decode → Kinesis + IoT Core[/dim]",
        border_style="blue"
    ))

    endpoint  = DataflowEndpoint(local_mode=True)
    vita_enc  = VITA49Encoder(stream_id=0xABCD1234)
    sig_sim   = LEOSignalSimulator(carrier_freq_hz=8_100_000, sample_rate_hz=25_000_000, snr_db=14)
    sat_sim   = SatelliteTelemetrySimulator(spacecraft_id=0x1A2B, satellite_name="SPACENET-1A")

    for contact_num in range(1, n_contacts + 1):
        console.print(f"\n[bold green]▶ Contact {contact_num}/{n_contacts}[/bold green] — SPACENET-1A via AWS-GS-SYDNEY")

        # Publish contact start
        contact_meta = {
            "satellite_id":   "SPACENET-1A",
            "ground_station": "AWS-GS-SYDNEY",
            "contact_id":     f"CONTACT-2026-{contact_num:03d}",
            "elevation_max":  72.3,
            "duration_s":     520,
        }
        endpoint.kinesis.publish_contact_start(contact_meta)

        # Encode and send context packet
        ctx = VITA49ContextFields(
            rf_freq_hz=8_100_000_000.0, bandwidth_hz=25_000_000.0,
            sample_rate_hz=25_000_000.0, gain_db=42.0, ref_level_dbm=-85.0,
            polarisation="RHCP", satellite_id="SPACENET-1A",
            ground_station="AWS-GS-SYDNEY",
            contact_id=contact_meta["contact_id"],
        )
        ctx_pkt = vita_enc.encode_context_packet(ctx)
        endpoint.process_vita49_packet(ctx_pkt)

        # Stream frames
        for i in range(frames_per_contact):
            # 1. Generate CCSDS TM frame
            tm_raw = sat_sim.generate_tm_frame(VirtualChannel.HOUSEKEEPING)

            # 2. Pack TM frame bytes as VITA 49 signal payload
            #    (In reality these are I/Q samples that get demodulated to TM)
            #    For POC: we simulate the demodulated output directly
            endpoint.process_ccsds_frame(tm_raw, ctx.__dict__ if ctx else None)

            if verbose and i % 5 == 0:
                stats = endpoint.get_stats()
                console.print(
                    f"  [dim]Frame {i+1:3d}/{frames_per_contact}"
                    f" | TM decoded: {stats['tm_frames_decoded']}"
                    f" | HK published: {stats['hk_records_pub']}"
                    f" | Errors: {stats['errors']}[/dim]"
                )
            time.sleep(0.02)

        # Contact end
        endpoint.kinesis.publish_contact_end(contact_meta)
        console.print(f"  [green]✓ Contact {contact_num} complete[/green]")

    # Final stats
    console.print("\n")
    stats = endpoint.get_stats()
    console.print("[bold yellow]► Dataflow Endpoint Statistics[/bold yellow]")
    t = RTable(box=box.ROUNDED, header_style="bold blue")
    t.add_column("Metric", style="cyan")
    t.add_column("Value", justify="right", style="white")
    for k, v in stats.items():
        t.add_row(k.replace("_", " ").title(), str(v))
    console.print(t)

    console.print("\n[bold yellow]► Kinesis HK Stream Stats[/bold yellow]")
    k_stats = endpoint.kinesis.hk_stream.get_stats()
    t2 = RTable(box=box.ROUNDED, header_style="bold blue")
    t2.add_column("Metric", style="cyan")
    t2.add_column("Value", justify="right", style="white")
    for k, v in k_stats.items():
        t2.add_row(k.replace("_", " ").title(), str(v))
    console.print(t2)

    console.print("\n[bold yellow]► IoT Core MQTT Stats[/bold yellow]")
    m_stats = endpoint.mqtt_pub.get_stats()
    t3 = RTable(box=box.ROUNDED, header_style="bold blue")
    t3.add_column("Metric", style="cyan")
    t3.add_column("Value", justify="right", style="white")
    for k, v in m_stats.items():
        t3.add_row(k.replace("_", " ").title(), str(v))
    console.print(t3)

    # Sample Kinesis record
    console.print("\n[bold yellow]► Sample Kinesis HK Record[/bold yellow]")
    if endpoint.kinesis.hk_stream._records:
        console.print_json(json.dumps(
            endpoint.kinesis.hk_stream._records[0]["data"], indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AWS Ground Station Dataflow Endpoint POC")
    parser.add_argument("--contacts", type=int, default=1, help="Number of contacts to simulate")
    parser.add_argument("--frames",   type=int, default=20, help="Frames per contact")
    args = parser.parse_args()
    run_simulation(n_contacts=args.contacts, frames_per_contact=args.frames)
