# IKA-PRISMA Edge Control API Service
This service is meant to control the PRISMA industrial experiment. 
It will be run on a skAInet Edge Compute device.

## Building the executable

### Set your build-version

The `pyproject.toml` as well as the `app/main.py` include information about the version of this build.
Edit them manually:
```sh
APP_VERSION
COMMIT_HASH
BUILD_DATE
MAINTAINER
```

### Build-Environment
Setup a build env and install all dependencies:
```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install .
```

### Build the executable
Use PyInstaller to generate the `./dist/control_service/control_service` executable:
```sh
pyinstaller --name control_service app/main.py
```

