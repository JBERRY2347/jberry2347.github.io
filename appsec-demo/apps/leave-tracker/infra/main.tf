resource "aws_s3_bucket" "attachments" {
  bucket = "acme-leave-attachments"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "attachments" {
  bucket = aws_s3_bucket.attachments.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_db_instance" "leave" {
  identifier          = "leave-tracker"
  engine              = "postgres"
  instance_class      = "db.t3.small"
  allocated_storage   = 20
  storage_encrypted   = true
  publicly_accessible = false
}
