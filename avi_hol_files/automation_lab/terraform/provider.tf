terraform {
  required_providers {
    avi = {
      source = "vmware/avi"
    }
  }
}

provider "avi" {
  avi_username             = "admin"
  avi_password             = var.avi_password
  avi_controller           = "alb-a.site-a.vcf.lab"
  avi_version              = "32.1.1"
}