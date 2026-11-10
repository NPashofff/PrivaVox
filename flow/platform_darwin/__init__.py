"""macOS implementations of Flow's platform layer (phase W1).

Modules here import mac-only dependencies (mlx, Quartz, fcntl, …) at module
top and must only be imported behind the sys.platform dispatch — see
flow/platform_impl.py and docs/windows-port-plan.md.
"""
