#!/usr/bin/env python3
"""
VCF Automation provider portal API helper for creating public subnets.
"""
import base64
import requests
import json
import sys


def get_provider_token(vcfa_host, admin_pass):
    """
    Get JWT token for provider management context.
    Returns token on success, None on failure.
    """
    VCFA_USERNAME = 'admin@system'
    creds = base64.b64encode(f'{VCFA_USERNAME}:{admin_pass}'.encode()).decode()

    resp = requests.post(
        f'https://{vcfa_host}/cloudapi/1.0.0/sessions/provider',
        headers={
            'Authorization': f'Basic {creds}',
            'Accept': 'application/json;version=9.0.0',
            'Content-Type': 'application/json;version=9.0.0'
        },
        verify=False,
        timeout=15,
    )

    if resp.status_code != 200:
        print(f"ERROR: Provider login failed (HTTP {resp.status_code})")
        return None

    token = resp.headers.get('x-vmware-vcloud-access-token')
    if not token:
        print("ERROR: Login succeeded but no x-vmware-vcloud-access-token header")
        return None

    return token


def create_public_subnet(vcfa_host, admin_pass, subnet_name='default-us-east-1-subnet-public1',
                         region='us-east-1', vpc='default-us-east-1', ipspace='ipspace-wld-a'):
    """
    Create a public subnet in VCF Automation provider portal.

    Args:
        vcfa_host: FQDN of VCF Automation appliance (e.g., auto-a.site-a.vcf.lab)
        admin_pass: Administrator password
        subnet_name: Name for the subnet
        region: Region name (e.g., us-east-1)
        vpc: VPC name (e.g., default-us-east-1)
        ipspace: IP space name (e.g., ipspace-wld-a)

    Returns:
        True on success, False on failure
    """
    token = get_provider_token(vcfa_host, admin_pass)
    if not token:
        return False

    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json;version=9.0.0',
        'Content-Type': 'application/json'
    }

    # First, get the IP space ID
    url = f'https://{vcfa_host}/cloudapi/v1/ipSpaces?page=1&pageSize=100'
    resp = requests.get(url, headers=headers, verify=False, timeout=15)

    if resp.status_code != 200:
        print(f"ERROR: Could not list IP spaces (HTTP {resp.status_code})")
        return False

    ipspaces = resp.json().get('values', [])
    ipspace_obj = next((ip for ip in ipspaces if ip.get('name') == ipspace), None)

    if not ipspace_obj:
        print(f"ERROR: IP space '{ipspace}' not found")
        return False

    ipspace_id = ipspace_obj.get('id')
    region_ref = ipspace_obj.get('regionRef', {})

    # Get the VPC ID
    vpc_url = f'https://{vcfa_host}/cloudapi/v1/vpcs?page=1&pageSize=100'
    resp = requests.get(vpc_url, headers=headers, verify=False, timeout=15)

    if resp.status_code != 200:
        print(f"ERROR: Could not list VPCs (HTTP {resp.status_code})")
        return False

    vpcs = resp.json().get('values', [])
    vpc_obj = next((v for v in vpcs if v.get('name') == vpc), None)

    if not vpc_obj:
        print(f"ERROR: VPC '{vpc}' not found")
        return False

    vpc_id = vpc_obj.get('id')

    # Create the subnet
    subnet_payload = {
        'name': subnet_name,
        'description': 'Public subnet for external traffic',
        'vpcRef': vpc_id,
        'ipSpaceRef': ipspace_id,
        'accessMode': 'PUBLIC',
        'enableGatewayConnectivity': True,
        'enableStaticIpAllocation': False,
        'ipv4SubnetSize': 26,  # 64 IPs
        'dhcpMode': 'DHCP_SERVER'
    }

    subnet_url = f'https://{vcfa_host}/cloudapi/v1/subnets'
    resp = requests.post(subnet_url, json=subnet_payload, headers=headers, verify=False, timeout=15)

    if resp.status_code not in (200, 201, 202):
        print(f"ERROR: Failed to create subnet (HTTP {resp.status_code})")
        print(f"Response: {resp.text[:500]}")
        return False

    print(f"SUCCESS: Subnet '{subnet_name}' created")
    return True


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: create_public_subnet.py <vcfa_host> <admin_password> [subnet_name] [region] [vpc] [ipspace]")
        sys.exit(1)

    vcfa_host = sys.argv[1]
    admin_pass = sys.argv[2]
    subnet_name = sys.argv[3] if len(sys.argv) > 3 else 'default-us-east-1-subnet-public1'
    region = sys.argv[4] if len(sys.argv) > 4 else 'us-east-1'
    vpc = sys.argv[5] if len(sys.argv) > 5 else 'default-us-east-1'
    ipspace = sys.argv[6] if len(sys.argv) > 6 else 'ipspace-wld-a'

    success = create_public_subnet(vcfa_host, admin_pass, subnet_name, region, vpc, ipspace)
    sys.exit(0 if success else 1)
