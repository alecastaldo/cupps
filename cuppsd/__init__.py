"""CUPPS device handler and agent application.

``cuppsd`` is the device handler of Figure 9.4 architecture (b): it owns every
CUPPS session and presents them to a browser-based agent UI over the loopback
adapter.
"""

from .service import AppState, CuppsService, ServiceConfig

__all__ = ["AppState", "CuppsService", "ServiceConfig"]

#: Application version, in the component form of section 5.1.
__version__ = "01.00.0001"

#: Application name reported to the platform in <authenticateRequest>.
APPLICATION_NAME = "CUPPSAGENT"
