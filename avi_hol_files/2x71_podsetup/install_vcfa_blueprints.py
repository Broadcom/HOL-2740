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
    'HOL L7 Fullstack (dynamic cert-manager cert)': 'l7-fullstack-certmanager.yaml',
    'HOL L7 Fullstack (static cert)': 'l7-fullstack-staticcert.yaml',
    'HOL L4 Passthrough': 'l4-passthrough.yaml',
    'HOL Two Webservers': 'two-web-servers.yaml'
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


def _find_blueprint(lsf, headers, name):
    import requests

    # NOTE: unlike project-service, this endpoint does not honor $filter --
    # confirmed live: a $filter for a name that doesn't exist still returned
    # every blueprint in the project. Fetch everything and match client-side.
    resp = requests.get(
        f'https://{VCFA_HOST}/blueprint/api/blueprints',
        headers=headers, verify=False, timeout=15,
    )
    if resp.status_code != 200:
        # Raise rather than return a sentinel -- the caller's per-blueprint
        # try/except logs this as a WARNING and skips, same as any other
        # failure here. Don't attempt a create we couldn't first check for.
        raise RuntimeError(f'could not list blueprints to check for {name!r} (HTTP {resp.status_code}): {resp.text[:300]}')

    return next((bp for bp in _as_list(resp.json()) if bp.get('name') == name), None)


def _get_blueprint(lsf, headers, blueprint_id):
    """
    Fetch the full blueprint object, including its `content` field --
    unlike the list response _find_blueprint() uses (confirmed live
    2026-08-23: the list payload omits `content` entirely, only the
    single-object GET includes it), so this is required before any
    content-drift comparison or PUT update.
    """
    import requests

    resp = requests.get(
        f'https://{VCFA_HOST}/blueprint/api/blueprints/{blueprint_id}',
        headers=headers, verify=False, timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f'could not fetch blueprint {blueprint_id!r} (HTTP {resp.status_code}): {resp.text[:300]}')
    return resp.json()


def _update_blueprint_content(lsf, headers, existing, content, name):
    """
    PUT updated `content` onto an already-existing blueprint's draft.

    Confirmed live 2026-08-23 that PUT /blueprint/api/blueprints/{id} is
    the actual update mechanism for a blueprint's draft content (the
    versions/release endpoints below only ever snapshot+release whatever
    content the draft currently holds -- they take no content of their
    own). Without this call, editing a blueprint's YAML in git and
    re-running this script has no effect: every subsequent
    version+release just re-stamps the same stale content the blueprint
    was originally created with.
    """
    import requests

    resp = requests.put(
        f'https://{VCFA_HOST}/blueprint/api/blueprints/{existing["id"]}',
        headers=headers, verify=False, timeout=30,
        json={
            'name': existing['name'],
            'description': existing['description'],
            'content': content,
            'projectId': existing['projectId'],
            'requestScopeOrg': existing['requestScopeOrg'],
        },
    )
    if resp.status_code not in (200, 201):
        lsf.write_output(f'  WARNING: could not update content for blueprint {name!r} (HTTP {resp.status_code}): {resp.text[:300]}')
        return False
    return True


# Version/release endpoints below follow VMware Aria Automation's documented
# on-prem API (VCFA shares this blueprint-versioning surface). Confirmed
# live 2026-08-23 that this apiVersion/path pair works correctly against
# this pod's own VCFA build: a released version's own `status` (GET
# /blueprint/api/blueprints/{id}/versions) does read back RELEASED. The
# blueprint's own top-level `status` field is a DIFFERENT thing (always
# reads DRAFT -- it's the draft/editable object's own state, unrelated to
# whether any version of it has been released) -- see
# install_vcfa_blueprints()'s docstring for why that distinction matters.
VCFA_BLUEPRINT_API_VERSION = '2019-09-12'


def _release_blueprint(lsf, headers, blueprint_id, name):
    """
    Version + release a blueprint so it's actually consumable from the
    Service Broker catalog -- creating a blueprint alone leaves it in DRAFT
    status, which never appears there. Two separate calls are required;
    release cannot happen in the same call as versioning.

    Not sufficient on its own for a blueprint to actually appear in the
    catalog UI -- that also requires a content source (pointed at this
    project) and a content-sharing policy sharing it, both normally
    one-time per-project setup done in the Automation UI, not per-blueprint.
    If release succeeds here but the blueprint still doesn't show up for
    students, check for those two independently before assuming this call
    is broken.
    """
    import requests
    import time as _time

    version = _time.strftime('hol-%Y%m%d%H%M%S')
    params = {'apiVersion': VCFA_BLUEPRINT_API_VERSION}

    resp = requests.post(
        f'https://{VCFA_HOST}/blueprint/api/blueprints/{blueprint_id}/versions',
        headers=headers, params=params, verify=False, timeout=30,
        json={'version': version, 'description': f'HOL-2740 auto-release ({name})', 'release': False},
    )
    if resp.status_code not in (200, 201):
        lsf.write_output(f'  WARNING: could not version blueprint {name!r} (HTTP {resp.status_code}): {resp.text[:300]}')
        return

    release_resp = requests.post(
        f'https://{VCFA_HOST}/blueprint/api/blueprints/{blueprint_id}/versions/{version}/actions/release',
        headers=headers, params=params, verify=False, timeout=30,
    )
    if release_resp.status_code not in (200, 201, 204):
        lsf.write_output(
            f'  WARNING: could not release blueprint {name!r} version {version} '
            f'(HTTP {release_resp.status_code}): {release_resp.text[:300]}'
        )
        return

    lsf.write_output(f'  {name}: released version {version} to catalog')


def install_vcfa_blueprints(lsf):
    """
    Idempotently create/update AND release each blueprint in BLUEPRINTS in
    VCFA_ORG's VCFA_PROJECT_NAME project, from the YAML files in
    vcfa_blueprints/ alongside this script.

    Creating a blueprint alone leaves it in DRAFT status -- invisible in
    the Service Broker catalog students actually use. _release_blueprint()
    is what makes a version consumable there.

    BUG FIXED 2026-08-23: this used to treat an existing blueprint's own
    top-level `status` field as the "already released" signal and skip
    everything else once it read RELEASED. That field never actually reads
    RELEASED, though -- confirmed live it reads DRAFT permanently
    regardless of how many versions have been released (it's the state of
    the draft/editable object itself, not a rollup of its versions; the
    real per-version release status lives at GET
    /blueprint/api/blueprints/{id}/versions instead). Two consequences,
    both live on this pod before this fix: (1) every run always fell into
    the "not released -- releasing" branch and minted + released a brand
    new version, for every blueprint, on every single run (confirmed: 5
    versions after 5 runs); and (2) there was no path at all that updated
    an existing blueprint's `content` from the YAML file -- the
    version/release endpoints only ever snapshot the draft's *current*
    content, which was set once at creation and never touched again. So
    editing a blueprint's YAML in git and re-running this script had zero
    effect on what the catalog actually served, while still looking like
    it worked ("released version ... to catalog" logged every time).

    Fixed by: (a) fetching the existing blueprint's real detail (incl.
    `content`, which the list endpoint _find_blueprint() uses omits
    entirely) and diffing it against the local YAML file byte-for-byte;
    (b) PUTting the new content via _update_blueprint_content() when it
    differs; (c) only minting+releasing a new version when the content
    actually changed OR the blueprint has never had a released version at
    all (totalReleasedVersions == 0) -- an unedited, already-released
    blueprint is now a true no-op instead of growing a new version every
    run.

    Non-fatal: any failure anywhere in this chain -- VCFA unreachable,
    login failing, project/org not found, a single blueprint's create,
    update, or release call failing -- is logged as a WARNING and
    skipped, never lsf.labfail()'d.
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
            content = open(os.path.join(BLUEPRINTS_DIR, filename)).read()

            existing = _find_blueprint(lsf, headers, name)
            if existing:
                detail = _get_blueprint(lsf, headers, existing['id'])
                content_changed = detail.get('content') != content
                never_released = (detail.get('totalReleasedVersions') or 0) == 0

                if not content_changed and not never_released:
                    lsf.write_output(f'  {name}: content unchanged and already released -- no-op')
                    continue

                if content_changed:
                    lsf.write_output(f'  {name}: content differs from git -- updating')
                    if not _update_blueprint_content(lsf, headers, detail, content, name):
                        continue

                reason = 'content changed' if content_changed else 'no released version yet'
                lsf.write_output(f'  {name}: releasing new version ({reason})')
                _release_blueprint(lsf, headers, existing['id'], name)
                continue

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
            if resp.status_code not in (200, 201):
                lsf.write_output(f'  WARNING: could not create blueprint {name!r} (HTTP {resp.status_code}): {resp.text[:300]}')
                continue

            lsf.write_output(f'  {name}: created')
            blueprint_id = resp.json().get('id')
            if not blueprint_id:
                lsf.write_output(f'  WARNING: create response for {name!r} had no id -- cannot release')
                continue
            _release_blueprint(lsf, headers, blueprint_id, name)
        except Exception as e:
            lsf.write_output(f'  WARNING: could not create/release blueprint {name!r}: {e}')


def patch_supervisor_namespace_storage_quota(lsf):
    """
    Raise the acme-east-prod-wrp4h Supervisor namespace's storage-class
    quota via CCI's supervisornamespaces PATCH. Unrelated to the blueprint
    catalog, just sharing this module's VCFA auth/session helpers.

    Called from adjustomatic.py's main(), immediately after
    install_vcfa_blueprints() -- deliberately NOT from this module's own
    __main__ block, since that's used for standalone blueprint-install
    testing only (2026-08-13). Run after the blueprint step so a fresh
    blueprint deployment doesn't land in a namespace still at the
    un-bumped default quota.

    CCI's supervisornamespaces PATCH is JSON Merge Patch (RFC 7396) --
    confirmed against vcf/automation's supervisor-k8.service.ts, which
    always sends this content type for this call (never the plain
    application/json used by the blueprint calls in install_vcfa_blueprints()).

    Non-fatal: any failure -- VCFA unreachable, login failing, the PATCH
    itself failing after retries -- is logged as a WARNING, never
    lsf.labfail()'d.
    """
    import requests
    requests.packages.urllib3.disable_warnings()

    access_token = _get_access_token(lsf)
    if not access_token:
        return
    patch_headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/merge-patch+json',
    }

    namespace_patch_url = (
        f'https://{VCFA_HOST}/cci/kubernetes/apis/infrastructure.cci.vmware.com/'
        'v1alpha3/namespaces/default-project/supervisornamespaces/acme-east-prod-wrp4h'
    )
    namespace_patch_body = {
        "spec": {
            "classConfigOverrides": {
                "storageClasses": [
                    {
                        "name": "cluster-wld01-01a-optimal-datastore-default-policy-autoraid",
                        "limit": "2000000Mi",
                    }
                ]
            }
        }
    }

    # Every PATCH re-validates the whole spec server-side, including fields
    # we're not touching (e.g. segName) -- confirmed against vcf/automation's
    # SupervisorNamespaceSpecResolver.resolveSeg(), which re-resolves segName
    # on every patch and wraps any non-404 failure (e.g. Avi/NSX still
    # settling right after the AKO restart forced above) into a generic
    # HTTP 500 "Failed to resolve SEG". That's transient, not a bad SEG
    # reference, so retry a few times before giving up.
    import time
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.patch(namespace_patch_url, headers=patch_headers, verify=False, timeout=30, json=namespace_patch_body)
            if resp.status_code in (200, 201, 204):
                lsf.write_output('  acme-east-prod-wrp4h: storage class quota patched')
                break
            lsf.write_output(f'  attempt {attempt}/{max_attempts}: could not patch namespace (HTTP {resp.status_code}): {resp.text[:300]}')
        except Exception as e:
            lsf.write_output(f'  attempt {attempt}/{max_attempts}: could not patch namespace: {e}')
        if attempt < max_attempts:
            time.sleep(20)
    else:
        lsf.write_output(f'  WARNING: could not patch namespace after {max_attempts} attempts -- giving up')


if __name__ == '__main__':
    # Standalone entry point for testing the blueprint installer directly
    # against a live pod, without running the rest of adjustomatic.py.
    # Deliberately runs ONLY install_vcfa_blueprints() -- NOT
    # patch_supervisor_namespace_storage_quota(), which is invoked from
    # adjustomatic.py's main() instead. Mirrors adjustomatic.py's own
    # main() bootstrap (sys.path.append('/hol') + import lsfunctions) --
    # on manager, /hol isn't a real path, so invoke with PYTHONPATH set
    # instead, same gotcha as adjustomatic.py itself:
    #   PYTHONPATH=/home/holuser/hol python3 install_vcfa_blueprints.py
    import sys
    sys.path.append('/hol')
    import lsfunctions as lsf

    install_vcfa_blueprints(lsf)