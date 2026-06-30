# A record pointing rebar.solutions.navateam.com at the Gerrit Elastic IP.
resource "aws_route53_record" "gerrit" {
  zone_id = var.dns_zone_id
  name    = var.dns_name
  type    = "A"
  ttl     = 300
  records = [aws_eip.gerrit.public_ip]
}
