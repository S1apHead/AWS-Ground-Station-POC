"""
Kinesis Data Streams Producer
------------------------------
Streams decoded CCSDS telemetry from the dataflow endpoint processor
into Amazon Kinesis for real-time processing.

Flow:
  CCSDS TM Frame
      └► Lambda/ECS decoder
              └► Kinesis Data Streams   (this module)
                      ├► Lambda consumer → Timestream
                      ├► Lambda consumer → OpenSearch
                      └► Lambda consumer → IoT TwinMaker

Partition Key Strategy:
  - Housekeeping:  "HK-{spacecraft_id}"    → dedicated shard
  - Science data:  "SCI-{spacecraft_id}"   → dedicated shard
  - Events/alerts: "ALERT-{spacecraft_id}" → dedicated shard
"""

import json
import time
import random
import sys
import os
from datetime import datetime, timezone


# ── Local simulation mode (no AWS credentials needed for POC) ─────────────────
class LocalKinesisSimulator:
    """
    Simulates Kinesis put_record for local POC testing.
    Replace with boto3 kinesis client for real AWS deployment.
    """

    def __init__(self, stream_name: str):
        self.stream_name   = stream_name
        self.records_sent  = 0
        self.total_bytes   = 0
        self._records      = []

    def put_record(self, data: dict, partition_key: str) -> dict:
        payload = json.dumps(data).encode("utf-8")
        self.records_sent += 1
        self.total_bytes  += len(payload)
        seq = f"{int(time.time()*1000):020d}{self.records_sent:05d}"
        record = {
            "stream":        self.stream_name,
            "partition_key": partition_key,
            "sequence":      seq,
            "payload_bytes": len(payload),
            "data":          data,
        }
        self._records.append(record)
        return {"SequenceNumber": seq, "ShardId": f"shardId-{hash(partition_key) % 4:06d}"}

    def put_records_batch(self, records: list) -> dict:
        """Batch up to 500 records / 5MB"""
        results = []
        for r in records[:500]:
            result = self.put_record(r["data"], r["partition_key"])
            results.append(result)
        return {"Records": results, "FailedRecordCount": 0}

    def get_stats(self) -> dict:
        return {
            "stream_name":   self.stream_name,
            "records_sent":  self.records_sent,
            "total_bytes":   self.total_bytes,
            "avg_bytes_rec": round(self.total_bytes / max(1, self.records_sent), 1),
        }


def get_kinesis_client(stream_name: str, local_mode: bool = True):
    """Return local simulator or real boto3 client"""
    if local_mode:
        return LocalKinesisSimulator(stream_name)
    else:
        import boto3
        return boto3.client("kinesis", region_name=os.getenv("AWS_REGION", "ap-southeast-2"))


# ── Telemetry Record Builder ──────────────────────────────────────────────────
class TelemetryStreamProducer:
    """
    Formats decoded satellite telemetry and streams to Kinesis.
    Handles: HK telemetry, events, alerts, contact metadata.
    """

    STREAM_HK      = "spacenet-telemetry-hk"
    STREAM_SCIENCE = "spacenet-telemetry-science"
    STREAM_EVENTS  = "spacenet-telemetry-events"

    def __init__(self, local_mode: bool = True):
        self.hk_stream  = get_kinesis_client(self.STREAM_HK,      local_mode)
        self.sci_stream = get_kinesis_client(self.STREAM_SCIENCE,  local_mode)
        self.evt_stream = get_kinesis_client(self.STREAM_EVENTS,   local_mode)
        self.local_mode = local_mode

    def publish_hk_telemetry(self, telemetry: dict) -> dict:
        """Publish housekeeping telemetry record"""
        record = {
            "record_type":    "HK_TELEMETRY",
            "schema_version": "1.0",
            "ingested_at":    datetime.now(timezone.utc).isoformat(),
            "source":         "CCSDS_VC0",
            **telemetry,
        }
        pk = f"HK-{telemetry.get('satellite_id', 'UNKNOWN')}"
        return self.hk_stream.put_record(record, pk)

    def publish_contact_start(self, contact_meta: dict) -> dict:
        """Publish contact session start event"""
        record = {
            "record_type":  "CONTACT_START",
            "event_time":   datetime.now(timezone.utc).isoformat(),
            **contact_meta,
        }
        pk = f"CONTACT-{contact_meta.get('satellite_id', 'UNKNOWN')}"
        return self.evt_stream.put_record(record, pk)

    def publish_contact_end(self, contact_meta: dict) -> dict:
        """Publish contact session end event"""
        record = {
            "record_type":    "CONTACT_END",
            "event_time":     datetime.now(timezone.utc).isoformat(),
            **contact_meta,
        }
        pk = f"CONTACT-{contact_meta.get('satellite_id', 'UNKNOWN')}"
        return self.evt_stream.put_record(record, pk)

    def publish_anomaly_alert(self, satellite_id: str, parameter: str,
                               value: float, threshold: float,
                               severity: str = "WARNING") -> dict:
        """Publish telemetry anomaly alert"""
        record = {
            "record_type":  "ANOMALY_ALERT",
            "severity":     severity,
            "satellite_id": satellite_id,
            "parameter":    parameter,
            "value":        value,
            "threshold":    threshold,
            "event_time":   datetime.now(timezone.utc).isoformat(),
            "alert_id":     f"ALERT-{int(time.time()*1000)}",
        }
        pk = f"ALERT-{satellite_id}"
        return self.evt_stream.put_record(record, pk)

    def publish_state_change(self, satellite_id: str,
                              old_state: str, new_state: str,
                              reason: str) -> dict:
        """Publish satellite state machine transition"""
        record = {
            "record_type":  "STATE_CHANGE",
            "satellite_id": satellite_id,
            "old_state":    old_state,
            "new_state":    new_state,
            "reason":       reason,
            "event_time":   datetime.now(timezone.utc).isoformat(),
        }
        pk = f"STATE-{satellite_id}"
        return self.evt_stream.put_record(record, pk)


# ── Demo ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.path.append("..")
    from ccsds.ccsds_tm_frame import SatelliteTelemetrySimulator

    from rich.console import Console
    from rich.table import Table as RTable
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich import box

    console = Console()
    console.print("\n[bold cyan]Kinesis Data Streams — Telemetry Producer POC[/bold cyan]")
    console.print("[dim]Simulating 20 HK frames + events streaming to Kinesis[/dim]\n")

    sim      = SatelliteTelemetrySimulator(spacecraft_id=0x1A2B, satellite_name="SPACENET-1A")
    producer = TelemetryStreamProducer(local_mode=True)

    # Contact start
    contact_meta = {
        "satellite_id":   "SPACENET-1A",
        "ground_station": "AWS-GS-SYDNEY",
        "contact_id":     "CONTACT-2026-001",
        "elevation_max":  72.3,
        "duration_s":     520,
        "freq_uplink":    2_025_000_000,
        "freq_downlink":  8_100_000_000,
    }
    r = producer.publish_contact_start(contact_meta)
    console.print(f"[green]✓ Contact START published[/green] → shard {r['ShardId']}")

    # Stream HK telemetry
    console.print("\n[yellow]Streaming housekeeping telemetry...[/yellow]")
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TextColumn("{task.completed}/{task.total}")) as progress:
        task = progress.add_task("Publishing HK records", total=20)
        for i in range(20):
            hk = sim.generate_hk_telemetry()

            # Inject an anomaly at frame 10
            if i == 10:
                hk["thm_battery_temp_c"] = 52.3  # Over threshold

            r = producer.publish_hk_telemetry(hk)

            # Check thresholds
            if hk["thm_battery_temp_c"] > 45.0:
                producer.publish_anomaly_alert(
                    satellite_id="SPACENET-1A",
                    parameter="thm_battery_temp_c",
                    value=hk["thm_battery_temp_c"],
                    threshold=45.0,
                    severity="WARNING"
                )
                console.print(f"  [red]⚠ ANOMALY: battery temp {hk['thm_battery_temp_c']}°C > 45°C[/red]")

            if hk["pwr_in_eclipse"]:
                console.print(f"  [dim]  Eclipse detected at frame {i+1}[/dim]")

            progress.advance(task)
            time.sleep(0.05)

    # State change
    producer.publish_state_change("SPACENET-1A", "NOMINAL", "SAFE_MODE",
                                   "Battery temperature threshold exceeded")
    console.print("\n[red]⚡ State change NOMINAL → SAFE_MODE published[/red]")

    # Contact end
    contact_meta["frames_received"] = 20
    contact_meta["bytes_received"]  = 20 * 1115
    r = producer.publish_contact_end(contact_meta)
    console.print(f"[green]✓ Contact END published[/green] → shard {r['ShardId']}")

    # Stats
    console.print("\n[bold yellow]► Kinesis Stream Statistics[/bold yellow]")
    for client in [producer.hk_stream, producer.sci_stream, producer.evt_stream]:
        stats = client.get_stats()
        t = RTable(box=box.SIMPLE, show_header=False)
        t.add_column("K", style="cyan")
        t.add_column("V", style="white")
        for k, v in stats.items():
            t.add_row(k, str(v))
        console.print(t)

    # Sample records
    console.print("\n[bold yellow]► Sample Kinesis Record (HK Telemetry)[/bold yellow]")
    if producer.hk_stream._records:
        sample = producer.hk_stream._records[0]
        console.print_json(json.dumps(sample["data"], indent=2))
