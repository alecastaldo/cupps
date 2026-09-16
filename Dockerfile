# CUPPS-in-a-box: a complete CUPPS environment with no airport attached.
#
# This image is NOT how the application reaches a workstation. A CUPPS
# workstation runs the application as a native process launched from the
# platform's own menu, inheriting drive-letter mapped storage and the Windows
# Spooler (see docs/DEPLOYMENT.md section 5). Containers are for the two jobs
# that happen away from the workstation:
#
#   1. CI -- the test suite and the conformance harness are self-contained.
#   2. DCS onboarding -- a departure control team gets a working platform,
#      device set and agent application with one command, instead of waiting
#      for airport lab time.
#
FROM python:3.11-slim

LABEL org.opencontainers.image.title="CUPPS-in-a-box" \
      org.opencontainers.image.description="CUPPS 01.04 platform simulator, \
device handler and conformance harness for development and CI" \
      org.opencontainers.image.source="https://github.com/alecastaldo/cupps"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/cupps

WORKDIR /opt/cupps

# Dependencies first, so a source change does not re-resolve them.
COPY pyproject.toml ./
RUN pip install --no-cache-dir pycryptodome && \
    adduser --disabled-password --gecos "" --uid 10001 cupps

COPY cupps/ ./cupps/
COPY cuppsd/ ./cuppsd/
COPY simulator/ ./simulator/
COPY conformance/ ./conformance/
COPY tools/ ./tools/
COPY webui/ ./webui/
COPY cuppsit.py ./

USER cupps

# The agent UI. The simulator's own ports are ephemeral by default; the
# compose file pins them when a DCS team needs to reach them directly.
EXPOSE 8631

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,sys,json; \
r=json.load(urllib.request.urlopen('http://127.0.0.1:8631/api/health',timeout=4)); \
sys.exit(0 if r.get('ok') else 1)"

# Bind to all interfaces *inside the container only*; the compose file maps it
# to the loopback adapter on the host. A workstation deployment keeps the
# handler on 127.0.0.1 (docs/DEPLOYMENT.md section 5.4).
CMD ["python3", "-m", "cuppsd", "--simulator", "--airline", "ZZ", \
     "--airline-name", "CUPPS IN A BOX", "--host", "0.0.0.0", "--port", "8631"]
