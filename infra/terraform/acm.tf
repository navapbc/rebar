# ---------------------------------------------------------------------------
# acm.tf — wildcard TLS certificate for *.solutions.navateam.com (re-homed from snap)
# ---------------------------------------------------------------------------
# Domain-wide wildcard cert used by the auth_host SSO CloudFront distribution
# (auth_host.tf) and available to any other *.solutions.navateam.com surface.
# Adopted from snap's shared state (epic gaugeable-combatable-skylark). DNS-validated
# into the same Route53 public zone rebar already uses (var.dns_zone_id).
resource "aws_acm_certificate" "wildcard" {
  domain_name       = "*.${local.sso_domain}"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
    # Shared, production-critical cert; never let a destroy/replace slip through.
    prevent_destroy = true
  }

  tags = {
    Project = "rebar"
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.wildcard.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = var.dns_zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
  allow_overwrite = true
}

# NOTE: snap's config had an `aws_acm_certificate_validation.wildcard` gate here. That
# resource only blocks until DNS validation completes at CREATION time — irrelevant for a
# cert rebar ADOPTED already ISSUED (and it "doesn't support import"). The cert auto-renews
# off the aws_route53_record.cert_validation records above, so consumers reference
# aws_acm_certificate.wildcard.arn directly (see auth_host.tf viewer_certificate).
