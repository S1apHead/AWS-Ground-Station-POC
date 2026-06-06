"""
orbit-propagator — SpaceNet Fleet Management Microservice
LLD Ref: LLD-FM-001

Responsibilities:
  - Fetch current TLEs from Space-Track.org (or local cache)
  - Propagate orbital state using SGP4/SDP4 (simplified analytic model here)
  - Publish orbital state to DynamoDB and IoT TwinMaker
  - Provide REST endpoint for real-time position queries
  - Run pass prediction for contact scheduler

The production implementation uses the `sgp4` Python library (pip install sgp4)
which implements the full SGP4/SDP4 orbital propagator.
"""

import os
import math
import json
import time
import logging
import datetime
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

LOCAL_MODE      = os.getenv("LOCAL_MODE", "true").lower() == "true"
AWS_REGION      = os.getenv("AWS_REGION", "ap-southeast-2")
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL_S", "30"))

# Physical constants
MU_KM3_S2  = 398600.4418    # Earth gravitational parameter
RE_KM      = 6378.137        # Earth radius
J2         = 1.08262668e-3   # J2 perturbation coefficient
OMEGA_EARTH = 7.2921150e-5   # Earth rotation rate (rad/s)


@dataclass
class OrbitalState:
    satellite_id:   str
    timestamp_utc:  str
    # Keplerian elements
    semi_major_axis_km: float
    eccentricity:       float
    inclination_deg:    float
    raan_deg:           float          # Right Ascension of Ascending Node
    arg_perigee_deg:    float
    true_anomaly_deg:   float
    # Derived
    altitude_km:        float
    latitude_deg:       float
    longitude_deg:      float
    velocity_km_s:      float
    period_min:         float
    # Visibility
    eclipse:            bool
    sun_angle_deg:      float


class SGP4Propagator:
    """
    Simplified two-body + J2 analytic propagator (for simulation/testing).
    Production: replace with `from sgp4.api import Satrec` from the sgp4 library.
    """

    def __init__(self, tle_line1: str, tle_line2: str):
        self.tle_line1 = tle_line1
        self.tle_line2 = tle_line2
        self._parse_tle()

    def _parse_tle(self):
        l2 = self.tle_line2
        self.inclination  = math.radians(float(l2[8:16].strip()))
        self.raan         = math.radians(float(l2[17:25].strip()))
        self.eccentricity = float("0." + l2[26:33].strip())
        self.arg_perigee  = math.radians(float(l2[34:42].strip()))
        self.mean_anomaly = math.radians(float(l2[43:51].strip()))
        self.mean_motion  = float(l2[52:63].strip())  # rev/day
        self.n_rad_s      = self.mean_motion * 2 * math.pi / 86400.0  # rad/s
        self.period_s     = 2 * math.pi / self.n_rad_s
        self.semi_major   = (MU_KM3_S2 / self.n_rad_s**2) ** (1/3)

    def _kepler_solve(self, mean_anom: float, ecc: float, tol: float = 1e-10) -> float:
        """Solve Kepler's equation M = E - e*sin(E) via Newton-Raphson."""
        E = mean_anom
        for _ in range(50):
            dE = (mean_anom - E + ecc * math.sin(E)) / (1 - ecc * math.cos(E))
            E += dE
            if abs(dE) < tol:
                break
        return E

    def propagate(self, t: datetime.datetime) -> OrbitalState:
        """Propagate satellite to given UTC epoch."""
        # Time since TLE epoch (simplified: use current UTC offset)
        epoch_jd   = 2460310.5  # 2024 Jan 1 placeholder
        t_jd       = (t - datetime.datetime(2000, 1, 1, 12)).total_seconds() / 86400 + 2451545.0
        dt_s       = (t_jd - epoch_jd) * 86400

        # Propagate mean anomaly
        M = (self.mean_anomaly + self.n_rad_s * dt_s) % (2 * math.pi)

        # J2 secular drift in RAAN
        p  = self.semi_major * (1 - self.eccentricity**2)
        raan_drift = (-1.5 * self.n_rad_s * J2 * (RE_KM / p)**2
                       * math.cos(self.inclination) * dt_s)
        raan = (self.raan + raan_drift) % (2 * math.pi)

        # Eccentric anomaly → true anomaly
        E   = self._kepler_solve(M, self.eccentricity)
        nu  = 2 * math.atan2(
            math.sqrt(1 + self.eccentricity) * math.sin(E / 2),
            math.sqrt(1 - self.eccentricity) * math.cos(E / 2),
        )

        # Radius
        r_km    = self.semi_major * (1 - self.eccentricity * math.cos(E))
        alt_km  = r_km - RE_KM
        v_km_s  = math.sqrt(MU_KM3_S2 * (2/r_km - 1/self.semi_major))

        # Position in orbital plane
        u  = self.arg_perigee + nu
        # ECI coordinates (simplified)
        x  = r_km * (math.cos(raan) * math.cos(u) -
                      math.sin(raan) * math.sin(u) * math.cos(self.inclination))
        y  = r_km * (math.sin(raan) * math.cos(u) +
                      math.cos(raan) * math.sin(u) * math.cos(self.inclination))
        z  = r_km * (math.sin(self.inclination) * math.sin(u))

        # ECI → ECEF (approximate: Earth rotates by GAST)
        gast  = OMEGA_EARTH * (t.replace(tzinfo=None) -
                                datetime.datetime(2000, 1, 1, 12)).total_seconds()
        x_ecef = x * math.cos(gast) + y * math.sin(gast)
        y_ecef = -x * math.sin(gast) + y * math.cos(gast)
        z_ecef = z

        lat_rad = math.atan2(z_ecef, math.sqrt(x_ecef**2 + y_ecef**2))
        lon_rad = math.atan2(y_ecef, x_ecef)
        lat_deg = math.degrees(lat_rad)
        lon_deg = math.degrees(lon_rad)

        # Sun angle (simplified)
        day_of_year = t.timetuple().tm_yday
        sun_long    = math.radians(360 * (day_of_year - 81) / 365)
        sun_x       = math.cos(sun_long)
        sun_z       = math.sin(sun_long) * math.sin(math.radians(23.5))
        dot         = (x * sun_x + z * sun_z) / r_km
        sun_angle   = math.degrees(math.acos(max(-1, min(1, dot))))
        eclipse     = sun_angle > 90.0  # very simplified

        return OrbitalState(
            satellite_id       = "UNKNOWN",
            timestamp_utc      = t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            semi_major_axis_km = round(self.semi_major, 3),
            eccentricity       = round(self.eccentricity, 7),
            inclination_deg    = round(math.degrees(self.inclination), 4),
            raan_deg           = round(math.degrees(raan), 4),
            arg_perigee_deg    = round(math.degrees(self.arg_perigee), 4),
            true_anomaly_deg   = round(math.degrees(nu), 4),
            altitude_km        = round(alt_km, 2),
            latitude_deg       = round(lat_deg, 4),
            longitude_deg      = round(lon_deg, 4),
            velocity_km_s      = round(v_km_s, 4),
            period_min         = round(self.period_s / 60, 2),
            eclipse            = eclipse,
            sun_angle_deg      = round(sun_angle, 2),
        )


class OrbitPropagatorService:
    def __init__(self, satellites: list[dict]):
        self._satellites = satellites
        self._propagators = {
            sat["satellite_id"]: SGP4Propagator(sat["tle_line1"], sat["tle_line2"])
            for sat in satellites
        }

        if not LOCAL_MODE:
            import boto3
            self._dynamo    = boto3.resource("dynamodb", region_name=AWS_REGION)
            self._iot_twin  = boto3.client("iottwinmaker", region_name=AWS_REGION)

    def get_state(self, satellite_id: str,
                  t: Optional[datetime.datetime] = None) -> Optional[OrbitalState]:
        prop = self._propagators.get(satellite_id)
        if not prop:
            return None
        t = t or datetime.datetime.utcnow()
        state = prop.propagate(t)
        state.satellite_id = satellite_id
        return state

    def update_all(self):
        """Propagate all satellites and publish state."""
        t = datetime.datetime.utcnow()
        states = []
        for sat_id, prop in self._propagators.items():
            state = prop.propagate(t)
            state.satellite_id = sat_id
            states.append(state)
            logger.info(
                "[ORBIT] %s  lat=%.2f° lon=%.2f° alt=%.0fkm  eclipse=%s",
                sat_id, state.latitude_deg, state.longitude_deg,
                state.altitude_km, state.eclipse,
            )
            if not LOCAL_MODE:
                self._publish_to_dynamo(state)

        return states

    def _publish_to_dynamo(self, state: OrbitalState):
        try:
            tbl = self._dynamo.Table("spacenet-telemetry_state")
            tbl.update_item(
                Key={"satellite_id": state.satellite_id},
                UpdateExpression=(
                    "SET lat = :lat, lon = :lon, alt_km = :alt, "
                    "eclipse = :ec, velocity_km_s = :v, orbital_state = :os"
                ),
                ExpressionAttributeValues={
                    ":lat": str(state.latitude_deg),
                    ":lon": str(state.longitude_deg),
                    ":alt": str(state.altitude_km),
                    ":ec":  state.eclipse,
                    ":v":   str(state.velocity_km_s),
                    ":os":  json.dumps(asdict(state)),
                },
            )
        except Exception as exc:
            logger.error("DynamoDB update error: %s", exc)


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
    svc = OrbitPropagatorService(SAMPLE_SATELLITES)
    states = svc.update_all()

    print(f"\n{'='*60}")
    print("SpaceNet Orbit Propagator")
    print(f"{'='*60}")
    for s in states:
        print(f"\n  {s.satellite_id}")
        print(f"    Position : lat={s.latitude_deg}°  lon={s.longitude_deg}°")
        print(f"    Altitude : {s.altitude_km} km")
        print(f"    Velocity : {s.velocity_km_s} km/s")
        print(f"    Period   : {s.period_min} min")
        print(f"    Eclipse  : {s.eclipse}  Sun Angle: {s.sun_angle_deg}°")
