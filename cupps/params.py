"""CUPPS platform parameters.

Every value here is taken verbatim from CUPPS TS 01.04.0004 section 26.11
(Platform Parameters), Table 26.4.  Applications and platforms must allow a
run-time tolerance of +/- 1% on all time-related parameters (26.11, note).
"""

from __future__ import annotations

# --- Application lifecycle (26.11.1 - 26.11.4) ---------------------------
APP_ATH_TIME = 5.0        # 26.11.1  platform authenticates an application
APP_SPG_TIME = 45.0       # 26.11.2  app shutdown once directed
APP_STG_TIME = 30.0       # 26.11.3  platform sets up app runtime env
APP_STP_TIME = 30.0       # 26.11.4  platform clean-up after app terminates

# --- Request processing ceilings (26.11.5 - 26.11.18) --------------------
AUTH_REQ_MAX_TIME = 20.0  # 26.11.5   <authenticateRequest>
DEV_ACQ_MAX_TIME = 20.0   # 26.11.6   <deviceAcquireRequest>
DEV_LKD_TIME = 60.0       # 26.11.7   max time a device stays in dLkd
DEV_NORM_TIME = 5.0       # 26.11.8   reader input normalisation window
DEV_POLL_MAX_FREQ = 5.0   # 26.11.9   min seconds between status polls
DEV_PR_ACC_TIME = 5.0     # 26.11.10  simpleHostPrint accumulation (0-9s)
DEV_REL_MAX_TIME = 5.0    # 26.11.11  <deviceReleaseRequest>
DEV_RES_RTN_TIME = 600.0  # 26.11.12  shared-access result retention
DEV_SPG_TIME = 30.0       # 26.11.13  dSpg -> dZom
DEV_STG_TIME = 60.0       # 26.11.14  dStg -> dZom
INT_LVL_MAX_TIME = 10.0   # 26.11.15  <interfaceLevelsAvailableRequest>
MAX_DEV_LOCK_TIME = 5.0   # 26.11.16  <deviceLockRequest>
MAX_DEV_STS_TIME = 5.0    # 26.11.17  <deviceStatusRequest>
MAX_DEV_UNL_TIME = 3.0    # 26.11.18  <deviceUnlockRequest>

# --- messageID ranges (26.11.19 - 26.11.23) ------------------------------
MAX_MESSAGE_ID = 0xFFFFFFFF       # 26.11.19  top of platform range
MAX_SPG_DEFER_TIMES = 4           # 26.11.20  shutdown deferrals allowed
MIN_MESSAGE_ID = 0x00000001       # 26.11.21  bottom of application range
MIN_PLATFORM_MSG_ID = 0xFFFF0000  # 26.11.22  bottom of platform range
MSG_ID_LIST_LEN = 255             # 26.11.23  recent-messageID list length

# --- Storage minimums (26.11.24 - 26.11.29, 26.11.38) --------------------
PGS_MIN = 1 * 1024**3        # 26.11.24  persistent global storage / airline
PLS_MIN = 1 * 1024**3        # 26.11.25  persistent local storage / airline
PNS_MIN = 1 * 1024**3        # 26.11.26  persistent network storage / airline
PLT_APP_STR_MIN = 5 * 1024**3  # 26.11.28  application storage / airline
PLT_LOG_MIN_STR_DAYS = 5     # 26.11.29  log retention, in days
TS_MIN = 1 * 1024**3         # 26.11.38  transient storage

# --- Platform behaviour (26.11.27, 26.11.30 - 26.11.37) ------------------
PLT_ALT_MAX_DLV_TIME = 600.0   # 26.11.27  alert delivery to all workstations
PLT_MAX_MSG_SIZE = 0x00FFFFFF  # 26.11.30  max interface message body, ~16MB
PLT_REP_GRAN = 60.0            # 26.11.32  reporting granularity
PLT_SOCK_MAX_IDLE_TIME = 600.0 # 26.11.33  idle socket protection
PLT_STREAM_ACCUM_TIME = 60.0   # 26.11.34  max gap between bytes on a socket
PLT_STREAM_OUT_MSGS = 5        # 26.11.35  outstanding messages per socket
PLT_STREAM_OUT_MSGS_ZL = 20    # 26.11.36  outstanding messages on a ZL device
PR_MAX_RESPONSE_TIME = 120.0   # 26.11.37  spooler-driven PR printRequest

# --- User / workstation (26.11.39 - 26.11.45) ----------------------------
USR_ATH_TIME = 10.0    # 26.11.39  authenticate an end-user
USR_ATH_TRIES = 3      # 26.11.40  successive failed logins allowed
USR_SPG_TIME = 30.0    # 26.11.41  uSpg -> uZom
USR_SS_TIME = 600.0    # 26.11.42  screen saver before uSs -> uSpg
USR_TO_TIME = 600.0    # 26.11.43  idle before uStd -> uSs
WS_ALT_TIME = 600.0    # 26.11.44  alert display before discard
WS_SPG_TIME = 30.0     # 26.11.45  wSpg -> wZom

#: Run-time tolerance mandated by section 26.11 for time-based parameters.
TIME_TOLERANCE = 0.01


def with_tolerance(seconds: float, tolerance: float = TIME_TOLERANCE) -> float:
    """Return ``seconds`` widened by the tolerance the spec requires.

    Section 26.11 requires both platforms and applications to accept a +/- 1%
    run-time variance, so a timer armed against a spec deadline uses the upper
    bound rather than the nominal value.
    """
    return seconds * (1.0 + tolerance)
