resource "aws_s3_bucket" "exports" {
  bucket = "acme-payments-exports"
  acl    = "public-read"
}

resource "aws_security_group" "portal" {
  name = "payments-portal"

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_instance" "ledger" {
  identifier        = "payments-ledger"
  engine            = "postgres"
  instance_class    = "db.t3.medium"
  allocated_storage = 50
  storage_encrypted = false
}
