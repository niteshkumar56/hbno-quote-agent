"""Loads the reference data (catalog, price levels, customers, stock, flags, holidays)."""
from __future__ import annotations

import csv
import functools
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from . import config

PACK_SIZES = ["4 oz", "16 oz", "1 gal", "5 gal", "55 gal drum"]


@dataclass
class Product:
    name: str
    category: str
    aliases: list[str]
    packs: dict[str, str] = field(default_factory=dict)      # pack_size -> sku


@dataclass
class Tier:
    min_qty: int
    unit_price: Decimal
    floor: Decimal
    valid_days: int


@dataclass
class RefData:
    products: dict[str, Product]          # product name -> Product
    skus: dict[str, dict]                 # sku -> catalog row (+ "product")
    tiers: dict[str, list[Tier]]          # sku -> tiers ascending by min_qty
    customers: dict[str, dict]            # email_domain -> customer row
    stock: dict[str, int]
    flags: dict
    holidays: set[str]
    synonyms: dict[str, list[str]]        # product name -> extra (e.g. translated) names

    def flag_severity(self, code: str) -> str:
        return self.flags["flags"][code]["severity"]

    @functools.cached_property
    def vocabulary(self) -> frozenset[str]:
        """Every word in a catalog name, alias or synonym (lowercase). The redactor never treats
        these as personal names, so 'Rosemary' in a request survives redaction."""
        words: set[str] = set()
        for p in self.products.values():
            for s in [p.name, *p.aliases, *self.synonyms.get(p.name, [])]:
                words |= set(re.findall(r"[a-záéíóúñç]+", s.lower()))
        return frozenset(words)


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def load(data_dir: Path | None = None) -> RefData:
    data_dir = Path(data_dir or config.DATA_DIR)
    products: dict[str, Product] = {}
    skus: dict[str, dict] = {}
    for row in _read_csv(data_dir / "catalog.csv"):
        p = products.setdefault(row["name"], Product(
            name=row["name"], category=row["category"],
            aliases=[a.strip() for a in row["aliases"].split(";") if a.strip()]))
        p.packs[row["pack_size"]] = row["sku"]
        skus[row["sku"]] = {**row, "active": row["active"].upper() == "Y"}

    tiers: dict[str, list[Tier]] = {}
    for row in _read_csv(data_dir / "price_levels.csv"):
        tiers.setdefault(row["sku"], []).append(Tier(
            int(row["min_qty"]), Decimal(row["unit_price_usd"]),
            Decimal(row["price_floor_usd"]), int(row["quote_valid_business_days"])))
    for t in tiers.values():
        t.sort(key=lambda x: x.min_qty)

    customers = {r["email_domain"].lower(): r for r in _read_csv(data_dir / "customers.csv")}
    stock = {r["sku"]: int(r["available_qty"]) for r in _read_csv(data_dir / "stock.csv")}

    flags = json.loads((config.CONFIG_DIR / "flags.json").read_text(encoding="utf-8"))
    holidays = set(json.loads((config.CONFIG_DIR / "holidays.json").read_text(encoding="utf-8"))["holidays"])
    syn_path = config.CONFIG_DIR / "synonyms.json"
    synonyms = json.loads(syn_path.read_text(encoding="utf-8"))["synonyms"] if syn_path.exists() else {}

    return RefData(products, skus, tiers, customers, stock, flags, holidays, synonyms)


def catalog_for_prompt(ref: RefData) -> str:
    """Product names, pack sizes and aliases only. No SKUs' prices, stock or status."""
    lines = []
    for p in ref.products.values():
        packs = ", ".join(s for s in PACK_SIZES if s in p.packs)
        lines.append(f"{p.name} | {p.category} | {packs} | {'; '.join(p.aliases)}")
    return "\n".join(lines)
