from __future__ import annotations

import argparse
import random
from datetime import date, datetime, timedelta

from app.config import Config
from app.db import Database


def ensure_schema(db: Database) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            city TEXT,
            signup_date TEXT
        );
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT,
            price REAL NOT NULL
        );
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            order_date TEXT NOT NULL,
            total REAL NOT NULL DEFAULT 0
        );
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL
        );
        """
    )
    # Helpful indexes
    db.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_items_product ON order_items(product_id);")


def reset_data(db: Database) -> None:
    # Clear data in FK order
    db.execute("DELETE FROM order_items;")
    db.execute("DELETE FROM orders;")
    db.execute("DELETE FROM products;")
    db.execute("DELETE FROM customers;")


def random_date(start: date, end: date) -> str:
    span = (end - start).days
    d = start + timedelta(days=random.randint(0, span))
    return d.isoformat()


def seed_data(
    db: Database,
    customers_n: int = 50,
    products_n: int = 40,
    orders_n: int = 300,
) -> None:
    random.seed(42)

    # Basic pools
    first_names = [
        "Alice","Bob","Carol","Dave","Eve","Frank","Grace","Heidi","Ivan","Judy","Mallory","Niaj","Olivia","Peggy","Rupert","Sybil","Trent","Uma","Victor","Wendy","Xavier","Yasmin","Zane"
    ]
    last_names = [
        "Smith","Johnson","Williams","Brown","Jones","Miller","Davis","Garcia","Rodriguez","Wilson","Martinez","Anderson","Taylor","Thomas","Hernandez","Moore","Martin","Jackson","Thompson","White"
    ]
    cities = [
        "New York","San Francisco","Los Angeles","Seattle","Chicago","Boston","Austin","Denver","Miami","Atlanta"
    ]
    adjectives = [
        "Smart","Ultra","Pro","Nano","Eco","Rapid","Prime","Lite","Max","Quantum","Hyper","Fusion","Terra","Aero","Aqua"
    ]
    nouns = [
        "Widget","Gizmo","Device","Hub","Sensor","Cable","Charger","Adapter","Module","Panel","Kit","Bundle","Service","Subscription","License"
    ]
    categories = ["Gadgets","Accessories","Services","Software","Hardware","Apparel"]

    # Customers
    if db.query("SELECT COUNT(*) FROM customers").rows[0][0] == 0:
        cid = 1
        for _ in range(customers_n):
            name = f"{random.choice(first_names)} {random.choice(last_names)}"
            city = random.choice(cities)
            signup = random_date(date(2023, 1, 1), date(2025, 1, 1))
            db.execute("INSERT INTO customers(id, name, city, signup_date) VALUES (?, ?, ?, ?)", [cid, name, city, signup])
            cid += 1

    # Products
    if db.query("SELECT COUNT(*) FROM products").rows[0][0] == 0:
        pid = 1
        for _ in range(products_n):
            name = f"{random.choice(adjectives)} {random.choice(nouns)}"
            category = random.choice(categories)
            base = {
                "Gadgets": (20, 120),
                "Accessories": (5, 40),
                "Services": (50, 200),
                "Software": (30, 150),
                "Hardware": (80, 400),
                "Apparel": (10, 90),
            }[category]
            price = round(random.uniform(*base), 2)
            db.execute("INSERT INTO products(id, name, category, price) VALUES(?, ?, ?, ?)", [pid, name, category, price])
            pid += 1

    # Orders + items
    if db.query("SELECT COUNT(*) FROM orders").rows[0][0] == 0:
        # Fetch current ids and prices
        customer_ids = [r[0] for r in db.query("SELECT id FROM customers").rows]
        products = db.query("SELECT id, price FROM products").rows
        if not customer_ids or not products:
            return
        oid = 1
        item_id = 1
        for _ in range(orders_n):
            cust = random.choice(customer_ids)
            odate = random_date(date(2024, 1, 1), date(2025, 6, 30))
            total = 0.0
            db.execute("INSERT INTO orders(id, customer_id, order_date, total) VALUES (?, ?, ?, 0)", [oid, cust, odate])

            # 1-5 items
            for _ in range(random.randint(1, 5)):
                prod_id, base_price = random.choice(products)
                qty = random.randint(1, 3)
                # occasional discount +/- 10%
                unit_price = round(base_price * random.uniform(0.9, 1.1), 2)
                total += qty * unit_price
                db.execute(
                    "INSERT INTO order_items(id, order_id, product_id, quantity, unit_price) VALUES (?, ?, ?, ?, ?)",
                    [item_id, oid, prod_id, qty, unit_price],
                )
                item_id += 1
            db.execute("UPDATE orders SET total = ? WHERE id = ?", [round(total, 2), oid])
            oid += 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed or reseed the SQLite database with demo data")
    parser.add_argument("--reset", action="store_true", help="Delete existing data before seeding")
    parser.add_argument("--customers", type=int, default=50, help="Number of customers to create")
    parser.add_argument("--products", type=int, default=40, help="Number of products to create")
    parser.add_argument("--orders", type=int, default=300, help="Number of orders to create")
    args = parser.parse_args()

    cfg = Config.load()
    db = Database(cfg.database_url)
    ensure_schema(db)
    if args.reset:
        reset_data(db)
    seed_data(db, customers_n=args.customers, products_n=args.products, orders_n=args.orders)
    print("Seed complete. Current schema:")
    print(db.schema())


if __name__ == "__main__":
    main()
