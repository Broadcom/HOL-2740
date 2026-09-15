terraform {
  required_providers {
    avi = {
      source = "vmware/avi"
    }
  }
}

locals {
  avi_password = chomp(file("/home/holuser/creds.txt"))
}

provider "avi" {
  avi_username             = "admin"
  avi_password             = local.avi_password
  avi_controller           = "alb-a.site-a.vcf.lab"
  avi_version              = "32.1.1"
}