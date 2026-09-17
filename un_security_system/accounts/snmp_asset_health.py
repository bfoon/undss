"""
SNMP polling for printers and switches.

Preferred SNMPv3 environment variables:
  UNPASS_SNMP_V3_USER
  UNPASS_SNMP_V3_AUTH_KEY
  UNPASS_SNMP_V3_PRIV_KEY

Optional legacy fallback:
  UNPASS_SNMP_COMMUNITY
"""
import os


def _auth():
    try:
        from pysnmp.hlapi import (
            UsmUserData,
            CommunityData,
            usmHMACSHAAuthProtocol,
            usmAesCfb128Protocol,
        )
    except Exception as exc:
        raise RuntimeError("pysnmp is not installed.") from exc

    user = os.getenv("UNPASS_SNMP_V3_USER", "").strip()
    auth_key = os.getenv("UNPASS_SNMP_V3_AUTH_KEY", "")
    priv_key = os.getenv("UNPASS_SNMP_V3_PRIV_KEY", "")

    if user and auth_key and priv_key:
        return UsmUserData(
            user,
            authKey=auth_key,
            privKey=priv_key,
            authProtocol=usmHMACSHAAuthProtocol,
            privProtocol=usmAesCfb128Protocol,
        )

    community = os.getenv("UNPASS_SNMP_COMMUNITY", "").strip()
    if community:
        return CommunityData(community, mpModel=1)

    raise RuntimeError(
        "No SNMP credentials configured. Configure SNMPv3 environment variables."
    )


def snmp_get(host, oid, timeout=2, retries=1):
    from pysnmp.hlapi import (
        SnmpEngine,
        UdpTransportTarget,
        ContextData,
        ObjectType,
        ObjectIdentity,
        getCmd,
    )

    it = getCmd(
        SnmpEngine(),
        _auth(),
        UdpTransportTarget((host, 161), timeout=timeout, retries=retries),
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
    )
    err_ind, err_status, _, var_binds = next(it)

    if err_ind:
        raise RuntimeError(str(err_ind))
    if err_status:
        raise RuntimeError(str(err_status.prettyPrint()))

    return str(var_binds[0][1]) if var_binds else ""


def snmp_walk(host, oid, timeout=2, retries=1):
    from pysnmp.hlapi import (
        SnmpEngine,
        UdpTransportTarget,
        ContextData,
        ObjectType,
        ObjectIdentity,
        nextCmd,
    )

    rows = []
    for err_ind, err_status, _, var_binds in nextCmd(
        SnmpEngine(),
        _auth(),
        UdpTransportTarget((host, 161), timeout=timeout, retries=retries),
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
        lexicographicMode=False,
    ):
        if err_ind:
            raise RuntimeError(str(err_ind))
        if err_status:
            raise RuntimeError(str(err_status.prettyPrint()))
        for name, value in var_binds:
            rows.append((str(name), str(value)))

    return rows


def _ints(rows):
    values = []
    for _, value in rows:
        try:
            values.append(int(value))
        except Exception:
            pass
    return values


def poll_generic(host):
    payload = {"ip_address": host}

    try:
        payload["hostname"] = snmp_get(host, "1.3.6.1.2.1.1.5.0")
    except Exception:
        pass

    try:
        ticks = int(snmp_get(host, "1.3.6.1.2.1.1.3.0"))
        payload["uptime_seconds"] = int(ticks / 100)
    except Exception:
        pass

    return payload


def poll_printer(host):
    payload = poll_generic(host)

    # Printer-MIB prtMarkerLifeCount.
    try:
        life = [
            value
            for value in _ints(
                snmp_walk(host, "1.3.6.1.2.1.43.10.2.1.4")
            )
            if value >= 0
        ]
        if life:
            # Several printers expose more than one overlapping marker.
            # max() is safer than summing those counters.
            payload["page_count_total"] = max(life)
    except Exception:
        pass

    # Printer-MIB supply max/current levels.
    try:
        maxima = _ints(
            snmp_walk(host, "1.3.6.1.2.1.43.11.1.1.8")
        )
        levels = _ints(
            snmp_walk(host, "1.3.6.1.2.1.43.11.1.1.9")
        )
        percentages = []
        for level, maximum in zip(levels, maxima):
            if level >= 0 and maximum > 0:
                percentages.append(
                    round((level / maximum) * 100, 1)
                )
        if percentages:
            # Generic MIBs do not reliably identify which supply is black
            # across every vendor; V1 uses the first reported supply and keeps
            # all values in the raw payload for vendor-specific refinement.
            payload["toner_black_percent"] = percentages[0]
            payload["supplies_percent"] = percentages
    except Exception:
        pass

    return payload


def poll_switch(host):
    payload = poll_generic(host)

    try:
        statuses = _ints(
            snmp_walk(host, "1.3.6.1.2.1.2.2.1.8")
        )
        payload["ports_up"] = sum(1 for x in statuses if x == 1)
        payload["ports_down"] = sum(1 for x in statuses if x != 1)
    except Exception:
        pass

    errors = 0

    try:
        errors += sum(
            _ints(
                snmp_walk(host, "1.3.6.1.2.1.2.2.1.14")
            )
        )
    except Exception:
        pass

    try:
        errors += sum(
            _ints(
                snmp_walk(host, "1.3.6.1.2.1.2.2.1.20")
            )
        )
    except Exception:
        pass

    payload["port_errors"] = errors
    return payload
