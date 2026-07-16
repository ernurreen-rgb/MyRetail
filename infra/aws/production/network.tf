resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${var.name_prefix}-vpc"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.name_prefix}-igw"
  }
}

resource "aws_subnet" "public" {
  count = 3

  vpc_id                  = aws_vpc.main.id
  availability_zone       = var.availability_zones[count.index]
  cidr_block              = var.public_subnet_cidrs[count.index]
  map_public_ip_on_launch = false

  tags = {
    Name = "${var.name_prefix}-public-${var.availability_zones[count.index]}"
    Tier = "public"
  }
}

resource "aws_subnet" "app" {
  count = 3

  vpc_id            = aws_vpc.main.id
  availability_zone = var.availability_zones[count.index]
  cidr_block        = var.app_subnet_cidrs[count.index]

  tags = {
    Name = "${var.name_prefix}-app-${var.availability_zones[count.index]}"
    Tier = "application"
  }
}

resource "aws_subnet" "database" {
  count = 3

  vpc_id            = aws_vpc.main.id
  availability_zone = var.availability_zones[count.index]
  cidr_block        = var.database_subnet_cidrs[count.index]

  tags = {
    Name = "${var.name_prefix}-database-${var.availability_zones[count.index]}"
    Tier = "database"
  }
}

resource "aws_eip" "nat" {
  count = 3

  domain = "vpc"

  tags = {
    Name = "${var.name_prefix}-nat-${var.availability_zones[count.index]}"
  }

  depends_on = [aws_internet_gateway.main]
}

resource "aws_nat_gateway" "main" {
  count = 3

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = {
    Name = "${var.name_prefix}-nat-${var.availability_zones[count.index]}"
  }

  depends_on = [aws_internet_gateway.main]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${var.name_prefix}-public"
  }
}

resource "aws_route_table_association" "public" {
  count = 3

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "app" {
  count = 3

  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main[count.index].id
  }

  tags = {
    Name = "${var.name_prefix}-app-${var.availability_zones[count.index]}"
  }
}

resource "aws_route_table_association" "app" {
  count = 3

  subnet_id      = aws_subnet.app[count.index].id
  route_table_id = aws_route_table.app[count.index].id
}

resource "aws_route_table" "database" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.name_prefix}-database-isolated"
  }
}

resource "aws_route_table_association" "database" {
  count = 3

  subnet_id      = aws_subnet.database[count.index].id
  route_table_id = aws_route_table.database.id
}
