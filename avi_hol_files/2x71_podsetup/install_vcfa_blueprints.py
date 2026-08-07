"""
Idempotently create the VCF Automation (VCFA) blueprints this pod's
Automation lab module expects to already exist in the org/project below,
via the `/blueprint/api/blueprints` REST API -- pre-populating the lab
content instead of requiring students (or a previous manual step) to paste
each one in through the UI.

VCFA_ORG ('acme-east-a') is an All-Apps org, but the blueprint-management
API itself is the same `/blueprint/api/blueprints` service used by VM-Apps
orgs -- confirmed against this pod's own in-product API Help Center. The
All-Apps/VM-Apps distinction only changes which resource types a
blueprint's `content` can legally express (CCI.Supervisor.* here, vs.
Cloud.Machine/Idem.* for VM-Apps) -- not this create/list API surface.

Auth is the VCD-style Cloud API session, confirmed live against this pod
(2026-07-30) -- VCFA shares this session model with VMware Cloud Director:
    POST /cloudapi/1.0.0/sessions
    Authorization: Basic base64("admin@<org>:<password>")
returns an `x-vmware-vcloud-access-token` response header that is itself
a ready-to-use bearer token for every other VCFA API used here (no
separate OAuth exchange, and no manually-pre-generated API token needed --
this authenticates with the same standard lab password every other
component in this repo uses, via lsf.get_password()). Plain CSP-style
username/password login (/csp/gateway/am/api/login) 404s on this pod, and
/iaas/api/login demands a refreshToken -- this cloudapi/sessions path is
the one that actually works for an All-Apps org.

The project-service and blueprint-list responses are Spring-style
paginated envelopes (`{"content": [...], "totalElements": N, ...}`),
confirmed live -- _as_list() unwraps that (and tolerates a bare list too,
in case some other VCFA endpoint doesn't paginate).
"""

import os

VCFA_HOST = 'auto-a.site-a.vcf.lab'
VCFA_ORG = 'acme-east-a'
VCFA_USERNAME = f'admin@{VCFA_ORG}'
VCFA_PROJECT_NAME = 'default-project'

BLUEPRINTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vcfa_blueprints')
BLUEPRINTS = {
    'HOL L7 Fullstack (self-signed cert)': 'l7-fullstack-selfsigned.yaml',
    'HOL L7 Fullstack (static cert)': 'l7-fullstack-staticcert.yaml',
    'HOL L4 Passthrough': 'l4-passthrough.yaml',
}


def _as_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get('content', [])
    return []


def _get_access_token(lsf):
    import base64
    import requests

    password = lsf.get_password()
    creds = base64.b64encode(f'{VCFA_USERNAME}:{password}'.encode()).decode()
    resp = requests.post(
        f'https://{VCFA_HOST}/cloudapi/1.0.0/sessions',
        headers={'Authorization': f'Basic {creds}', 'Accept': 'application/json;version=9.0.0'},
        verify=False, timeout=15,
    )
    if resp.status_code != 200:
        lsf.write_output(f'  WARNING: VCFA login failed (HTTP {resp.status_code}): {resp.text[:300]}')
        return None

    access_token = resp.headers.get('x-vmware-vcloud-access-token')
    if not access_token:
        lsf.write_output('  WARNING: VCFA login succeeded but response had no x-vmware-vcloud-access-token header')
    return access_token


def _get_project_id(lsf, headers):
    import requests

    resp = requests.get(
        f'https://{VCFA_HOST}/project-service/api/projects',
        params={'$filter': f"name eq '{VCFA_PROJECT_NAME}'"},
        headers=headers, verify=False, timeout=15,
    )
    if resp.status_code != 200:
        lsf.write_output(f'  WARNING: could not look up project {VCFA_PROJECT_NAME!r} (HTTP {resp.status_code}): {resp.text[:300]}')
        return None

    projects = _as_list(resp.json())
    if not projects:
        lsf.write_output(f'  WARNING: project {VCFA_PROJECT_NAME!r} not found in org {VCFA_ORG!r}')
        return None
    return projects[0]['id']


def _blueprint_exists(lsf, headers, name):
    import requests

    # NOTE: unlike project-service, this endpoint does not honor $filter --
    # confirmed live: a $filter for a name that doesn't exist still returned
    # every blueprint in the project. Fetch everything and match client-side.
    resp = requests.get(
        f'https://{VCFA_HOST}/blueprint/api/blueprints',
        headers=headers, verify=False, timeout=15,
    )
    if resp.status_code != 200:
        lsf.write_output(f'  WARNING: could not check for existing blueprint {name!r} (HTTP {resp.status_code}): {resp.text[:300]}')
        return True  # don't attempt a create we couldn't first check for

    return any(bp.get('name') == name for bp in _as_list(resp.json()))


def install_vcfa_blueprints(lsf):
    """
    Idempotently create each blueprint in BLUEPRINTS (skipping any that
    already exist by name) in VCFA_ORG's VCFA_PROJECT_NAME project, from
    the YAML files in vcfa_blueprints/ alongside this script.

    Non-fatal: any failure anywhere in this chain -- VCFA unreachable,
    login failing, project/org not found, a single blueprint's create call
    failing -- is logged as a WARNING and skipped, never lsf.labfail()'d.
    """
    import requests
    requests.packages.urllib3.disable_warnings()

    lsf.write_output('Checking VCF Automation blueprint catalog...')

    access_token = _get_access_token(lsf)
    if not access_token:
        return
    headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}

    project_id = _get_project_id(lsf, headers)
    if not project_id:
        return

    for name, filename in BLUEPRINTS.items():
        try:
            if _blueprint_exists(lsf, headers, name):
                lsf.write_output(f'  {name}: already exists -- no-op')
                continue

            content = open(os.path.join(BLUEPRINTS_DIR, filename)).read()
            resp = requests.post(
                f'https://{VCFA_HOST}/blueprint/api/blueprints',
                headers=headers, verify=False, timeout=30,
                json={
                    'name': name,
                    'description': f'HOL-2740 Automation lab blueprint ({filename})',
                    'content': content,
                    'projectId': project_id,
                    'requestScopeOrg': False,
                },
            )
            if resp.status_code in (200, 201):
                lsf.write_output(f'  {name}: created')
            else:
                lsf.write_output(f'  WARNING: could not create blueprint {name!r} (HTTP {resp.status_code}): {resp.text[:300]}')
        except Exception as e:
            lsf.write_output(f'  WARNING: could not create blueprint {name!r}: {e}')
    try:
        # CCI's supervisornamespaces PATCH is JSON Merge Patch (RFC 7396) --
        # confirmed against vcf/automation's supervisor-k8.service.ts, which
        # always sends this content type for this call (never the plain
        # application/json used by the blueprint calls above).
        patch_headers = {**headers, 'Content-Type': 'application/merge-patch+json'}
        resp = requests.patch(
                f'https://{VCFA_HOST}/cci/kubernetes/apis/infrastructure.cci.vmware.com/v1alpha3/namespaces/default-project/supervisornamespaces/acme-east-prod-wrp4h',
                headers=patch_headers, verify=False, timeout=30,
                json={
                    "spec": {
                        "classConfigOverrides": {
                            "storageClasses": [
                        {
                            "name": "cluster-wld01-01a-optimal-datastore-default-policy-autoraid",
                            "limit": "2000000Mi"
                        }
                    ]}}},)
        if resp.status_code in (200, 201, 204):
            lsf.write_output(f'  {name}: created')
        else:
            lsf.write_output(f'  WARNING: could not patch namespace: (HTTP {resp.status_code}): {resp.text[:300]}')
    except Exception as e:
            lsf.write_output(f'  WARNING: could not patch namespace: {e}')

#patch
#cci/kubernetes/apis/infrastructure.cci.vmware.com/v1alpha3/namespaces/default-project/supervisornamespaces/acme-east-prod-wrp4h
#{"spec":{"classConfigOverrides":{"storageClasses":[{"name":"cluster-wld01-01a-optimal-datastore-default-policy-autoraid","limit":"2000000Mi"}]}}}