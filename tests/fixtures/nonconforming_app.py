"""A deliberately non-conforming CUPPS application, to prove the harness bites.

Violations, each targeting a specific check:
  APP-0020  authenticates twice on one connection
  APP-0060  acquires a device but never sets the interface mode
  APP-0064  locks a Special Mode (ZL) device
  APP-0065  polls device status far faster than DevPollMaxFreq
  APP-0030  closes the platform connection without <byeRequest>
  APP-0032  closes a device connection without <deviceReleaseRequest>
"""
import os, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))
from cupps import xmlmsg, params
from cupps.transport import Connection

HOST, PORT = os.environ["CUPPSPN"], int(os.environ["CUPPSPP"])

def rpc(c, name, **attrs):
    m = xmlmsg.build(name, c.allocate_message_id(), attrs=attrs or None,
                     interface_level="01.04")
    return c.send_request(m, timeout=20)

def handshake(c):
    rpc(c, "interfaceLevelsAvailableRequest", hsXsdVersion="01.00.0120")
    rpc(c, "interfaceLevelRequest", level="01.04")
    c.interface_level = "01.04"

plat = Connection(HOST, PORT, name="bad-plat"); plat.connect()
handshake(plat)
r = rpc(plat, "authenticateRequest", airline="ZZ", eventToken="BADAPP0000000001")
token = r.body.get("deviceToken")
# VIOLATION APP-0020: authenticate a second time on the same connection
try: rpc(plat, "authenticateRequest", airline="ZZ", eventToken="BADAPP0000000001")
except Exception: pass

dev = None
for d in r.body.find("deviceList").findall("device"):
    ipp = d.find(d.get("deviceParameterType")).find("ipAndPort")
    name = d.get("deviceName")
    if name.endswith("BC1"): dev = (name, ipp.get("ip"), int(ipp.get("port")))
    if name.endswith("ZL1"): zl = (name, ipp.get("ip"), int(ipp.get("port")))

# VIOLATION APP-0060: acquire without ever sending interfaceModeRequest
bc = Connection(dev[1], dev[2], name="bad-bc"); bc.connect(); handshake(bc)
rpc(bc, "deviceAcquireRequest", deviceName=dev[0], deviceToken=token, airlineID="ZZ")
# VIOLATION APP-0065: poll much faster than DevPollMaxFreq (5.0s)
for _ in range(4):
    try: rpc(bc, "deviceStatusRequest")
    except Exception: pass
    time.sleep(0.3)

# VIOLATION APP-0064: lock a Special Mode device
zlc = Connection(zl[1], zl[2], name="bad-zl"); zlc.connect(); handshake(zlc)
rpc(zlc, "deviceAcquireRequest", deviceName=zl[0], deviceToken=token, airlineID="ZZ")
rpc(zlc, "interfaceModeRequest", mode="special")
try: rpc(zlc, "deviceLockRequest", lockMethod="byConnection")
except Exception: pass

time.sleep(2)
# VIOLATIONS APP-0030 / APP-0032: just drop everything, no bye, no release
bc.close(); zlc.close(); plat.close()
time.sleep(float(os.environ.get("BADAPP_LINGER", "30")))
