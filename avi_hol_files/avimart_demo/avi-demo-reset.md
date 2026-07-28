# Avi Demo Reset

Resets the `WAF-avimart-policy` WAF policy on the Avi controller to a clean pre-demo state:

| Setting | Before demo | After reset |
|---|---|---|
| `mode` | `WAF_MODE_ENFORCEMENT` | `WAF_MODE_DETECTION_ONLY` |
| `pre_crs_groups[virtualpatch].enable` | `true` | `false` |

## Prerequisites

- `curl` and `python3` available on the host running the script
- Network access to the Avi controller

## Usage

```bash
export AVI_PASSWORD='your-password'
./avi-demo-reset.sh
```

> **Note:** Use single quotes around the password if it contains `!` — double quotes trigger bash history expansion for that character.

The script prints the final values of both changed fields and exits non-zero if either did not apply correctly.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `AVI_PASSWORD` | *(required)* | Admin password |
| `AVI_USER` | `admin` | Admin username |
| `AVI_HOST` | `sfo-w01-avilb01.sfo.rainpole.io` | Controller hostname or IP |
| `AVI_TENANT` | `chrisblog` | Avi tenant name |
| `AVI_VERSION` | `32.1.1` | API version header |
| `AVI_DEBUG` | `0` | Set to `1` to print raw API responses for troubleshooting |
