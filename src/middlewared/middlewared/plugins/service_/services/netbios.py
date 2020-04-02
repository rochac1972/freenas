from .base import SimpleService


class NetBIOSService(SimpleService):
    name = "netbios"

    freebsd_rc = "nmbd"

    systemd_unit = "nmbd"
