"""
contact-scheduler — SpaceNet Fleet Management Microservice
LLD Ref: LLD-FM-001

Responsibilities:
  - Poll satellite TLEs and compute upcoming pass windows via SGP4
  - Reserve contacts with AWS Ground Station API (or local simulator)
  - Conflict resolution with priority queue
  - Publish contact schedule events to EventBridge / DynamoDB
"""

import os
import json
import math
import logging
import datetime
from dataclasses import dataclass, asdict
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

# ── Configuration ─────────────────────────────────────────────────────────────
AWS_REGION          = os.getenv("AWS_REGION", "ap-southeast-2")
DYNAMODB_TABLE      = os.getenv("DYNAMODB_CONTACTS_TABLE", "spacenet-contacts")
MISSION_PROFILE_ARN = os.getenv("MISSION_PROFILE_ARN", "")
LOCAL_MODE          = os.getenv("LOCAL_MODE", "true").lower() == "true"
SCHEDULER_HORIZON_H = int(os.getenv("SCHEDULER_HORIZON_HOURS", "24"))
MIN_ELEVATION_DEG   = float(os.getenv("MIN_ELEVATION_DEG", "5.0"))


# ── Data Classes ─────────────────────────────────────────────────────────────
@dataclass
class PassWindow:
    satellite_id: str
    ground_station_id: str
    aos: datetime.datetime        # Acquisition of Signal
    los: datetime.datetime        # Loss of Signal
    max_elevation_deg: float
    duration_s: int
    priority: int = 2             # 1=CRITICAL, 2=HIGH, 3=NORMAL


@dataclass
class ScheduledContact:
    contact_id: str
    satellite_id: str
    ground_station_id: str
    start_time: str
    end_time: str
    status: str
    mission_profile_arn: str
    max_elevation_deg: float


# ── Orbital Pass Prediction (simplified SGP4-like logic for simulation) ───────
class OrbitPredictor:
    """Simplified pass predictor (replace with sgp4 library in production)."""

    EARTH_RADIUS_KM = 6371.0

    def __init__(self, tle_line1: str, tle_line2: str):
        self.tle_line1 = tle_line1
        self.tle_line2 = tle_line2
        # Parse mean motion from TLE line 2 (revs per day)
        self.revs_per_day = float(tle_line2[52:63].strip())
        self.inclination_deg = float(tle_line2[8:16].strip())
        self.period_min = 1440.0 / self.revs_per_day

    def get_pass_windows(
        self,
        gs_lat: float,
        gs_lon: float,
        gs_alt_km: float,
        start: datetime.datetime,
        end: datetime.datetime,
        min_elevation_deg: float = 5.0,
    ) -> list[PassWindow]:
        """Return simulated pass windows over the given time window."""
        windows = []
        t = start
        dt = datetime.timedelta(minutes=self.period_min * 0.8)

        # Simulate passes at orbital period intervals
        while t < end:
            # Simulate a pass with random-ish elevation based on inclination
            peak_el = abs(math.sin(math.radians(self.inclination_deg)) * 60.0
                          + (hash(str(t)) % 20 - 10))
            if peak_el >= min_elevation_deg:
                aos = t + datetime.timedelta(minutes=2)
                dur_s = max(300, int(peak_el * 12))
                los = aos + datetime.timedelta(seconds=dur_s)
                if los <= end:
                    windows.append(PassWindow(
                        satellite_id="SN-001",
                        ground_station_id="PERTH",
                        aos=aos,
                        los=los,
                        max_elevation_deg=round(peak_el, 1),
                        duration_s=dur_s,
                    ))
            t += dt

        return windows


# ── AWS Ground Station (or local simulator) ───────────────────────────────────
class LocalGroundStationSimulator:
    def __init__(self):
        self._contacts: dict[str, ScheduledContact] = {}

    def reserve_contact(
        self, satellite_id: str, mission_profile_arn: str,
        start: datetime.datetime, end: datetime.datetime,
        gs_id: str, max_el: float,
    ) -> str:
        import uuid
        contact_id = str(uuid.uuid4())[:8]
        self._contacts[contact_id] = ScheduledContact(
            contact_id=contact_id,
            satellite_id=satellite_id,
            ground_station_id=gs_id,
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            status="SCHEDULED",
            mission_profile_arn=mission_profile_arn,
            max_elevation_deg=max_el,
        )
        logger.info("[LOCAL-GS] Reserved contact %s for %s @ %s",
                    contact_id, satellite_id, start.isoformat())
        return contact_id

    def list_contacts(self) -> list[ScheduledContact]:
        return list(self._contacts.values())


class ContactScheduler:
    def __init__(self):
        if LOCAL_MODE:
            self._gs = LocalGroundStationSimulator()
            self._dynamo = None
        else:
            self._gs_client  = boto3.client("groundstation", region_name=AWS_REGION)
            self._dynamo     = boto3.resource("dynamodb", region_name=AWS_REGION)
            self._table      = self._dynamo.Table(DYNAMODB_TABLE)
            self._events     = boto3.client("events", region_name=AWS_REGION)

    def schedule_horizon(self, satellites: list[dict]) -> list[ScheduledContact]:
        """Schedule all passes for every satellite in the next SCHEDULER_HORIZON_H hours."""
        now = datetime.datetime.utcnow()
        horizon = now + datetime.timedelta(hours=SCHEDULER_HORIZON_H)
        contacts: list[ScheduledContact] = []

        # Ground stations (stub data — pull from Ground Station API in production)
        ground_stations = [
            {"id": "PERTH",    "lat": -31.95, "lon": 115.86, "alt_km": 0.02},
            {"id": "SANTIAGO", "lat": -33.45, "lon": -70.67, "alt_km": 0.57},
            {"id": "FAIRBANKS","lat":  64.84, "lon": -147.72, "alt_km": 0.13},
        ]

        for sat in satellites:
            predictor = OrbitPredictor(sat["tle_line1"], sat["tle_line2"])
            for gs in ground_stations:
                windows = predictor.get_pass_windows(
                    gs["lat"], gs["lon"], gs["alt_km"], now, horizon,
                    min_elevation_deg=MIN_ELEVATION_DEG,
                )
                for w in windows:
                    w.satellite_id = sat["satellite_id"]
                    w.ground_station_id = gs["id"]
                    c = self._reserve(sat["satellite_id"], w)
                    if c:
                        contacts.append(c)

        return contacts

    def _reserve(self, satellite_id: str, window: PassWindow) -> Optional[ScheduledContact]:
        try:
            if LOCAL_MODE:
                contact_id = self._gs.reserve_contact(
                    satellite_id=satellite_id,
                    mission_profile_arn=MISSION_PROFILE_ARN or "arn:local:mp:spacenet",
                    start=window.aos,
                    end=window.los,
                    gs_id=window.ground_station_id,
                    max_el=window.max_elevation_deg,
                )
                contact = ScheduledContact(
                    contact_id=contact_id,
                    satellite_id=satellite_id,
                    ground_station_id=window.ground_station_id,
                    start_time=window.aos.isoformat(),
                    end_time=window.los.isoformat(),
                    status="SCHEDULED",
                    mission_profile_arn=MISSION_PROFILE_ARN or "arn:local",
                    max_elevation_deg=window.max_elevation_deg,
                )
            else:
                resp = self._gs_client.reserve_contact(
                    groundStationId=window.ground_station_id,
                    missionProfileArn=MISSION_PROFILE_ARN,
                    satelliteArn=f"arn:aws:groundstation:::{satellite_id}",
                    startTime=window.aos,
                    endTime=window.los,
                )
                contact = ScheduledContact(
                    contact_id=resp["contactId"],
                    satellite_id=satellite_id,
                    ground_station_id=window.ground_station_id,
                    start_time=window.aos.isoformat(),
                    end_time=window.los.isoformat(),
                    status="SCHEDULED",
                    mission_profile_arn=MISSION_PROFILE_ARN,
                    max_elevation_deg=window.max_elevation_deg,
                )
                # Persist to DynamoDB
                self._table.put_item(Item=asdict(contact))

            return contact

        except Exception as exc:
            logger.error("Failed to reserve contact for %s: %s", satellite_id, exc)
            return None


# ── Sample TLEs (ISS-like placeholder) ───────────────────────────────────────
SAMPLE_SATELLITES = [
    {
        "satellite_id": "SN-001",
        "name": "SpaceNet-1",
        "tle_line1": "1 25544U 98067A   24001.50000000  .00001264  00000-0  29521-4 0  9993",
        "tle_line2": "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49559389432869",
    },
    {
        "satellite_id": "SN-002",
        "name": "SpaceNet-2",
        "tle_line1": "1 25545U 98067B   24001.60000000  .00001000  00000-0  23000-4 0  9991",
        "tle_line2": "2 25545  51.6416 250.0000 0006700 135.0000 320.0000 15.49600000432870",
    },
]


if __name__ == "__main__":
    scheduler = ContactScheduler()
    contacts = scheduler.schedule_horizon(SAMPLE_SATELLITES)

    print(f"\n{'='*60}")
    print(f"SpaceNet Contact Scheduler — {len(contacts)} contacts scheduled")
    print(f"Horizon: {SCHEDULER_HORIZON_H}h | Mode: {'LOCAL' if LOCAL_MODE else 'AWS'}")
    print(f"{'='*60}")
    for c in contacts[:5]:
        print(f"  [{c.contact_id}] {c.satellite_id} @ {c.ground_station_id}")
        print(f"    AOS: {c.start_time}  |  El: {c.max_elevation_deg}°")
    if len(contacts) > 5:
        print(f"  ... and {len(contacts) - 5} more contacts")
