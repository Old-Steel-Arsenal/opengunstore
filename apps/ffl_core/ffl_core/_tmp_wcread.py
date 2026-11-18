import json, frappe
def run(tag="read"):
    from ffl_woo_sync.woocommerce.client import WooCommerceClient
    wc = WooCommerceClient.from_settings("retail")
    pid = frappe.db.get_value("Item", "OPTTEST-MAG", "woo_product_id")
    p = wc.get_product(pid)
    vs = wc.list_variations(pid)
    meta = lambda m: {x["key"]: x["value"] for x in m if x["key"].startswith("_osa_preorder")}
    out = {"parent": {k: p.get(k) for k in ("id", "name", "type", "status", "sku", "description", "date_modified_gmt")},
        "parent_images": [i.get("src") for i in p.get("images") or []],
        "parent_attributes": [(a["name"], a["options"], a.get("variation")) for a in p["attributes"]],
        "parent_preorder_meta": meta(p.get("meta_data") or []),
        "variations": sorted([{"id": v["id"], "sku": v["sku"], "status": v["status"], "modified": v.get("date_modified_gmt"), "regular_price": v["regular_price"],
            "stock_quantity": v.get("stock_quantity"), "stock_status": v.get("stock_status"),
            "attrs": [(a["name"], a["option"]) for a in v["attributes"]], "preorder": meta(v.get("meta_data") or [])}
            for v in vs.values()], key=lambda v: v["sku"])}
    path = f"/tmp/wc-{tag}.json"
    open(path, "w").write(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
