# startlist

The "startlist" is merged with the default proxy allowlist during labstartup.

Once labstartup is complete, then the "allowlist" is used to restrict access to domains.

Use caution adding an allowlist file as it overwrites the predefined allowlist. For reference, here are the contents of the default allowlist:

```plain
# allowlist - HOLFY27 Default Domain Allowlist
# Version 5.0 - 2026-02-12
# Domains allowed through the squid proxy filter

# VMware / Broadcom domains
vmware.com
broadcom.com
broadcom.okta.com
tanzu.vmware.com
tanzu.broadcom.com
hashicorp.com
download.java.net
odyssey.vmware.com
odyssey-dev.vmware.com
odyssey-stage.vmware.com
s3.amazonaws.com
s3-us-west-1.amazonaws.com
archive.ubuntu.com
security.ubuntu.com
packages.microsoft.com
q1uk5yfspf.execute-api.us-east-2.amazonaws.com
cjguvz7527.execute-api.us-west-1.amazonaws.com
auyrv6ve2i.execute-api.us-west-2.amazonaws.com

# oh-my-posh custom prompt
ohmyposh.dev

```
