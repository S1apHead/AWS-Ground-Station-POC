"""
contact-reporter — SpaceNet Fleet Management Microservice
LLD Ref: LLD-FM-001

Responsibilities:
  - Generate post-contact reports after every ground pass
  - Compute link budget metrics (Eb/N0, SNR, data volume)
  - Retrieve telemetry summary from Timestream for the contact window
  - Write report JSON to S3 contact-reports bucket
  - Update DynamoDB contacts table with report S3 URI
"""

import os
import json
import math
import logging
import datetime
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

AWS_REGION         = os.getenv("AWS_REGION", "ap-southeast-2")
S3_REPORTS_BUCKET  = os.getenv("S3_REPORTS_BUCKET", "spacenet-contact-reports")
DYNAMODB_CONTACTS  = os.getenv("DYNAMODB_CONTACTS_TABLE", "spacenet-contacts")
TIMESTREAM_DATABASE = os.getenv("TIMESTREAM_DATABASE", "spacenet-telemetry")
TIMESTREAM_HK_TABLE = os.getenv("TIMESTREAM_HK_TABLE", "satellite_hk")
LOCAL_MODE         = os.getenv("LOCAL_MODE", "true").lower() == "true"


@dataclass
class LinkBudget:
    eirp_dbw:           float   # Effective Isotropic Radiated Power
    free_space_loss_db: float   # Free-space path loss
    g_t_db:             float   # Ground station G/T
    received_power_dbw: float   # C (carrier power)
    noise_density_dbw_hz: float # N0
    eb_n0_db:           float   # Energy per bit to noise density
    snr_db:             float   # Signal-to-noise ratio
    link_margin_db:     float   # Margin above required Eb/N0
    data_rate_kbps:     float
    contact_duration_s: int
    data_volume_mb:     float


@dataclass
class ContactReport:
    report_id:      str
    contact_id:     str
    satellite_id:   str
    ground_station: str
    contact_start:  str
    contact_end:    str
    max_elevation_deg: float
    link_budget:    dict
    telemetry_summary: dict
    anomalies_count: int
    frames_received: int
    frame_loss_pct:  float
    generated_at:   str
    s3_uri:         Optional[str] = None


class LinkBudgetCalculator:
    """
    Simplified Friis transmission link budget calculator.
    """
    BOLTZMANN_DB = -228.6  # dBW/Hz/K (10*log10(1.38e-23))

    def calculate(
        self,
        freq_mhz: float,          # Carrier frequency
        satellite_altitude_km: float,
        elevation_deg: float,
        tx_power_w: float,        # Satellite TX power
        tx_gain_dbi: float,       # Satellite antenna gain
        rx_g_t_db: float,         # Ground station G/T
        data_rate_kbps: float,
        contact_duration_s: int,
        required_eb_n0_db: float = 6.0,  # QPSK at BER 1e-6
    ) -> LinkBudget:
        # Slant range (approximate)
        RE = 6371.0
        h  = satellite_altitude_km
        el = math.radians(elevation_deg)
        slant_km = (-RE * math.sin(el) +
                     math.sqrt((RE * math.sin(el))**2 + h**2 + 2*RE*h))

        # EIRP
        eirp_dbw = 10 * math.log10(tx_power_w) + tx_gain_dbi

        # Free-space path loss (Friis)
        fsl_db = (20 * math.log10(freq_mhz * 1e6) +
                  20 * math.log10(slant_km * 1e3) -
                  147.55)  # 20*log10(4π/c)

        # Received carrier power
        c_dbw = eirp_dbw - fsl_db + rx_g_t_db

        # Noise density
        n0_dbw = self.BOLTZMANN_DB + 290  # 290K noise temp approximation + 10*log10(k*T)

        # Eb/N0
        rb_db    = 10 * math.log10(data_rate_kbps * 1e3)
        eb_n0_db = c_dbw - n0_dbw - rb_db
        snr_db   = c_dbw - (n0_dbw + rb_db)
        margin   = eb_n0_db - required_eb_n0_db

        # Data volume
        data_mb  = data_rate_kbps * contact_duration_s / 8 / 1024

        return LinkBudget(
            eirp_dbw           = round(eirp_dbw, 2),
            free_space_loss_db = round(fsl_db, 2),
            g_t_db             = rx_g_t_db,
            received_power_dbw = round(c_dbw, 2),
            noise_density_dbw_hz = round(n0_dbw, 2),
            eb_n0_db           = round(eb_n0_db, 2),
            snr_db             = round(snr_db, 2),
            link_margin_db     = round(margin, 2),
            data_rate_kbps     = data_rate_kbps,
            contact_duration_s = contact_duration_s,
            data_volume_mb     = round(data_mb, 2),
        )


class ContactReporter:
    def __init__(self):
        self._lb_calc = LinkBudgetCalculator()
        self._reports: list[ContactReport] = []
        self._local_mode = LOCAL_MODE

        if not LOCAL_MODE:
            import boto3
            self._s3     = boto3.client("s3", region_name=AWS_REGION)
            self._dynamo = boto3.resource("dynamodb", region_name=AWS_REGION)
            self._ts     = boto3.client("timestream-query", region_name=AWS_REGION,
                                        endpoint_discovery_enabled=True)

    def generate_report(self, contact_info: dict) -> ContactReport:
        import uuid
        report_id   = str(uuid.uuid4())[:8]
        duration_s  = int(contact_info.get("duration_s", 600))
        max_el      = float(contact_info.get("max_elevation_deg", 30.0))
        altitude_km = float(contact_info.get("altitude_km", 550.0))

        # Link budget (S-band TT&C parameters)
        lb = self._lb_calc.calculate(
            freq_mhz            = 2250.0,
            satellite_altitude_km = altitude_km,
            elevation_deg       = max_el,
            tx_power_w          = 2.0,          # 2W S-band TX
            tx_gain_dbi         = 6.0,           # patch antenna
            rx_g_t_db           = 18.0,          # AWS GS G/T
            data_rate_kbps      = contact_info.get("data_rate_kbps", 64.0),
            contact_duration_s  = duration_s,
        )

        # Simulate telemetry summary (Timestream query in production)
        frames_rx    = int(duration_s * 0.95)  # 95% frame receive rate
        frame_loss   = round((1 - frames_rx / max(1, duration_s)) * 100, 2)

        report = ContactReport(
            report_id      = report_id,
            contact_id     = contact_info.get("contact_id", "UNKNOWN"),
            satellite_id   = contact_info.get("satellite_id", "UNKNOWN"),
            ground_station = contact_info.get("ground_station", "UNKNOWN"),
            contact_start  = contact_info.get("start_time", ""),
            contact_end    = contact_info.get("end_time", ""),
            max_elevation_deg = max_el,
            link_budget    = asdict(lb),
            telemetry_summary = {
                "hk_packets_received": frames_rx,
                "avg_battery_soc_pct": 85.3,
                "avg_obc_temp_c":      24.1,
                "avg_attitude_err_deg": 0.08,
            },
            anomalies_count = contact_info.get("anomalies_count", 0),
            frames_received = frames_rx,
            frame_loss_pct  = frame_loss,
            generated_at    = datetime.datetime.utcnow().isoformat() + "Z",
        )

        self._reports.append(report)

        if LOCAL_MODE:
            self._print_report(report)
        else:
            self._save_to_s3(report)
            self._update_dynamo(report)

        return report

    def _print_report(self, r: ContactReport):
        logger.info("Post-Contact Report [%s]", r.report_id)
        logger.info("  Contact : %s", r.contact_id)
        logger.info("  Sat     : %s @ %s", r.satellite_id, r.ground_station)
        logger.info("  Elevation: %.1f°  Duration: %ds", r.max_elevation_deg,
                    r.link_budget["contact_duration_s"])
        logger.info("  Eb/N0   : %.1f dB (margin: %.1f dB)", r.link_budget["eb_n0_db"],
                    r.link_budget["link_margin_db"])
        logger.info("  Data    : %.1f MB  Frames: %d  Loss: %.1f%%",
                    r.link_budget["data_volume_mb"], r.frames_received, r.frame_loss_pct)

    def _save_to_s3(self, report: ContactReport):
        key = (f"contacts/{report.satellite_id}/"
               f"{datetime.datetime.utcnow().strftime('%Y/%m/%d')}/"
               f"{report.contact_id}-{report.report_id}.json")
        try:
            self._s3.put_object(
                Bucket=S3_REPORTS_BUCKET,
                Key=key,
                Body=json.dumps(asdict(report), indent=2).encode(),
                ContentType="application/json",
                ServerSideEncryption="aws:kms",
            )
            report.s3_uri = f"s3://{S3_REPORTS_BUCKET}/{key}"
            logger.info("Report saved: %s", report.s3_uri)
        except Exception as exc:
            logger.error("S3 upload error: %s", exc)

    def _update_dynamo(self, report: ContactReport):
        try:
            tbl = self._dynamo.Table(DYNAMODB_CONTACTS)
            tbl.update_item(
                Key={"contact_id": report.contact_id},
                UpdateExpression=(
                    "SET report_s3_uri = :uri, frame_loss_pct = :fl, "
                    "data_volume_mb = :dv, eb_n0_db = :en"
                ),
                ExpressionAttributeValues={
                    ":uri": report.s3_uri or "",
                    ":fl":  str(report.frame_loss_pct),
                    ":dv":  str(report.link_budget["data_volume_mb"]),
                    ":en":  str(report.link_budget["eb_n0_db"]),
                },
            )
        except Exception as exc:
            logger.error("DynamoDB update error: %s", exc)


if __name__ == "__main__":
    reporter = ContactReporter()

    sample_contacts = [
        {
            "contact_id":        "CNT-001",
            "satellite_id":      "SN-001",
            "ground_station":    "PERTH",
            "start_time":        "2026-06-06T02:00:00Z",
            "end_time":          "2026-06-06T02:10:00Z",
            "duration_s":        600,
            "max_elevation_deg": 42.5,
            "altitude_km":       550.0,
            "data_rate_kbps":    256.0,
            "anomalies_count":   0,
        },
        {
            "contact_id":        "CNT-002",
            "satellite_id":      "SN-002",
            "ground_station":    "SANTIAGO",
            "start_time":        "2026-06-06T04:30:00Z",
            "end_time":          "2026-06-06T04:37:30Z",
            "duration_s":        450,
            "max_elevation_deg": 18.2,
            "altitude_km":       550.0,
            "data_rate_kbps":    128.0,
            "anomalies_count":   1,
        },
    ]

    print(f"\n{'='*60}")
    print("SpaceNet Contact Reporter")
    print(f"{'='*60}")
    for contact in sample_contacts:
        r = reporter.generate_report(contact)
        print(f"\n  Report: {r.report_id}  |  {r.satellite_id} @ {r.ground_station}")
        print(f"  Eb/N0: {r.link_budget['eb_n0_db']} dB  "
              f"Margin: {r.link_budget['link_margin_db']} dB")
        print(f"  Data:  {r.link_budget['data_volume_mb']} MB  "
              f"Frame loss: {r.frame_loss_pct}%")
