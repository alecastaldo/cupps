"""A CUPPS platform simulator for integration testing and acceptance."""

from .platform_sim import (
    SimulatedDevice,
    PlatformSimulator,
    default_devices,
)

__all__ = ["PlatformSimulator", "SimulatedDevice", "default_devices"]
