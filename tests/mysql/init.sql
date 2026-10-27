CREATE DATABASE IF NOT EXISTS biggy_test CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS biggy_test2 CHARACTER SET utf8mb4;
GRANT ALL ON biggy_test.* TO 'biggy'@'%';
GRANT ALL ON biggy_test2.* TO 'biggy'@'%';
