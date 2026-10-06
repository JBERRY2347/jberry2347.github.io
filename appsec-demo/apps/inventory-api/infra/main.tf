resource "aws_s3_bucket" "exports" {
  bucket = "acme-inventory-exports"
}

resource "aws_s3_bucket_public_access_block" "exports" {
  bucket                  = aws_s3_bucket.exports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_db_instance" "inventory" {
  identifier          = "inventory"
  engine              = "postgres"
  instance_class      = "db.t3.small"
  allocated_storage   = 20
  storage_encrypted   = true
  publicly_accessible = false
}
