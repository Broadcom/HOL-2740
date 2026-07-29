#!/usr/bin/env python3
"""
Standalone diagnostic for adjustomatic.py's telemetry credential chain.
Run directly on manager:

    PYTHONPATH=/home/holuser/hol python3 test_telemetry_upload.py

Reproduces _get_telemetry_access_token()'s steps one at a time, so
whichever one fails prints its own real error -- instead of the single
generic WARNING adjustomatic.py logs during a real run. Never prints the
decrypted secret values themselves, only key presence/names and Google's
own response body from the token endpoint.
"""
import json
import os
import subprocess
import sys

VAULT_FILE = '/vpodrepo/2027-labs/2740/avi_hol_files/2x71_podsetup/secrets.yml'
VAULT_PASSWORD_FILE = '/home/holuser/vaultsecret.txt'


def main():
    print(f'--- 1. vault file exists? ({VAULT_FILE}) ---')
    if not os.path.isfile(VAULT_FILE):
        print('MISSING -- this is the failure.')
        sys.exit(1)
    print('present.')

    print('--- 2. decrypt ---')
    result = subprocess.run(
        ['/usr/bin/ansible-vault', 'view', '--vault-password-file', VAULT_PASSWORD_FILE, VAULT_FILE],
        capture_output=True, text=True,
    )
    print(f'exit code: {result.returncode}')
    if result.returncode != 0:
        print(f'stderr: {result.stderr}')
        print('DECRYPT FAILED -- this is the failure (vault password mismatch?).')
        sys.exit(1)
    print('decrypt OK.')

    print('--- 3. credential parses? ---')
    import yaml
    vault_contents = yaml.safe_load(result.stdout)
    raw = vault_contents.get('telemetry_user_credentials_json')
    print(f'key present: {raw is not None}')
    if not raw:
        print('MISSING KEY telemetry_user_credentials_json -- this is the failure.')
        sys.exit(1)
    try:
        creds = json.loads(raw)
    except Exception as e:
        print(f'JSON PARSE FAILED: {e} -- this is the failure.')
        sys.exit(1)
    print(f'parsed keys: {list(creds.keys())}')
    for required in ('client_id', 'client_secret', 'refresh_token'):
        print(f'  has {required}: {required in creds}')
    if not all(k in creds for k in ('client_id', 'client_secret', 'refresh_token')):
        print('MISSING REQUIRED FIELD -- this is the failure.')
        sys.exit(1)

    print('--- 4. token exchange ---')
    import requests
    resp = requests.post('https://oauth2.googleapis.com/token', data={
        'client_id': creds['client_id'],
        'client_secret': creds['client_secret'],
        'refresh_token': creds['refresh_token'],
        'grant_type': 'refresh_token',
    })
    print(f'status: {resp.status_code}')
    print(f'body: {resp.text}')
    if resp.status_code == 200:
        print('SUCCESS.')
    else:
        print("TOKEN EXCHANGE FAILED -- this is the failure (see body above for Google's reason).")


if __name__ == '__main__':
    main()
