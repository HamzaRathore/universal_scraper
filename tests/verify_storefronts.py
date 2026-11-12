"""Run one bounded CLI smoke test for each of 30 public storefronts."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
STOREFRONTS = {
    "Revolve": "https://www.revolve.com/dresses-lace/br/cec96b/?&keyword=lace+dresses",
    "Vinted": "https://www.vinted.co.uk/catalog?search_text=dress",
    "ASOS": "https://www.asos.com/women/dresses/cat/?cid=8799",
    "Zara": "https://www.zara.com/us/en/woman-dresses-l1066.html",
    "McQueen": "https://www.mcqueen.com/en-us/",
    "Engine": "https://engine.com.pk/collections/all",
    "Breakout": "https://www.breakout.com.pk/collections/all",
    "Versace": "https://www.versace.com/us/en/",
    "H&M": "https://www2.hm.com/en_us/women/products/dresses.html",
    "Mango": "https://shop.mango.com/us/en",
    "UNIQLO": "https://www.uniqlo.com/us/en/",
    "Nike": "https://www.nike.com/w/mens-shoes-nik1zy7ok",
    "adidas": "https://www.adidas.com/us/men-shoes",
    "PUMA": "https://us.puma.com/us/en/men/shoes",
    "New Balance": "https://www.newbalance.com/men/shoes/",
    "Levi's": "https://www.levi.com/US/en_US/",
    "Gap": "https://www.gap.com/browse/women/dresses?cid=13658",
    "Abercrombie": "https://www.abercrombie.com/shop/a-wd/womens-dresses-and-jumpsuits",
    "Aritzia": "https://www.aritzia.com/us/en",
    "Gucci": "https://www.gucci.com/us/en/",
    "Prada": "https://www.prada.com/us/en.html",
    "Burberry": "https://us.burberry.com/l/womens-coats-jackets/trench-coats/",
    "Coach": "https://www.coach.com/",
    "Michael Kors": "https://www.michaelkors.global/pk/en/women/handbags/",
    "Reformation": "https://www.thereformation.com/dresses",
    "Gymshark": "https://www.gymshark.com/collections/all-products",
    "Sapphire": "https://pk.sapphireonline.pk/collections/ready-to-wear",
    "Limelight": "https://www.limelight.pk/collections/all",
    "Gul Ahmed": "https://www.gulahmedshop.com/",
    "Khaadi": "https://global.khaadi.com/floral-sleeveless-kurta/1-26-231-A-J1-VG_MULTI.html",
}


BASELINE_FAILURES = {
    "Revolve", "ASOS", "Zara", "McQueen", "Versace", "H&M", "Mango", "UNIQLO", "adidas",
    "New Balance", "Levi's", "Gap", "Abercrombie", "Aritzia", "Gucci", "Prada", "Coach", "Michael Kors",
}


def check(entry):
    brand, url = entry
    command = [
        sys.executable, str(ROOT / "universal_scraper.py"), url, "--output", "-",
        "--max-pages", "1", "--max-sitemaps", "0", "--max-products", "1",
        "--max-details", "2", "--timeout", "10", "--no-robots-cache", "--no-mongo",
    ]
    result = {"brand": brand, "url": url}
    try:
        with TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            process = subprocess.run([*command, "--report", str(report)], capture_output=True, encoding="utf-8",
                                     errors="replace", timeout=150, cwd=ROOT)
            result["job"] = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
    except subprocess.TimeoutExpired as exc:
        return dict(result, status="run_timeout", log=(exc.stderr or b"").decode("utf-8", "replace").splitlines())
    log = process.stderr.splitlines()
    result.update(exit_code=process.returncode, log=log)
    stats = next((json.loads(line) for line in reversed(log) if line.startswith('{"records":')), {})
    result["stats"] = stats
    row = next((json.loads(line) for line in process.stdout.splitlines() if line.strip()), {})
    if process.returncode == 0 and row.get("record_type") == "product" and row.get("title") and row.get("url"):
        result["status"] = "product_detail" if row["detail_status"] == "fetched" else "product_card"
        result["sample"] = {key: row.get(key) for key in
                            ("url", "title", "method", "price", "currency", "missing_fields")}
    elif not stats:
        result["status"] = "cli_error"
    else:
        result["status"] = next((key for key in (
            "rate_limited", "robots_unavailable", "robots_skipped", "http_errors", "browser_errors",
        ) if stats.get(key)), "no_product")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="JSON Lines report path")
    parser.add_argument("--baseline-failures", action="store_true", help="Run the original 18 unsuccessful storefronts")
    args = parser.parse_args()
    entries = [(brand, url) for brand, url in STOREFRONTS.items()
               if not args.baseline_failures or brand in BASELINE_FAILURES]
    with args.output.open("w", encoding="utf-8") as report, ThreadPoolExecutor(max_workers=2) as pool:
        for result in pool.map(check, entries):
            report.write(json.dumps(result, ensure_ascii=False) + "\n")
            report.flush()
            print(f"{result['brand']}: {result['status']}", flush=True)


if __name__ == "__main__":
    main()
