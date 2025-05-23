# workflow
# batch copy the data from medusa database to a new DB
# use apache beam for baych and stream processing
# store the beam result in timescale DB
# connect timescale to  metabase
# use metabase to visualize the data
# etl_pipeline.py
import os, json, datetime
import requests
import psycopg2
import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from prefect import flow, task

# Database connection settings (from env or defaults)
MEDUSA_DB_CONN = {
    "host": os.getenv("MEDUSA_HOST", "medusa-db"),
    "port": os.getenv("MEDUSA_PORT", "5432"),
    "dbname": os.getenv("MEDUSA_DB", "medusa_db"),
    "user": os.getenv("MEDUSA_USER", "medusa"),
    "password": os.getenv("MEDUSA_PASS", "medusa_password"),
}
TIMESCALE_DB_CONN = {
    "host": os.getenv("TIMESCALE_HOST", "timescale"),
    "port": os.getenv("TIMESCALE_PORT", "5432"),
    "dbname": os.getenv("TIMESCALE_DB", "ts_metrics_db"),
    "user": os.getenv("TIMESCALE_USER", "ts_user"),
    "password": os.getenv("TIMESCALE_PASS", "ts_password"),
}

FX_API_URL = "https://api.exchangerate.host/latest?base={base}&symbols={symbols}"


@task
def extract_data():
    """Extract source data from Medusa Postgres (orders, items, products, customers)."""
    conn = psycopg2.connect(**MEDUSA_DB_CONN)
    cur = conn.cursor()
    # Fetch tables
    cur.execute(
        """
        SELECT id, order_date, customer_id, currency, total 
        FROM orders;
    """
    )
    orders = [
        {
            "id": r[0],
            "order_date": r[1],
            "customer_id": r[2],
            "currency": r[3],
            "total": float(r[4]),
        }
        for r in cur.fetchall()
    ]
    cur.execute("SELECT id, name FROM customers;")
    customers = {r[0]: r[1] for r in cur.fetchall()}  # dict for quick lookup
    cur.execute("SELECT id, name, price, currency FROM products;")
    products = {
        r[0]: {"name": r[1], "price": float(r[2]), "currency": r[3]}
        for r in cur.fetchall()
    }
    cur.execute(
        """
        SELECT order_id, product_id, quantity, price 
        FROM order_items;
    """
    )
    items = [
        {"order_id": r[0], "product_id": r[1], "quantity": r[2], "price": float(r[3])}
        for r in cur.fetchall()
    ]
    cur.close()
    conn.close()
    # Return all data as a dictionary
    return {
        "orders": orders,
        "customers": customers,
        "products": products,
        "items": items,
    }


@task
def fetch_fx_rates(base_currency="USD", target_currencies=None):
    """Fetch latest FX rates for currency conversion (to USD by default)."""
    if target_currencies is None:
        target_currencies = set()
    # Identify currencies present in orders that differ from base
    currencies = ",".join(sorted(target_currencies))
    url = FX_API_URL.format(base=base_currency, symbols=currencies)
    resp = requests.get(url)
    rates = {}
    if resp.status_code == 200:
        data = resp.json()
        rates = data.get("rates", {})
    # Include base currency at 1:1
    rates[base_currency] = 1.0
    return rates


class WriteToTimescale(beam.DoFn):
    """Beam DoFn to write results into TimescaleDB (Postgres). Uses upsert logic."""

    def __init__(self, table):
        self.table = table
        # Use a single connection per DoFn instance
        self.conn = None
        self.cur = None

    def setup(self):
        self.conn = psycopg2.connect(**TIMESCALE_DB_CONN)
        self.cur = self.conn.cursor()

    def process(self, element):
        # element structure depends on target table
        if self.table == "daily_revenue":
            date, revenue = element  # key, aggregated revenue
            self.cur.execute(
                "INSERT INTO daily_revenue(date, revenue_usd) VALUES (%s, %s) "
                "ON CONFLICT (date) DO UPDATE SET revenue_usd = EXCLUDED.revenue_usd;",
                (date, revenue),
            )
        elif self.table == "daily_product_sales":
            # element key: (date, product_id, product_name), value: (total_qty, total_rev)
            (date, prod_id, prod_name), (total_qty, total_rev) = element
            self.cur.execute(
                "INSERT INTO daily_product_sales(date, product_id, product_name, quantity_sold, revenue_usd) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (date, product_id) DO UPDATE "
                "SET quantity_sold = EXCLUDED.quantity_sold, revenue_usd = EXCLUDED.revenue_usd;",
                (date, prod_id, prod_name, total_qty, total_rev),
            )
        elif self.table == "customer_clv":
            cust_id, cust_name, total_spent = element
            now = datetime.datetime.utcnow()
            self.cur.execute(
                "INSERT INTO customer_clv(customer_id, customer_name, total_spent_usd, last_updated) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (customer_id) DO UPDATE "
                "SET total_spent_usd = EXCLUDED.total_spent_usd, last_updated = EXCLUDED.last_updated;",
                (cust_id, cust_name, total_spent, now),
            )
        # We do not commit each record; commit in teardown for efficiency

    def teardown(self):
        if self.conn:
            self.conn.commit()
            self.cur.close()
            self.conn.close()


@task
def transform_and_load(data, fx_rates):
    """Transform data using Beam and load metrics into TimescaleDB."""
    orders = data["orders"]
    products = data["products"]
    customers = data["customers"]
    items = data["items"]
    # Prepare a Beam pipeline (DirectRunner for local). To switch to Dataflow or others, set runner and options accordingly.
    options = PipelineOptions(
        runner="DirectRunner",
        # example for Dataflow: runner="DataflowRunner", project="YOUR_GCP_PROJECT", region="YOUR_REGION", etc.
    )

    def convert_to_usd(amount, currency):
        # Helper to convert using fx_rates closure
        rate = fx_rates.get(currency, 1.0)
        return round(amount * rate, 2)

    with beam.Pipeline(options=options) as p:
        # PCollections
        orders_pc = p | "CreateOrders" >> beam.Create(orders)
        items_pc = p | "CreateItems" >> beam.Create(items)
        # Convert order totals to USD and key by date for daily revenue
        daily_revenue = (
            orders_pc
            | "MapDailyRevenue"
            >> beam.Map(
                lambda o: (o["order_date"], convert_to_usd(o["total"], o["currency"]))
            )
            | "SumRevenueByDate" >> beam.CombinePerKey(sum)
            | "WriteDailyRevenue" >> beam.ParDo(WriteToTimescale("daily_revenue"))
        )
        # Compute CLV per customer (sum of all orders per customer in USD)
        clv = (
            orders_pc
            | "MapCLV"
            >> beam.Map(
                lambda o: (o["customer_id"], convert_to_usd(o["total"], o["currency"]))
            )
            | "SumSpendByCustomer" >> beam.CombinePerKey(sum)
            # Enrich with customer name using side input
            | "AddCustomerName"
            >> beam.Map(
                lambda kv, cust_map: (kv[0], cust_map.get(kv[0], "Unknown"), kv[1]),
                cust_map=beam.pvalue.AsDict(
                    p | beam.Create([(cid, name) for cid, name in customers.items()])
                ),
            )
            | "WriteCLV" >> beam.ParDo(WriteToTimescale("customer_clv"))
        )
        # Compute daily product sales: join items with orders and products
        # First, prepare side inputs for orders (to get order date & currency) and products (name)
        order_info = p | "OrderInfoMap" >> beam.Create(
            [(o["id"], (o["order_date"], o["currency"])) for o in orders]
        )
        product_info = p | "ProductInfoMap" >> beam.Create(
            [(pid, prod["name"]) for pid, prod in products.items()]
        )
        # Enrich each order_item with order date and product name
        enriched_items = items_pc | "EnrichItems" >> beam.Map(
            lambda item, orders_map, prod_map: {
                "date": orders_map[item["order_id"]][0],
                "currency": orders_map[item["order_id"]][1],
                "product_id": item["product_id"],
                "product_name": prod_map.get(item["product_id"], "Unknown"),
                "quantity": item["quantity"],
                "revenue_usd": convert_to_usd(
                    item["price"] * item["quantity"], orders_map[item["order_id"]][1]
                ),
            },
            orders_map=beam.pvalue.AsDict(order_info),
            prod_map=beam.pvalue.AsDict(product_info),
        )
        # Aggregate by date & product
        product_sales = (
            enriched_items
            | "KeyByDateProduct"
            >> beam.Map(
                lambda it: (
                    (it["date"], it["product_id"], it["product_name"]),
                    (it["quantity"], it["revenue_usd"]),
                )
            )
            | "SumByProductDate"
            >> beam.CombinePerKey(
                lambda records: (
                    sum(q for q, rev in records),
                    sum(rev for q, rev in records),
                )
            )
            | "WriteProductSales" >> beam.ParDo(WriteToTimescale("daily_product_sales"))
        )
        # (Pipeline execution happens upon exiting 'with' block)
    return "ETL_success"


@flow(name="medusa_metrics_pipeline")
def run_etl_pipeline():
    # 1. Extract data from source
    data = extract_data()
    # 2. Determine unique currencies (besides USD) in orders for FX API
    currencies = {o["currency"] for o in data.result()["orders"]} - {"USD"}
    # 3. Fetch FX rates for those currencies to USD
    rates = fetch_fx_rates(base_currency="USD", target_currencies=currencies)
    # 4. Transform and load data into TimescaleDB
    result = transform_and_load(data.result(), rates.result())
    return result


if __name__ == "__main__":
    # If running as a script, just execute the flow (in practice, Prefect agent + schedule would trigger this)
    run_etl_pipeline()
