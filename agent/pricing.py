"""Pricing from price_levels.csv only. Nothing here reads model output except quantity and SKU."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from . import config
from .data import RefData, Tier


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(config.CENT, rounding=ROUND_HALF_UP)


def tier_for(ref: RefData, sku: str, qty: int) -> Tier:
    tiers = ref.tiers.get(sku)
    if not tiers:
        raise KeyError(f"no price levels for {sku}")
    eligible = [t for t in tiers if t.min_qty <= qty]
    if not eligible:
        raise ValueError(f"quantity {qty} below minimum tier for {sku}")
    return eligible[-1]


def next_tier(ref: RefData, sku: str, qty: int) -> Tier | None:
    return next((t for t in ref.tiers[sku] if t.min_qty > qty), None)


def price_line(ref: RefData, sku: str, qty: int) -> dict:
    t = tier_for(ref, sku, qty)
    unit = money(t.unit_price)
    amount = money(unit * qty)
    out = {"unit_price": unit, "amount": amount, "tier_min_qty": t.min_qty,
           "price_floor": money(t.floor), "valid_business_days": t.valid_days, "flags": []}
    if unit < t.floor:   # integrity guard; never expected on valid data
        out["flags"].append(("PRICE_BELOW_FLOOR", f"table price {unit} is below floor {t.floor}"))
    nt = next_tier(ref, sku, qty)
    if nt is not None:
        nt_amount = money(money(nt.unit_price) * nt.min_qty)
        if nt_amount <= amount:
            out["flags"].append(("TIER_BREAK_OPPORTUNITY",
                                 f"{nt.min_qty} units at {money(nt.unit_price)} = {nt_amount} "
                                 f"(vs {qty} at {unit} = {amount}); quantity not changed"))
            out["next_tier"] = {"qty": nt.min_qty, "unit_price": money(nt.unit_price), "amount": nt_amount}
    return out


_PACK_GALLONS = {"1 gal": Decimal(1), "5 gal": Decimal(5), "55 gal drum": Decimal(55)}


def target_per_container(req: dict, pack_size: str, table_price: Decimal) -> Decimal | None:
    """Convert a customer-stated price or discount to the per-container price it implies, when
    possible. Used only to compare against the floor; it is never applied to the quote."""
    amount = req.get("amount")
    if amount is None:
        return None
    amount = Decimal(str(amount))
    kind, per = req.get("kind"), req.get("per")
    if kind == "discount_percent" or per == "percent":
        if not Decimal(0) < amount < Decimal(100):
            return None
        return money(table_price * (Decimal(100) - amount) / Decimal(100))
    if kind == "discount_amount":
        if per in (None, "container"):
            off = amount
        elif per == "gal" and pack_size in _PACK_GALLONS:
            off = amount * _PACK_GALLONS[pack_size]
        else:
            return None                                  # per order / per lb: can't place on one line
        return money(table_price - off) if off < table_price else None
    if per in (None, "container"):
        return money(amount)
    if per == "gal" and pack_size in _PACK_GALLONS:
        return money(amount * _PACK_GALLONS[pack_size])
    return None
