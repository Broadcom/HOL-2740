sudo usermod -aG docker $USER

openssl s_client -connect harbor.site-a.vcf.lab:443 -servername harbor.site-a.vcf.lab </dev/null 2>/dev/null | openssl x509 -outform PEM > harbor.site-a.vcf.lab.crt
sudo cp harbor.site-a.vcf.lab.crt /usr/local/share/ca-certificates/harbor.crt
sudo update-ca-certificates
sudo systemctl restart docker


docker pull harbor.site-a.vcf.lab/library/dvwaserver:v1
docker pull harbor.site-a.vcf.lab/library/hackazon:latest
docker pull harbor.site-a.vcf.lab/library/avi_demoweb:v1