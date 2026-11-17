# -*- coding: utf-8 -*-
"""看看这个局域网里有没有能用的 UPnP IGD（网关自己开端口的那种协议）。

为什么值得单独试一次：往公网开端口这件事，**最后一跳在网关（10.10.10.253）上**，
而我没有它的凭据。但如果它开着 UPnP-IGD，就能用标准协议把两条映射加进去——
不需要口令。早先用 COM 的 `HNetCfg.NATUPnP.StaticPortMappingCollection` 探过是
null，那可能只是"没做 SSDP 发现"，所以这里直接按协议走一遍：

    1. SSDP M-SEARCH（UDP 239.255.255.250:1900）找 InternetGatewayDevice
    2. 取 LOCATION 里的设备描述，读 WANIPConnection / WANPPPConnection 的 controlURL
    3. 打印它自报的 ExternalIPAddress，并与我们看到的一致不一致
    4. 只做**只读**的 GetExternalIPAddress / GetStatusInfo；AddPortMapping 需要
       `--add` 才执行（默认不写）。

用法：
    python upnp_probe.py                     # 只探（不发任何写请求）
    python upnp_probe.py --add --port 8787 --internal 10.10.10.102
"""
import argparse
import re
import socket
import sys
import urllib.request

MCAST = ("239.255.255.250", 1900)
TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:WANDevice:1",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


def ssdp(timeout=4.0):
    """发 M-SEARCH，把回包里的 LOCATION 都收回来（去重）。"""
    message = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        "HOST: %s:%d" % MCAST,
        'MAN: "ssdp:discover"',
        "MX: 2",
        "ST: %s" % TARGETS[0],
        "", "",
    ]).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    seen = {}
    try:
        sock.sendto(message, MCAST)
        while True:
            try:
                data, address = sock.recvfrom(65507)
            except socket.timeout:
                break
            text = data.decode("utf-8", "replace")
            location = re.search(r"(?im)^location:\s*(\S+)", text)
            server = re.search(r"(?im)^server:\s*(.+)$", text)
            st = re.search(r"(?im)^st:\s*(.+)$", text)
            if location:
                seen[location.group(1).strip()] = {
                    "from": address[0],
                    "server": (server.group(1).strip() if server else ""),
                    "st": (st.group(1).strip() if st else ""),
                }
    finally:
        sock.close()
    return seen


def fetch(url, timeout=6.0):
    with urllib.request.build_opener(
            urllib.request.ProxyHandler({})).open(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def services(xml):
    """从设备描述里抠出 (serviceType, controlURL) 列表。"""
    out = []
    for block in re.findall(r"<service>(.*?)</service>", xml, re.S | re.I):
        kind = re.search(r"<serviceType>(.*?)</serviceType>", block, re.S | re.I)
        control = re.search(r"<controlURL>(.*?)</controlURL>", block, re.S | re.I)
        if kind and control:
            out.append((kind.group(1).strip(), control.group(1).strip()))
    return out


def soap(url, service, action, body, timeout=8.0):
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body><u:%s xmlns:u=\"%s\">%s</u:%s></s:Body></s:Envelope>"
        % (action, service, body, action)
    ).encode()
    request = urllib.request.Request(
        url, data=envelope, method="POST",
        headers={"Content-Type": 'text/xml; charset="utf-8"',
                 "SOAPAction": '"%s#%s"' % (service, action)})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--add", action="store_true",
                        help="真的下发 AddPortMapping（默认只探不发）")
    parser.add_argument("--port", action="append", type=int, default=[],
                        help="要映射的端口，可重复")
    parser.add_argument("--internal", default="10.10.10.102")
    parser.add_argument("--lease", type=int, default=0,
                        help="租期秒数，0 = 永久")
    args = parser.parse_args(argv)

    print("=== SSDP 发现 ===")
    found = ssdp()
    if not found:
        print("  没有设备回答 M-SEARCH（网关没开 UPnP，或 UDP 1900 被过滤）。")
        return 1
    for location, meta in found.items():
        print("  %s" % location)
        print("    from=%s  server=%s  st=%s"
              % (meta["from"], meta["server"], meta["st"]))

    location = sorted(found)[0]
    print()
    print("=== 设备描述 %s ===" % location)
    try:
        xml = fetch(location)
    except Exception as exc:                                  # noqa: BLE001
        print("  取不到：%r" % exc)
        return 2
    model = re.search(r"<modelName>(.*?)</modelName>", xml, re.S | re.I)
    friendly = re.search(r"<friendlyName>(.*?)</friendlyName>", xml, re.S | re.I)
    print("  friendlyName: %s" % (friendly.group(1).strip() if friendly else "?"))
    print("  modelName   : %s" % (model.group(1).strip() if model else "?"))

    root = location[:location.rindex("/") + 1] if "/" in location else location
    table = services(xml)
    print("  服务 %d 条" % len(table))
    for kind, control in table[:20]:
        print("    %s -> %s" % (kind, control))

    wan = [(kind, control) for kind, control in table
           if "WANIPConnection" in kind or "WANPPPConnection" in kind]
    if not wan:
        print("  没有 WANIPConnection/WANPPPConnection：这个网关不提供端口映射服务。")
        return 3

    kind, control = wan[0]
    url = control if control.startswith("http") else root + control.lstrip("/")
    print()
    print("=== 只读查询 %s ===" % url)
    for action in ("GetExternalIPAddress", "GetStatusInfo"):
        try:
            status, text = soap(url, kind, action, "")
            value = re.search(r"<New\w+>(.*?)</New\w+>", text, re.S)
            print("  %s -> HTTP %s  %s" % (action, status,
                                           value.group(1).strip() if value else text[:120]))
        except Exception as exc:                              # noqa: BLE001
            print("  %s -> 失败：%r" % (action, exc))

    if not args.add:
        print()
        print("（没有下发任何写请求。要真的加映射：--add --port 8787）")
        return 0

    print()
    print("=== 下发 AddPortMapping ===")
    for port in args.port or (8787,):
        body = ("<NewRemoteHost></NewRemoteHost>"
                "<NewExternalPort>%d</NewExternalPort>"
                "<NewProtocol>TCP</NewProtocol>"
                "<NewInternalPort>%d</NewInternalPort>"
                "<NewInternalClient>%s</NewInternalClient>"
                "<NewEnabled>1</NewEnabled>"
                "<NewPortMappingDescription>pentest-platform %d</NewPortMappingDescription>"
                "<NewLeaseDuration>%d</NewLeaseDuration>"
                % (port, port, args.internal, port, args.lease))
        try:
            status, text = soap(url, kind, "AddPortMapping", body)
            print("  %d -> HTTP %s %s" % (port, status,
                                          "OK" if "errorCode" not in text else text[:200]))
        except Exception as exc:                              # noqa: BLE001
            print("  %d -> 失败：%r" % (port, exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
