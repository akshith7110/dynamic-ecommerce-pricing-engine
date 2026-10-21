"""Local demo server: fixed retail sources, persisted observations, IAM-signed cloud check."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from market import BASIS, CATALOG, collect_catalog
from trends import analyze_history


class Demo:
    def __init__(self, profile, region, stack, email):
        self.settings = dict(profile=profile, region=region, stack=stack, email=email)
        self.lock = threading.Lock()
        self.last_attempt = 0
        self.cache_path = ROOT / ".local" / "retail-history.json"
        source = self.cache_path if self.cache_path.exists() else ROOT / "demo/data/retail-snapshot.json"
        self.data = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {"snapshot": None, "observations": []}
        if not self.data.get("snapshot") or not any(key in CATALOG for key in self.data["snapshot"].get("products", {})):
            self.data = json.loads((ROOT / "demo/data/retail-snapshot.json").read_text(encoding="utf-8"))
        self.data["observations"] = [row for row in self.data.get("observations", []) if row.get("catalogId") in CATALOG and row.get("recordId")]
        self.inspection_path = ROOT / ".local" / "aws-inspection.json"
        # Saved evidence remains on disk; require a fresh inspection each launch
        # so a switched profile/account never inherits another session's badges.
        self.inspection = None

    def state(self):
        with self.lock:
            data = json.loads(json.dumps(self.data))
        analytics = {}
        for key in CATALOG:
            groups = {}
            for row in data["observations"]:
                if row["catalogId"] == key:
                    groups.setdefault(row["series"], []).append(row)
            choices = [{"series": series, "seller": rows[0]["seller"], "rows": rows, "analysis": analyze_history(rows)} for series, rows in groups.items()]
            choices.sort(key=lambda choice: (bool(choice["analysis"]["forecast"]), choice["analysis"].get("lastDate", ""), choice["analysis"]["distinctDates"]), reverse=True)
            analytics[key] = choices
        return {**data, "analytics": analytics, "catalog": CATALOG, "awsInspection": self.inspection,
                "architecture": "API Gateway → EventBridge → Lambda → DynamoDB → SES",
                "cloud": {"stack": self.settings["stack"], "region": self.settings["region"]}}

    def inspect_aws(self):
        import boto3
        from aws_inspection import inspect_deployment
        report = inspect_deployment(boto3.Session(profile_name=self.settings["profile"], region_name=self.settings["region"]),
                                    self.settings["stack"], self.settings["email"])
        report = json.loads(json.dumps(report, default=str))
        self.inspection_path.parent.mkdir(parents=True, exist_ok=True)
        self.inspection_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        self.inspection = report
        return report

    def refresh(self):
        with self.lock:
            if time.monotonic() - self.last_attempt < 300:
                return {"message": "Refresh limited to once every five minutes to respect the public data API."}
            self.last_attempt = time.monotonic()
            snapshot = collect_catalog()
            self.data["lastAttempt"] = snapshot["checkedAt"]
            self.data["refreshErrors"] = snapshot["errors"]
            successful = any(product["offers"] for product in snapshot["products"].values())
            if successful:
                for key, product in snapshot["products"].items():
                    if not product["offers"] and self.data.get("snapshot", {}).get("products", {}).get(key, {}).get("offers"):
                        snapshot["products"][key] = self.data["snapshot"]["products"][key]
                self.data["snapshot"] = snapshot
                existing = {(row["catalogId"], row["recordId"]): row for row in self.data["observations"]}
                existing.update({(row["catalogId"], row["recordId"]): row for row in snapshot["observations"]})
                self.data["observations"] = list(existing.values())[-10000:]
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            temporary.replace(self.cache_path)
        return {"message": "Scrape finished. Each offer retains its actual collection timestamp." if successful else
                           "Sources unavailable. Showing the previous saved real snapshot, if present; check its collection date."}

    def cloud_check(self, catalog_id):
        # Credentials stay in the local Python process; the browser receives no keys.
        import boto3
        from botocore.config import Config
        from demo import request
        if not isinstance(catalog_id, str) or catalog_id not in CATALOG:
            raise ValueError("Unknown catalog product")
        session = boto3.Session(profile_name=self.settings["profile"], region_name=self.settings["region"])
        stack = session.client("cloudformation", config=Config(connect_timeout=4, read_timeout=8, retries={"max_attempts": 1})).describe_stacks(
            StackName=self.settings["stack"])["Stacks"][0]
        outputs = {row["OutputKey"]: row["OutputValue"] for row in stack["Outputs"]}
        api, product_id = outputs["ApiUrl"], "web-" + catalog_id
        product = {"productId": product_id, "name": CATALOG[catalog_id]["name"], "searchQuery": CATALOG[catalog_id]["sku"],
                   "priceSource": "web", "catalogId": catalog_id, "comparisonBasis": BASIS, "currency": "EUR", "marketplace": "WEB_FR",
                   "currentPrice": CATALOG[catalog_id]["currentPrice"], "minimumPrice": CATALOG[catalog_id]["minimumPrice"],
                   "undercutPercent": "5", "vendorEmail": self.settings["email"]}
        status, created = request(session, api, "POST", "/products", product)
        if status not in (201, 409):
            raise RuntimeError(f"Product API returned {status}. Run Start-RealPricingDemo.ps1 -Deploy to upgrade the collector.")
        status, products = request(session, api, "GET", "/products")
        existing = next((row for row in products if row.get("productId") == product_id), {}) if status == 200 else {}
        _numeric = {"currentPrice", "minimumPrice", "undercutPercent"}
        if any((Decimal(str(existing.get(key))) != Decimal(str(product[key]))) if key in _numeric
               else str(existing.get(key)) != str(product[key])
               for key in ("priceSource", "catalogId", "comparisonBasis", "currency", "currentPrice", "minimumPrice", "undercutPercent", "vendorEmail")):
            raise RuntimeError("Cloud product has different settings or old code. Run the -Deploy launcher before using cloud check.")
        status, accepted = request(session, api, "POST", "/checks", {"productId": product_id})
        event_id = accepted.get("Entries", [{}])[0].get("EventId")
        if status != 200 or accepted.get("FailedEntryCount") or not event_id:
            raise RuntimeError("EventBridge did not accept the price check")
        deadline = time.monotonic() + 90
        latest = None
        cooldown = next((int(row["ParameterValue"]) for row in stack.get("Parameters", [])
                         if row["ParameterKey"] == "AlertCooldownSeconds"), 86400)
        while time.monotonic() < deadline:
            status, rows = request(session, api, "GET", f"/products/{product_id}/prices?limit=20")
            matched = next((row for row in rows if row.get("observationId") == event_id and row.get("source") == "web"), None) if status == 200 else None
            if matched:
                latest = matched
                status, products = request(session, api, "GET", "/products")
                state = next((row for row in products if row.get("productId") == product_id), {}) if status == 200 else {}
                threshold = Decimal(existing["currentPrice"]) * (1 - Decimal(existing["undercutPercent"]) / 100)
                notification = None
                if Decimal(latest["competitorPrice"]) >= threshold:
                    notification = "not_required_threshold_not_met"
                elif state.get("lastAlertObservation") == event_id:
                    notification = "accepted_by_ses"
                elif state.get("lastAlertAt") and (datetime.now(timezone.utc) - datetime.fromisoformat(state["lastAlertAt"])).total_seconds() < cooldown:
                    notification = "prior_alert_cooldown"
                if notification:
                    return {"eventId": event_id, "apiUrl": api, "observation": latest,
                            "notification": notification, "lastAlertAt": state.get("lastAlertAt"),
                            "suggestedPrice": state.get("suggestedPrice") if notification == "accepted_by_ses" else None,
                            "note": "SES acceptance does not prove inbox delivery. A prior-alert cooldown is inferred from saved state; it does not confirm this event reached the evaluator."}
            time.sleep(3)
        return {"eventId": event_id, "observation": latest, "notification": "pending_or_threshold_not_met",
                "note": "Check Lambda logs and the failure queue if no observation arrives. No mock data substituted."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--profile", default="pricing-personal")
    parser.add_argument("--region", default="ap-south-1")
    parser.add_argument("--stack", default="dynamic-pricing-demo")
    parser.add_argument("--email", default="dhruv.agnihotri2024@vitstudent.ac.in")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    demo = Demo(args.profile, args.region, args.stack, args.email)
    allowed_hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, data, content_type="application/json"):
            raw = json.dumps(data).encode() if content_type == "application/json" else data
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.headers.get("Host") not in allowed_hosts:
                return self.send(403, {"error": "Localhost only"})
            if self.path == "/api/state":
                return self.send(200, demo.state())
            assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
            if self.path in assets:
                name, kind = assets[self.path]
                return self.send(200, (ROOT / "demo" / name).read_bytes(), kind)
            self.send(404, {"error": "Not found"})

        def do_POST(self):
            origin = self.headers.get("Origin")
            if (self.headers.get("Host") not in allowed_hosts or self.headers.get("X-Pricing-Demo") != "1"
                    or (origin and origin not in {"http://" + host for host in allowed_hosts})):
                return self.send(403, {"error": "Local demo origin required"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 2048:
                    return self.send(413, {"error": "Request too large"})
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                if self.path == "/api/refresh":
                    return self.send(200, demo.refresh())
                if self.path == "/api/cloud":
                    return self.send(200, demo.cloud_check(body.get("catalogId", "nutella-400g")))
                if self.path == "/api/inspect-aws":
                    return self.send(200, demo.inspect_aws())
                self.send(404, {"error": "Not found"})
            except Exception as exc:
                # Do not expose credential/provider error details to the page.
                self.send(500, {"error": str(exc) if isinstance(exc, (ValueError, RuntimeError)) else
                               f"{type(exc).__name__}: check AWS sign-in/profile or source connectivity in your terminal."})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Real pricing demo ready: {url} (Ctrl+C stops it)", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
