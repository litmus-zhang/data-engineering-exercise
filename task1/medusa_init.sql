-- medusa_init.sql: Define schema and seed initial data for Medusa DB
CREATE TABLE customers (
    id          SERIAL PRIMARY KEY,
    name        TEXT,
    email       TEXT,
    created_at  DATE
);
CREATE TABLE products (
    id          SERIAL PRIMARY KEY,
    name        TEXT,
    price       NUMERIC(10,2),    -- price in default currency (e.g., USD)
    currency    TEXT
);
CREATE TABLE orders (
    id            SERIAL PRIMARY KEY,
    order_date    DATE,
    customer_id   INT REFERENCES customers(id),
    currency      TEXT,
    total         NUMERIC(10,2)
);
CREATE TABLE order_items (
    id          SERIAL PRIMARY KEY,
    order_id    INT REFERENCES orders(id) ON DELETE CASCADE,
    product_id  INT REFERENCES products(id),
    quantity    INT,
    price       NUMERIC(10,2)     -- price per unit in order currency
);
-- Insert sample customers
INSERT INTO customers (name, email, created_at) VALUES
    ('Alice Smith', 'alice@example.com', '2025-05-01'),
    ('Bob Johnson', 'bob@example.com', '2025-05-05');
-- Insert sample products
INSERT INTO products (name, price, currency) VALUES
    ('Laptop', 1200.00, 'USD'),
    ('Phone', 800.00, 'USD'),
    ('Tablet', 600.00, 'EUR');
-- Insert sample orders (mix currencies for FX conversion demo)
INSERT INTO orders (order_date, customer_id, currency, total) VALUES
    ('2025-05-20', 1, 'USD', 1200.00),   -- Alice buys 1 Laptop for $1200
    ('2025-05-21', 1, 'EUR', 600.00),    -- Alice buys 1 Tablet for €600
    ('2025-05-21', 2, 'USD', 1600.00);   -- Bob buys 2 Phones for $1600
-- Insert order items corresponding to orders
INSERT INTO order_items (order_id, product_id, quantity, price) VALUES
    (1, 1, 1, 1200.00),  -- Order 1: 1 Laptop at $1200
    (2, 3, 1, 600.00),   -- Order 2: 1 Tablet at €600
    (3, 2, 2, 800.00);   -- Order 3: 2 Phones at $800 each = $1600
