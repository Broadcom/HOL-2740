data avi_healthmonitor system-http {
  name = "System-HTTP"
}

data avi_applicationprofile system-http {
  name = "System-HTTP"
}

data avi_cloud nsx-cloud {
  name = "Cloud-nsx-wld01-a"
}
data avi_tenant admin {
  name = "admin"
}
data avi_vrfcontext t1-gw-sitea {
  name = "wld01-a-t1"
}
data avi_network vm-segment {
  name = "wld01-avi-data"
}
data avi_networkprofile system-tcp-proxy {
  name = "System-TCP-Proxy"
}
data avi_sslprofile system-standard {
    name = "System-Standard"
}
data avi_sslkeyandcertificate wildcard-rsa {
    name = "hol_wildcard"
}
data avi_sslkeyandcertificate default-ecc {
    name = "System-Default-Cert-EC"
}