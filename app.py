import json
import asyncio
from pathlib import Path
from typing import Dict
from flask import Flask, render_template, request, redirect, url_for, abort, make_response

from nucore_client import fetch_new_orders, load_config

app = Flask(__name__)

DATA_PATH = Path("/app/data/orders.json")
DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

ALLOWED_STATUSES = ["New", "In Process", "Canceled", "Complete", "Unrecoverable"]

def load_orders() -> Dict[str, dict]:
    if DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return {}

def save_orders(data: Dict[str, dict]) -> None:
    DATA_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

def as_key(order_id: str, order_detail_id: str) -> str:
    return f"{order_id}-{order_detail_id}"

@app.get("/")
def index():
    orders = load_orders()
    return render_template("index.html", orders=list(orders.values()), statuses=ALLOWED_STATUSES)

@app.post("/import")
def import_orders():
    """Scrape NUcore and store NEW orders locally (sync wrapper around async)."""
    cfg = load_config()
    new_orders = asyncio.run(fetch_new_orders(cfg))
    data: Dict[str, dict] = {}
    for row in new_orders:
        key = as_key(str(row["order"]), str(row["order_detail"]))
        data[key] = {
            "key": key,
            "order": str(row["order"]),
            "order_detail": str(row["order_detail"]),
            "product": row["product"],
            "local_status": "New",
        }
    save_orders(data)
    return redirect(url_for("index"))

@app.post("/orders/<key>/status")
def update_status(key: str):
    orders = load_orders()
    if key not in orders:
        abort(404)
    new_status = (request.form.get("status") or "").strip()
    if new_status not in ALLOWED_STATUSES:
        abort(make_response(("Invalid status", 400)))
    orders[key]["local_status"] = new_status
    save_orders(orders)
    # Return just the updated row for HTMX swap
    return render_template("_row.html", row=orders[key], statuses=ALLOWED_STATUSES)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
