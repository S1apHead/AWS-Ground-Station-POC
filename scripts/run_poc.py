"""
AWS Ground Station POC — Full End-to-End Runner
-------------------------------------------------
Runs all protocol layers in sequence and saves sample data to JSON.
"""
import sys, os, json, time
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console
from rich.panel   import Panel
from rich.table   import Table as RTable
from rich         import box

from vita49.vita49_packet     import VITA49Encoder, VITA49Decoder, VITA49ContextFields, LEOSignalSimulator
from ccsds.ccsds_tm_frame     import CCSDSTMEncoder, SatelliteTelemetrySimulator, VirtualChannel
from kinesis.kinesis_producer import TelemetryStreamProducer
from iot.mqtt_publisher       import SatelliteMQTTPublisher, IoTConfig
from docker.dataflow_endpoint import run_simulation

console = Console()

def run_protocol_demo():
    console.print(Panel(
        "[bold cyan]AWS Ground Station — Full Protocol Stack POC[/bold cyan]\n\n"
        "[white]Protocol Layers:[/white]\n"
        "  [cyan]1.[/cyan] VITA 49 (VRT)   — I/Q signal data over UDP\n"
        "  [cyan]2.[/cyan] CCSDS TM        — Telemetry Transfer Frames\n"
        "  [cyan]3.[/cyan] Kinesis Streams  — Decoded telemetry fan-out\n"
        "  [cyan]4.[/cyan] MQTT / IoT Core  — Real-time device telemetry\n"
        "  [cyan]5.[/cyan] Full E2E         — Simulated contact pass",
        border_style="cyan", title="SpaceNet-IT | AWS Ground Station POC"
    ))
    time.sleep(0.5)

    # ── Layer 1: VITA 49 ──────────────────────────────────────────────────────
    console.rule("[bold cyan]Layer 1 — VITA 49 (VRT) Packets[/bold cyan]")
    enc = VITA49Encoder(stream_id=0xABCD1234)
    dec = VITA49Decoder()
    sim = LEOSignalSimulator(carrier_freq_hz=8_100_000, sample_rate_hz=25_000_000, snr_db=14)

    ctx = VITA49ContextFields(
        rf_freq_hz=8_100_000_000.0, bandwidth_hz=25_000_000.0,
        sample_rate_hz=25_000_000.0, gain_db=42.0, ref_level_dbm=-85.0,
        polarisation="RHCP", satellite_id="SPACENET-1A",
        ground_station="AWS-GS-SYDNEY", contact_id="CONTACT-2026-001"
    )
    ctx_bytes = enc.encode_context_packet(ctx)
    iq_samples = sim.generate_samples(512)
    sig_bytes  = enc.encode_signal_packet(iq_samples)

    ctx_dec = dec.decode(ctx_bytes)
    sig_dec = dec.decode(sig_bytes)

    t = RTable(box=box.ROUNDED, header_style="bold blue", title="VITA 49 Packets")
    t.add_column("Packet", style="cyan")
    t.add_column("Type", style="yellow")
    t.add_column("Size (bytes)", justify="right")
    t.add_column("Key Fields", style="dim")
    t.add_row("Context", ctx_dec["type_name"], str(len(ctx_bytes)),
              f"freq={ctx.rf_freq_hz/1e9:.1f}GHz BW={ctx.bandwidth_hz/1e6:.0f}MHz")
    t.add_row("Signal Data", sig_dec["type_name"], str(len(sig_bytes)),
              f"samples={sig_dec['sample_count']} ts={sig_dec['timestamp_s']}")
    console.print(t)

    # Save sample VITA 49 data
    sample_vita49 = {
        "context_packet": ctx_dec,
        "signal_packet":  {k: v for k, v in sig_dec.items() if k != "iq_samples"},
        "first_8_samples": sig_dec["iq_samples"][:8],
    }
    with open("sample-data/vita49_sample.json", "w") as f:
        json.dump(sample_vita49, f, indent=2)
    console.print("[dim]  ✓ VITA 49 sample saved to sample-data/vita49_sample.json[/dim]")

    # ── Layer 2: CCSDS TM ─────────────────────────────────────────────────────
    console.rule("[bold cyan]Layer 2 — CCSDS TM Transfer Frames[/bold cyan]")
    sat_sim  = SatelliteTelemetrySimulator(spacecraft_id=0x1A2B, satellite_name="SPACENET-1A")
    tm_codec = CCSDSTMEncoder()
    hk_data  = sat_sim.generate_hk_telemetry()

    frames_sample = []
    for vc in [VirtualChannel.HOUSEKEEPING, VirtualChannel.SCIENCE_DATA]:
        raw   = sat_sim.generate_tm_frame(vc)
        frame = tm_codec.decode_frame(raw)
        frames_sample.append({"vc": vc.name, "meta": frame})

        t2 = RTable(box=box.ROUNDED, header_style="bold blue",
                    title=f"CCSDS TM Frame — VC{vc.value}: {vc.name}")
        t2.add_column("Field", style="cyan")
        t2.add_column("Value", style="white")
        for k, v in frame.items():
            t2.add_row(k, str(v))
        console.print(t2)

    with open("sample-data/ccsds_frames_sample.json", "w") as f:
        json.dump({"frames": frames_sample, "hk_telemetry": hk_data}, f, indent=2, default=str)
    console.print("[dim]  ✓ CCSDS sample saved to sample-data/ccsds_frames_sample.json[/dim]")

    # ── Layer 3: Kinesis ──────────────────────────────────────────────────────
    console.rule("[bold cyan]Layer 3 — Kinesis Data Streams[/bold cyan]")
    producer = TelemetryStreamProducer(local_mode=True)
    for i in range(5):
        hk = sat_sim.generate_hk_telemetry()
        producer.publish_hk_telemetry(hk)

    producer.publish_anomaly_alert("SPACENET-1A", "thm_battery_temp_c", 53.2, 45.0, "WARNING")
    producer.publish_state_change("SPACENET-1A", "NOMINAL", "SAFE_MODE", "Thermal threshold")

    k_stats = producer.hk_stream.get_stats()
    t3 = RTable(box=box.ROUNDED, header_style="bold blue", title="Kinesis Stats")
    t3.add_column("Metric", style="cyan")
    t3.add_column("Value", justify="right", style="white")
    for k, v in k_stats.items():
        t3.add_row(k, str(v))
    console.print(t3)

    with open("sample-data/kinesis_records_sample.json", "w") as f:
        json.dump({"records": producer.hk_stream._records[:3]}, f, indent=2, default=str)
    console.print("[dim]  ✓ Kinesis sample saved to sample-data/kinesis_records_sample.json[/dim]")

    # ── Layer 4: MQTT ─────────────────────────────────────────────────────────
    console.rule("[bold cyan]Layer 4 — IoT Core MQTT[/bold cyan]")
    mqtt_pub = SatelliteMQTTPublisher("SPACENET-1A", IoTConfig(), local_mode=True)
    for i in range(5):
        hk = sat_sim.generate_hk_telemetry()
        mqtt_pub.publish_hk(hk)
        mqtt_pub.publish_power(hk)
        mqtt_pub.publish_adcs(hk)
        mqtt_pub.publish_orbital(hk)

    topic_counts = {}
    for log in mqtt_pub.client._log:
        topic_counts[log["topic"]] = topic_counts.get(log["topic"], 0) + 1

    t4 = RTable(box=box.ROUNDED, header_style="bold blue", title="MQTT Topics Published")
    t4.add_column("Topic", style="cyan")
    t4.add_column("Count", justify="right", style="green")
    for topic_pub, cnt in topic_counts.items():
        t4.add_row(topic_pub, str(cnt))
    console.print(t4)

    with open("sample-data/mqtt_messages_sample.json", "w") as f:
        json.dump({"messages": mqtt_pub.client._log[:5]}, f, indent=2, default=str)
    console.print("[dim]  ✓ MQTT sample saved to sample-data/mqtt_messages_sample.json[/dim]")

    # ── Layer 5: Full E2E ─────────────────────────────────────────────────────
    console.rule("[bold cyan]Layer 5 — Full End-to-End Contact Simulation[/bold cyan]")
    run_simulation(n_contacts=1, frames_per_contact=10, verbose=True)

    console.print(Panel(
        "[bold green]✓ POC Complete[/bold green]\n\n"
        "Sample data saved to [cyan]sample-data/[/cyan]:\n"
        "  • vita49_sample.json\n"
        "  • ccsds_frames_sample.json\n"
        "  • kinesis_records_sample.json\n"
        "  • mqtt_messages_sample.json\n\n"
        "[dim]Next steps:\n"
        "  1. Deploy dataflow_endpoint.py as ECS Fargate container in VPC\n"
        "  2. Configure AWS Ground Station contact profile → UDP dataflow endpoint\n"
        "  3. Replace local_mode=True with real boto3 / IoT Core clients\n"
        "  4. Add Timestream writer for time-series storage\n"
        "  5. Wire IoT TwinMaker to MQTT orbital telemetry topic[/dim]",
        border_style="green", title="SpaceNet-IT | AWS Ground Station POC"
    ))


if __name__ == "__main__":
    os.makedirs("sample-data", exist_ok=True)
    run_protocol_demo()
