from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field, model_validator
from enum import Enum
import logging
import os
import json
import time
from datetime import datetime
import tarfile
import io
from fastapi.responses import StreamingResponse

# ---------------------------------------------
# PRISMA Experiment Control API
# ---------------------------------------------
# This API controls motor and brake configurations for
# data acquisition in the PRISMA project. Motor and
# brake combinations are actuated to generate simulation
# data for experiment analysis.

# Maximum allowed brake current (A) and PWM switching frequency (Hz)
MAX_BRAKE_AMPERAGE = 3.0
MAX_PWM_FREQUENCY = 1000.0  # Maximum PWM switching frequency

APP_VERSION = "default"
COMMIT_HASH = "default"
BUILD_DATE = "default"
MAINTAINER = "default"


# Configure logging to capture every request
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prisma_control_api")

app = FastAPI(
    title="PRISMA Experiment Control API",
    description="Control motor relays and brake settings to run measurements and retrieve data",
    version="1.0.0"
)

# State holders for motor and brake
target_motor_state = {
    "supply_left": False,
    "supply_right": False,
    "star": False,
    "delta_left": False,
    "delta_right": False
}
current_break_amperage: float = 0.0
pwm_settings = {"amperage": 0.0, "duty_cycle": 0.0, "frequency": 0.0}

# Ensure measurements directory exists
MEASUREMENT_DIR = "measurements"
os.makedirs(MEASUREMENT_DIR, exist_ok=True)

# Endpoint for setting individual motor relay outputs.
class MotorRelaisModel(BaseModel):
    supply_left: bool
    supply_right: bool
    star: bool
    delta_left: bool
    delta_right: bool

    @model_validator(mode='after')
    def validate_exclusivity(cls, values):
        if values.get("star") and (values.get("delta_left") or values.get("delta_right")):
            raise ValueError("star mode cannot be combined with delta modes")
        if values.get("delta_left") and values.get("delta_right"):
            raise ValueError("delta_left and delta_right cannot both be True")
        return values

@app.post("/set_motor_relais")
def set_motor_relais(relais: MotorRelaisModel):
    """
    Set individual motor relay outputs.
    """
    logger.info(f"[set_motor_relais] Received: {relais.dict()}")
    target_motor_state.update(relais.dict())
    return {"success": True, "state": target_motor_state}

# Endpoint for setting motor mode by selecting predefined relay combinations.
class MotorMode(str, Enum):
    off = "off"
    star_left = "star-left"
    star_right = "star-right"
    delta_left = "delta-left"
    delta_right = "delta-right"

class MotorModeModel(BaseModel):
    mode: MotorMode

@app.post("/set_motor_mode")
def set_motor_mode(model: MotorModeModel):
    """
    Set motor mode by selecting predefined relay combinations.
    """
    logger.info(f"[set_motor_mode] Mode: {model.mode}")
    mode_map = {
        MotorMode.off:        {k: False for k in target_motor_state},
        MotorMode.star_left:  {"supply_left": True,  "supply_right": False, "star": True,  "delta_left": False, "delta_right": False},
        MotorMode.star_right: {"supply_left": False, "supply_right": True,  "star": True,  "delta_left": False, "delta_right": False},
        MotorMode.delta_left: {"supply_left": True,  "supply_right": False, "star": False, "delta_left": True,  "delta_right": False},
        MotorMode.delta_right:{"supply_left": False, "supply_right": True,  "star": False, "delta_left": False, "delta_right": True}
    }
    target_motor_state.update(mode_map[model.mode])
    return {"success": True, "state": target_motor_state}

# Endpoint for configuring brake via PWM settings.
class BreakPwmModel(BaseModel):
    amperage: float = Field(..., ge=0.0, le=MAX_BRAKE_AMPERAGE)
    duty_cycle: float = Field(..., ge=0.0, le=100.0)
    frequency: float = Field(..., ge=0.0, le=MAX_PWM_FREQUENCY)

@app.post("/set_break_pwm")
def set_break_pwm(b: BreakPwmModel):
    """
    Configure brake SSR in PWM mode with given settings.
    frequency=0 and duty_cycle=100% corresponds to steady current output.
    Reject request if on/off times shorter than half-period at max frequency.
    """
    # Minimum half-period at maximum frequency
    min_half_period = 1.0 / (2.0 * MAX_PWM_FREQUENCY)
    # Calculate on/off durations for requested settings
    if b.frequency > 0:
        period = 1.0 / b.frequency
        on_time = (b.duty_cycle / 100.0) * period
        off_time = period - on_time
        if on_time < min_half_period or off_time < min_half_period:
            logger.error(
                f"[set_break_pwm] PWM period too low: on_time={on_time}, off_time={off_time}, min_half_period={min_half_period}"
            )
            raise HTTPException(
                status_code=400,
                detail=f"PWM period too low: minimum half-period at max frequency is {min_half_period} seconds"
            )
    global current_break_amperage, pwm_settings
    current_break_amperage = b.amperage
    pwm_settings = b.dict()
    logger.info(f"[set_break_pwm] PWM settings: {pwm_settings}")
    return {"success": True, "pwm": pwm_settings}

# Endpoint for setting brake current by direct amperage using PWM mode.
class BreakAmperageModel(BaseModel):
    amperage: float = Field(..., ge=0.0, le=MAX_BRAKE_AMPERAGE)

@app.post("/set_break_amperage")
def set_break_amperage_endpoint(b: BreakAmperageModel):
    """
    Set brake current between 0 and MAX_BRAKE_AMPERAGE A as a special PWM case.
    """
    return set_break_pwm(BreakPwmModel(amperage=b.amperage, duty_cycle=100.0, frequency=0.0))

# Endpoint for setting brake current by percentage using PWM mode.
class BreakPercentageModel(BaseModel):
    percentage: float = Field(..., ge=0.0, le=100.0)

@app.post("/set_break_percentage")
def set_break_percentage(b: BreakPercentageModel):
    """
    Set brake current by percentage of max amperage as PWM.
    """
    amperage = (b.percentage / 100.0) * MAX_BRAKE_AMPERAGE
    return set_break_pwm(BreakPwmModel(amperage=amperage, duty_cycle=100.0, frequency=0.0))

# Endpoint to start a timed measurement and store results.
class MeasurementModel(BaseModel):
    duration: float = Field(..., gt=0)
    title: str
    extra: dict = Field(default_factory=dict)

    @model_validator(mode='before')
    def extract_extra_fields(cls, values):
        extras = {k: v for k, v in values.items() if k not in ("duration", "title")}
        values["extra"] = extras
        return values


def perform_measurement(folder: str, metadata: dict, duration: float):
    time.sleep(duration)
    with open(os.path.join(folder, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=4)

@app.post("/start_measurement")
def start_measurement(model: MeasurementModel, background_tasks: BackgroundTasks):
    """
    Start a timed measurement and store results.
    Returns folder identifier immediately.
    """
    logger.info(f"[start_measurement] Params: {{duration: {model.duration}, title: '{model.title}', extras: {model.extra}}}")
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    folder = os.path.join(MEASUREMENT_DIR, timestamp)
    os.makedirs(folder, exist_ok=True)
    metadata = {
        "timestamp": timestamp,
        "title": model.title,
        "motor_state": target_motor_state,
        "break_amperage": current_break_amperage,
        **model.extra
    }
    background_tasks.add_task(perform_measurement, folder, metadata, model.duration)
    return {"success": True, "folder": timestamp}

# Endpoint to retrieve all measurement metadata.
@app.get("/get_measurements")
def get_measurements():
    """
    Retrieve all measurement metadata.
    """
    logger.info("[get_measurements] Retrieving metadata for all measurements")
    results = []
    for entry in os.listdir(MEASUREMENT_DIR):
        path = os.path.join(MEASUREMENT_DIR, entry)
        meta_file = os.path.join(path, "metadata.json")
        if os.path.isdir(path) and os.path.isfile(meta_file):
            with open(meta_file) as f:
                results.append(json.load(f))
    return results

# Endpoint to download multiple measurement sets as a single tar archive.
class DownloadModel(BaseModel):
    timestamps: list[str]

@app.post("/dl_measurements")
def dl_measurements(model: DownloadModel):
    """
    Download multiple measurement sets as a single tar.
    Utilizes StreamingResponse to stream data chunks.
    """
    logger.info(f"[dl_measurements] Requested: {model.timestamps}")
    def stream_archives():
        outer_buffer = io.BytesIO()
        with tarfile.open(fileobj=outer_buffer, mode='w') as outer_tar:
            for ts in model.timestamps:
                folder = os.path.join(MEASUREMENT_DIR, ts)
                if not os.path.isdir(folder):
                    continue
                bz2_buf = io.BytesIO()
                with tarfile.open(fileobj=bz2_buf, mode='w:bz2') as bz2_tar:
                    bz2_tar.add(folder, arcname=ts)
                bz2_buf.seek(0)
                info = tarfile.TarInfo(name=f"{ts}.tar.bz2")
                info.size = len(bz2_buf.getvalue())
                outer_tar.addfile(info, bz2_buf)
        outer_buffer.seek(0)
        while True:
            chunk = outer_buffer.read(8192)
            if not chunk:
                break
            yield chunk

    return StreamingResponse(
        stream_archives(),
        media_type='application/x-tar',
        headers={
            'Content-Disposition': f'attachment; filename="download_{datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")}.tar"'
        }
    )

@app.get("/version", tags=["Info"])
async def version():
    """
    This is a healthcare endpoint that returns the version information of the application.

    Returns:
        dict: A dictionary containing the version information.
    """
    return {
        "app_version": APP_VERSION,
        "commit_hash": COMMIT_HASH,
        "build_date": BUILD_DATE,
        "maintainer": MAINTAINER,
    }


# Allow running via `python main.py`
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
