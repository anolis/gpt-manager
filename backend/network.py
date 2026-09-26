"""User-triggered discovery of SSH banners on directly attached IPv4 networks."""
import concurrent.futures
import ipaddress
import json
import platform
import re
import socket
import subprocess


def local_networks():
    addresses = []
    try:
        if platform.system() == 'Linux':
            result = subprocess.run(['ip', '-j', 'address', 'show', 'up'], capture_output=True, text=True, timeout=5, check=True)
            for interface in json.loads(result.stdout):
                for address in interface.get('addr_info', []):
                    if address.get('family') == 'inet':
                        addresses.append((interface['ifname'], address['local'], address['prefixlen']))
        elif platform.system() == 'Windows':
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', 'Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias,IPAddress,PrefixLength | ConvertTo-Json -Compress'], capture_output=True, text=True, timeout=10, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            items = json.loads(result.stdout or '[]')
            for item in ([items] if isinstance(items, dict) else items):
                addresses.append((item['InterfaceAlias'], item['IPAddress'], item['PrefixLength']))
        elif platform.system() == 'Darwin':
            result = subprocess.run(['ifconfig'], capture_output=True, text=True, timeout=5, check=True)
            interface = ''
            for line in result.stdout.splitlines():
                if line and not line[0].isspace():
                    interface = line.split(':')[0]
                match = re.search(r'inet (\d+\.\d+\.\d+\.\d+).*netmask (0x[0-9a-fA-F]+)', line)
                if match:
                    prefix = bin(int(match[2], 16)).count('1')
                    addresses.append((interface, match[1], prefix))
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    found = {}
    for interface, address, prefix in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_loopback or ip.is_link_local or not ip.is_private:
            continue
        # Large LANs are limited to the /24 containing this machine.
        network = ipaddress.ip_network(f'{address}/{max(prefix, 24)}', strict=False)
        found[str(network)] = {'cidr': str(network), 'interface': interface, 'localAddress': address}
    return list(found.values())


def probe_ssh(address):
    try:
        with socket.create_connection((address, 22), timeout=0.5) as connection:
            connection.settimeout(0.5)
            banner = connection.recv(256)
            if banner.startswith(b'SSH-'):
                return {'host': address, 'port': 22, 'banner': banner.splitlines()[0].decode('ascii', errors='replace')[:160]}
    except OSError:
        pass
    return None


def scan_network(cidr):
    allowed = {item['cidr'] for item in local_networks()}
    if cidr not in allowed:
        raise ValueError('Choose a currently attached local subnet from the list.')
    network = ipaddress.ip_network(cidr)
    if network.num_addresses > 256:
        raise ValueError('Local scans are limited to 256 addresses.')
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        hosts = [result for result in pool.map(probe_ssh, map(str, network.hosts())) if result]
    return {'network': cidr, 'hosts': hosts}
