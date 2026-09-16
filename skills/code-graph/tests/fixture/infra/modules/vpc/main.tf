resource "aws_vpc" "this" {
  cidr_block = var.cidr
}

resource "aws_subnet" "a" {
  vpc_id = aws_vpc.this.id
}
