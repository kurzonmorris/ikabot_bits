#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import json
import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime

from ikabot import config
from ikabot.config import *
from ikabot.helpers.botComm import *
from ikabot.helpers.getJson import getCity
from ikabot.helpers.gui import *
from ikabot.helpers.market import *
from ikabot.helpers.naval import getAvailableShips
from ikabot.helpers.pedirInfo import read, getShipCapacity
from ikabot.helpers.process import set_child_mode
from ikabot.helpers.signals import setInfoSignal
from ikabot.helpers.varios import addThousandSeparator, wait, getDateTime
from ikabot.function.sellResources import getMarketInfo, chooseCommercialCity
from ikabot.function.buyResources import getOffers, buy


# ============================================================
#  Constants
# ============================================================

TRADE_BUY = "333"
TRADE_SELL = "444"
MAX_BUY_AMOUNT = 40000000

RESOURCE_PARAMS = [
    ("resource", "resourcePrice", "resourceTradeType"),
    ("tradegood1", "tradegood1Price", "tradegood1TradeType"),
    ("tradegood2", "tradegood2Price", "tradegood2TradeType"),
    ("tradegood3", "tradegood3Price", "tradegood3TradeType"),
    ("tradegood4", "tradegood4Price", "tradegood4TradeType"),
]

CSV_COLUMNS = [
    "order_id",
    "priority",
    "resource",
    "order_type",
    "mode",
    "strategy",
    "target_player",
    "price",
    "quantity_remaining",
    "quantity_fulfilled",
    "per_cycle",
    "city_id",
    "city_name",
    "undercutting",
    "recurring",
    "daily_budget",
    "daily_spent",
    "status",
    "last_activity",
    "notes",
]

PRICE_LOG_COLUMNS = [
    "timestamp",
    "resource",
    "lowest_sell",
    "highest_buy",
    "num_offers",
    "city_id",
]

VALID_RESOURCES = ["Wood", "Wine", "Marble", "Crystal", "Sulfur"]
VALID_ORDER_TYPES = ["buy", "sell"]
VALID_MODES = ["own_offer", "active"]
VALID_STRATEGIES = ["cheapest", "closest", "specific", ""]
VALID_STATUSES = ["pending", "active", "complete", "paused", "error"]


# ============================================================
#  CSV Helpers
# ============================================================

def get_csv_path(session):
    """Build CSV file path from session info.
    Returns path like: .ikabot_autotrader_s1-en.csv
    """
    server = "s{}-{}".format(session.mundo, session.servidor)
    return ".ikabot_autotrader_{}.csv".format(server)


def get_price_log_path(session):
    """Build price log CSV file path."""
    server = "s{}-{}".format(session.mundo, session.servidor)
    return ".ikabot_autotrader_prices_{}.csv".format(server)


def read_orders(csv_path):
    """Read orders from CSV file.
    Returns list of order dicts. Skips invalid rows gracefully.
    Returns empty list if file doesn't exist.
    """
    if not os.path.exists(csv_path):
        return []
    orders = []
    try:
        with open(csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if validate_order(row):
                    for field in ["order_id", "priority", "price", "quantity_remaining",
                                  "quantity_fulfilled", "per_cycle", "daily_budget", "daily_spent"]:
                        try:
                            row[field] = int(row[field]) if row[field] else 0
                        except (ValueError, KeyError):
                            row[field] = 0
                    orders.append(row)
    except Exception:
        pass
    return orders


def write_orders(csv_path, orders):
    """Write orders to CSV with backup and atomic replace."""
    backup = csv_path + ".bak"
    if os.path.exists(csv_path):
        shutil.copy2(csv_path, backup)
    tmp = csv_path + ".tmp"
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for order in orders:
            writer.writerow(order)
    os.replace(tmp, csv_path)


def next_order_id(orders):
    """Get next available order ID."""
    if not orders:
        return 1
    return max(int(o.get("order_id", 0)) for o in orders) + 1


def validate_order(order):
    """Check that an order row has the minimum required fields with valid values.
    Returns True if usable, False if it should be skipped.
    """
    if not order.get("resource") or order["resource"] not in VALID_RESOURCES:
        return False
    if not order.get("order_type") or order["order_type"] not in VALID_ORDER_TYPES:
        return False
    if not order.get("mode") or order["mode"] not in VALID_MODES:
        return False
    if order.get("strategy", "") not in VALID_STRATEGIES:
        return False
    if not order.get("status") or order["status"] not in VALID_STATUSES:
        return False
    try:
        remaining = int(order.get("quantity_remaining", 0))
        if remaining < 0:
            return False
    except (ValueError, TypeError):
        return False
    try:
        price = int(order.get("price", 0))
        if price <= 0:
            return False
    except (ValueError, TypeError):
        return False
    return True


def make_order(order_id, resource, order_type, mode, price, quantity,
               city_id, city_name, priority=2, strategy="", target_player="",
               per_cycle=0, undercutting="no", recurring="none",
               daily_budget=0):
    """Create a new order dict with all fields populated."""
    return {
        "order_id": order_id,
        "priority": priority,
        "resource": resource,
        "order_type": order_type,
        "mode": mode,
        "strategy": strategy,
        "target_player": target_player,
        "price": price,
        "quantity_remaining": quantity,
        "quantity_fulfilled": 0,
        "per_cycle": per_cycle,
        "city_id": city_id,
        "city_name": city_name,
        "undercutting": undercutting,
        "recurring": recurring,
        "daily_budget": daily_budget,
        "daily_spent": 0,
        "status": "pending",
        "last_activity": getDateTime(),
        "notes": "",
    }


def log_price(price_log_path, resource, lowest_sell, highest_buy, num_offers, city_id):
    """Append a price observation to the price log CSV."""
    file_exists = os.path.exists(price_log_path)
    with open(price_log_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PRICE_LOG_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": getDateTime(),
            "resource": resource,
            "lowest_sell": lowest_sell if lowest_sell is not None else "",
            "highest_buy": highest_buy if highest_buy is not None else "",
            "num_offers": num_offers,
            "city_id": city_id,
        })


def display_orders(orders):
    """Print a formatted table of orders."""
    if not orders:
        print("No orders found.")
        return
    print("{:<4} {:<3} {:<8} {:<4} {:<10} {:<8} {:<12} {:<12} {:<12} {:<7}".format(
        "ID", "Pri", "Resource", "Type", "Mode", "Strategy", "Remaining", "Fulfilled", "Price", "Status",
    ))
    print("-" * 95)
    for o in orders:
        print("{:<4} {:<3} {:<8} {:<4} {:<10} {:<8} {:<12} {:<12} {:<12} {:<7}".format(
            o["order_id"],
            o["priority"],
            str(o["resource"])[:7],
            str(o["order_type"])[:4],
            str(o["mode"])[:10],
            str(o.get("strategy", ""))[:8],
            addThousandSeparator(o["quantity_remaining"]),
            addThousandSeparator(o["quantity_fulfilled"]),
            addThousandSeparator(o["price"]),
            str(o["status"])[:7],
        ))
    print("")


def _refresh_city(session, city):
    """Re-fetch city data to get current resource levels."""
    html = session.get(city_url + city["id"])
    fresh = getCity(html)
    fresh["pos"] = city["pos"]
    fresh["rango"] = city["rango"]
    return fresh


# ============================================================
#  Setup Flow
# ============================================================

def _choose_city(commercial_cities):
    """City picker for auto trader."""
    print("Which city's Trading Post should manage orders?\n")
    for i, city in enumerate(commercial_cities):
        print("({:d}) {}".format(i + 1, city["name"]))
    ind = read(min=1, max=len(commercial_cities))
    return commercial_cities[ind - 1]


def _show_city_info(session, city):
    """Display city resources, gold, and action points."""
    gold, gold_prod = getGold(session, city)
    html = session.get(city_url + city["id"])
    ap = getActionPoints(html)

    print("City: {}".format(city["name"]))
    print("Gold: {} (production: {}/hr)".format(
        addThousandSeparator(gold), addThousandSeparator(gold_prod)))
    if ap >= 0:
        print("Action Points: {}".format(ap))
    print("")
    print("Resources:")
    for i in range(5):
        avail = city["availableResources"][i] if i < len(city.get("availableResources", [])) else 0
        free = city["freeSpaceForResources"][i] if i < len(city.get("freeSpaceForResources", [])) else 0
        print("  {}: {} available, {} free space".format(
            materials_names[i],
            addThousandSeparator(avail),
            addThousandSeparator(free),
        ))
    print("")


def _create_order(session, city, orders, commercial_cities):
    """Interactive order creation. Returns a new order dict or None if cancelled."""
    oid = next_order_id(orders)

    # If multiple cities, let user pick which one for this order
    if len(commercial_cities) > 1:
        print("Which Trading Post for this order?")
        for i, c in enumerate(commercial_cities):
            marker = " (current)" if c["id"] == city["id"] else ""
            print("({:d}) {}{}".format(i + 1, c["name"], marker))
        idx = read(min=1, max=len(commercial_cities))
        order_city = commercial_cities[idx - 1]
    else:
        order_city = city

    # Resource
    print("\nResource:")
    for i, name in enumerate(materials_names):
        print("({:d}) {}".format(i + 1, name))
    res_idx = read(min=1, max=5) - 1
    resource = materials_names[res_idx]

    # Buy or Sell
    print("\n(1) Buy {}  (2) Sell {}".format(resource, resource))
    order_type = "buy" if read(min=1, max=2) == 1 else "sell"

    # Mode
    print("\nTrading mode:")
    print("(1) Own offer - post on marketplace, wait for others")
    print("(2) Active trading - browse offers, buy/sell and ship")
    mode = "own_offer" if read(min=1, max=2) == 1 else "active"

    # Strategy (active mode only)
    strategy = ""
    target_player = ""
    if mode == "active":
        print("\nBuy strategy:" if order_type == "buy" else "\nSell strategy:")
        print("(1) Cheapest price")
        print("(2) Closest distance")
        print("(3) Specific player")
        strat_choice = read(min=1, max=3)
        strategy = ["cheapest", "closest", "specific"][strat_choice - 1]
        if strategy == "specific":
            target_player = read(msg="Player name: ")
            if not target_player.strip():
                print("No player name entered, cancelled.")
                return None

    # Price
    if mode == "own_offer":
        html = getMarketInfo(session, order_city)
        limits = getPriceLimits(html)
        lo, hi = limits[res_idx]
        print("\nPrice per unit [min={}, max={}]:".format(lo, hi))
        price = read(min=lo, max=hi)
    else:
        if order_type == "buy":
            print("\nMax price willing to pay per unit:")
        else:
            print("\nMin price willing to accept per unit:")
        price = read(min=1, max=999999)

    # Quantity
    print("\nTotal quantity to {}:".format(order_type))
    quantity = read(min=1, max=999999999)

    # Per-cycle limit
    print("\nMax amount per cycle (0 = no limit) [default 0]:")
    per_cycle_input = read(min=0, max=quantity, empty=True)
    per_cycle = int(per_cycle_input) if per_cycle_input != "" else 0

    # Priority
    print("\nPriority:")
    print("(1) High  (2) Medium  (3) Low")
    priority = read(min=1, max=3)

    # Undercutting (own_offer mode)
    undercutting = "no"
    if mode == "own_offer":
        print("\nAuto-undercut/outbid each cycle? [y/N]")
        uc = read(values=["y", "Y", "n", "N", ""])
        if uc.lower() == "y":
            undercutting = "yes"

    # Recurring
    recurring = "none"
    daily_budget = 0
    print("\nWhen order completes:")
    print("(0) Stop - mark as complete")
    print("(1) Re-order same amount")
    print("(2) Re-order with daily gold budget")
    rec_choice = read(min=0, max=2)
    if rec_choice == 1:
        recurring = "amount:{}".format(quantity)
    elif rec_choice == 2:
        print("Daily gold budget:")
        daily_budget = read(min=1, max=999999999)
        recurring = "budget:{}".format(daily_budget)

    order = make_order(
        order_id=oid,
        resource=resource,
        order_type=order_type,
        mode=mode,
        price=price,
        quantity=quantity,
        city_id=str(order_city["id"]),
        city_name=order_city["name"],
        priority=priority,
        strategy=strategy,
        target_player=target_player.strip(),
        per_cycle=per_cycle,
        undercutting=undercutting,
        recurring=recurring,
        daily_budget=daily_budget,
    )

    # Confirm
    print("\n--- Order Summary ---")
    action = "BUY" if order_type == "buy" else "SELL"
    print("  {} {} {} @ {} per unit".format(
        action, addThousandSeparator(quantity), resource, price))
    print("  Mode: {} | Strategy: {}".format(mode, strategy or "n/a"))
    if target_player:
        print("  Target player: {}".format(target_player))
    print("  Priority: {} | Per-cycle: {} | City: {}".format(
        priority, addThousandSeparator(per_cycle) if per_cycle else "unlimited", order_city["name"]))
    if recurring != "none":
        print("  Recurring: {}".format(recurring))
    print("\nAdd this order? [Y/n]")
    rta = read(values=["y", "Y", "n", "N", ""])
    if rta.lower() == "n":
        return None

    return order


def autoMarketTrader(session, event, stdin_fd, predetermined_input):
    """Entry point for the auto market trader."""
    sys.stdin = os.fdopen(stdin_fd)
    config.predetermined_input = predetermined_input
    try:
        banner()

        # Find cities with trading posts
        commercial_cities = getCommercialCities(session)
        if not commercial_cities:
            print("No city has a Trading Post built.")
            enter()
            event.set()
            return

        # Select primary city
        if len(commercial_cities) == 1:
            city = commercial_cities[0]
        else:
            city = _choose_city(commercial_cities)
        banner()

        # Refresh city data
        city = _refresh_city(session, city)

        # Check for existing CSV
        csv_path = get_csv_path(session)
        existing_orders = read_orders(csv_path)

        if existing_orders:
            print("Existing orders found:\n")
            display_orders(existing_orders)
            print("What would you like to do?")
            print("(1) Add new orders alongside existing")
            print("(2) Clear all orders and start fresh")
            print("(3) Run with current orders (skip setup)")
            choice = read(min=1, max=3)

            if choice == 2:
                existing_orders = []
                print("Orders cleared.\n")
            elif choice == 3:
                # Skip straight to settings + launch
                banner()
                print("Launching with existing {} orders.\n".format(len(existing_orders)))
                display_orders(existing_orders)

                print("Check interval in minutes [15-240, default 60]:")
                interval_input = read(min=15, max=240, empty=True)
                interval = int(interval_input) if interval_input != "" else 60

                print("\nDaily summary notifications? [y/N]")
                ds = read(values=["y", "Y", "n", "N", ""])
                daily_summary = ds.lower() == "y"

                print("Price tracking? [y/N]")
                pt = read(values=["y", "Y", "n", "N", ""])
                price_tracking = pt.lower() == "y"

                print("\nProceed? [Y/n]")
                rta = read(values=["y", "Y", "n", "N", ""])
                if rta.lower() == "n":
                    event.set()
                    return

                _launch_background(session, event, csv_path, city, existing_orders,
                                   interval, daily_summary, price_tracking)
                return
            banner()

        # Show city info
        _show_city_info(session, city)

        # Marketplace info
        html = getMarketInfo(session, city)
        storage_cap = storageCapacityOfMarket(html)
        print("Trading Post storage capacity: {}\n".format(addThousandSeparator(storage_cap)))

        # Order creation loop
        orders = list(existing_orders)
        while True:
            print("--- Create Order #{} ---".format(next_order_id(orders)))
            new_order = _create_order(session, city, orders, commercial_cities)
            if new_order:
                orders.append(new_order)
                print("\nOrder #{} added.\n".format(new_order["order_id"]))

            if not any(o["status"] in ("pending", "active") for o in orders):
                print("No active orders yet. Add at least one order.")
                continue

            print("Add another order? [y/N]")
            more = read(values=["y", "Y", "n", "N", ""])
            if more.lower() != "y":
                break
            banner()

        # Settings
        banner()
        print("=== Settings ===\n")

        print("Check interval in minutes [15-240, default 60]:")
        interval_input = read(min=15, max=240, empty=True)
        interval = int(interval_input) if interval_input != "" else 60

        print("\nDaily summary notifications? [y/N]")
        ds = read(values=["y", "Y", "n", "N", ""])
        daily_summary = ds.lower() == "y"

        print("Price tracking (log prices each cycle)? [y/N]")
        pt = read(values=["y", "Y", "n", "N", ""])
        price_tracking = pt.lower() == "y"

        # Final summary
        banner()
        print("=== Auto Market Trader Summary ===\n")
        print("City: {} | Interval: {} min".format(city["name"], interval))
        print("Daily summary: {} | Price tracking: {}".format(
            "ON" if daily_summary else "OFF",
            "ON" if price_tracking else "OFF"))
        print("")
        display_orders(orders)

        print("Proceed? [Y/n]")
        rta = read(values=["y", "Y", "n", "N", ""])
        if rta.lower() == "n":
            event.set()
            return

        # Write CSV and launch
        write_orders(csv_path, orders)

        _launch_background(session, event, csv_path, city, orders,
                           interval, daily_summary, price_tracking)

    except KeyboardInterrupt:
        event.set()
        return


def _launch_background(session, event, csv_path, city, orders, interval,
                       daily_summary, price_tracking):
    """Fork background process and start the trading loop."""
    set_child_mode(session)
    event.set()

    info = "Auto Market Trader\n"
    info += "City: {} | Interval: {} min\n".format(city["name"], interval)
    info += "Orders: {}\n".format(len([o for o in orders if o["status"] in ("pending", "active")]))
    for o in orders:
        if o["status"] in ("pending", "active"):
            action = "BUY" if o["order_type"] == "buy" else "SELL"
            info += "  #{} {} {} {} @ {}\n".format(
                o["order_id"], action, addThousandSeparator(o["quantity_remaining"]),
                o["resource"], o["price"])
    setInfoSignal(session, info)

    settings = {
        "daily_summary": daily_summary,
        "price_tracking": price_tracking,
    }

    try:
        run_auto_trader(session, csv_path, interval, settings)
    except Exception:
        msg = "Error in Auto Market Trader:\n{}\nCause:\n{}".format(info, traceback.format_exc())
        sendToBot(session, msg)
    finally:
        session.logout()


# ============================================================
#  Background Loop + Own Offer Processing
# ============================================================

def _resource_index(resource_name):
    """Convert resource name to index (0-4)."""
    try:
        return VALID_RESOURCES.index(resource_name)
    except ValueError:
        return -1


def _build_own_offer_payload(city, resource_configs, existing_amounts, existing_prices, existing_types):
    """Build updateOffers payload. Preserves non-managed resources.
    resource_configs: dict mapping resource_index -> {type, amount, price}
    """
    payload = {
        "cityId": city["id"],
        "position": city["pos"],
        "action": "CityScreen",
        "function": "updateOffers",
        "backgroundView": "city",
        "currentCityId": city["id"],
        "templateView": "branchOfficeOwnOffers",
        "currentTab": "tab_branchOfficeOwnOffers",
        "actionRequest": actionRequest,
        "ajax": "1",
    }
    for i, (amt_key, price_key, type_key) in enumerate(RESOURCE_PARAMS):
        if i in resource_configs:
            cfg = resource_configs[i]
            payload[type_key] = cfg["type"]
            payload[amt_key] = str(cfg["amount"])
            payload[price_key] = str(cfg["price"])
        else:
            payload[type_key] = existing_types[i] if i < len(existing_types) else TRADE_SELL
            payload[amt_key] = str(existing_amounts[i])
            payload[price_key] = str(existing_prices[i])
    return payload


def process_own_offers(session, city, orders):
    """Process all own_offer orders for a city.
    Detects fulfilled trades, updates CSV orders, posts new offers.
    Returns (updated_orders, notifications).
    """
    notifications = []

    # Get current marketplace state
    html = getMarketInfo(session, city)
    current_amounts = onSellInMarket(html)
    current_prices = getOwnOfferPrices(html)
    current_types = getOwnOfferTradeTypes(html)
    storage_cap = storageCapacityOfMarket(html)
    price_limits = getPriceLimits(html)

    # Refresh city data
    city = _refresh_city(session, city)

    # Group own_offer orders by resource
    # Only one marketplace slot per resource, so if multiple orders exist for same resource,
    # we process the highest priority one first
    orders_by_resource = {}
    for o in orders:
        if o["mode"] != "own_offer" or o["status"] not in ("pending", "active"):
            continue
        idx = _resource_index(o["resource"])
        if idx < 0:
            continue
        if idx not in orders_by_resource:
            orders_by_resource[idx] = []
        orders_by_resource[idx].append(o)

    # Sort each resource's orders by priority then order_id
    for idx in orders_by_resource:
        orders_by_resource[idx].sort(key=lambda o: (o["priority"], o["order_id"]))

    # Track what we expect on marketplace vs what's there now
    # to detect fulfilled trades
    resource_configs = {}
    gold, _ = getGold(session, city)

    for idx, res_orders in orders_by_resource.items():
        if not res_orders:
            continue

        # The active order for this slot (highest priority)
        active_order = res_orders[0]
        active_order["status"] = "active"

        # Detect fulfillment: if current amount is less than what we posted last time
        # We track this via the notes field storing last posted amount
        last_posted = _get_last_posted(active_order)
        current = current_amounts[idx]
        if last_posted > 0 and current < last_posted:
            fulfilled = last_posted - current
            active_order["quantity_fulfilled"] += fulfilled
            active_order["quantity_remaining"] = max(0, active_order["quantity_remaining"] - fulfilled)
            active_order["last_activity"] = getDateTime()

            if active_order["order_type"] == "sell":
                gold_earned = fulfilled * active_order["price"]
                notifications.append("Sold {} {} @ {} = {} gold".format(
                    addThousandSeparator(fulfilled), active_order["resource"],
                    active_order["price"], addThousandSeparator(gold_earned)))
            else:
                gold_spent = fulfilled * active_order["price"]
                notifications.append("Bought {} {} @ {} = {} gold".format(
                    addThousandSeparator(fulfilled), active_order["resource"],
                    active_order["price"], addThousandSeparator(gold_spent)))

            # Check if complete
            if active_order["quantity_remaining"] <= 0:
                active_order["status"] = "complete"
                notifications.append("ORDER #{} COMPLETE: {} {}".format(
                    active_order["order_id"], active_order["order_type"].upper(),
                    active_order["resource"]))
                continue

        # Skip if order is now complete
        if active_order["quantity_remaining"] <= 0:
            active_order["status"] = "complete"
            continue

        # Handle undercutting
        price = active_order["price"]
        if active_order.get("undercutting") == "yes" and idx < len(price_limits):
            lo, hi = price_limits[idx]
            if active_order["order_type"] == "sell":
                lowest = scanMarketPrices(session, city, idx, "444")
                if lowest is not None and lowest > lo:
                    price = max(lo, lowest - 1)
            else:
                highest = scanMarketPrices(session, city, idx, "333")
                if highest is not None and highest < hi:
                    price = min(hi, highest + 1)
            active_order["price"] = price

        # Calculate amount to post
        desired = active_order["quantity_remaining"]
        if active_order["per_cycle"] > 0:
            desired = min(desired, active_order["per_cycle"])

        if active_order["order_type"] == "sell":
            # Limit by city available resources
            city_avail = city["availableResources"][idx] if idx < len(city.get("availableResources", [])) else 0
            desired = min(desired, city_avail)
            # Limit by storage capacity (account for other resources in storage)
            other_storage = sum(current_amounts[j] for j in range(5) if j != idx and j not in resource_configs)
            managed_storage = sum(resource_configs[j]["amount"] for j in resource_configs if resource_configs[j]["type"] == TRADE_SELL)
            available_storage = max(0, storage_cap - other_storage - managed_storage)
            desired = min(desired, available_storage)
            trade_type = TRADE_SELL
        else:
            # Buy order: limit by gold and max buy
            desired = min(desired, MAX_BUY_AMOUNT)
            if price > 0:
                affordable = gold // price
                desired = min(desired, affordable)
                gold -= desired * price
            # Limit by warehouse space
            free_space = city["freeSpaceForResources"][idx] if idx < len(city.get("freeSpaceForResources", [])) else 0
            desired = min(desired, free_space)
            trade_type = TRADE_BUY

        amount = max(0, desired)
        resource_configs[idx] = {"type": trade_type, "amount": amount, "price": price}
        _set_last_posted(active_order, amount)

    # Build and send payload if we have any managed resources
    if resource_configs:
        payload = _build_own_offer_payload(city, resource_configs, current_amounts, current_prices, current_types)
        session.post(params=payload)

    return orders, notifications


def _get_last_posted(order):
    """Extract last posted amount from order notes."""
    notes = str(order.get("notes", ""))
    match = re.search(r"last_posted:(\d+)", notes)
    if match:
        return int(match.group(1))
    return 0


def _set_last_posted(order, amount):
    """Store last posted amount in order notes."""
    notes = str(order.get("notes", ""))
    notes = re.sub(r"last_posted:\d+", "", notes).strip()
    if notes and not notes.endswith(";"):
        notes += ";"
    notes += "last_posted:{}".format(amount)
    order["notes"] = notes


def _update_status(session, orders):
    """Update process status line with order summary."""
    active = [o for o in orders if o["status"] in ("pending", "active")]
    if not active:
        session.setStatus("AutoTrader: idle | {}".format(getDateTime()))
        return
    parts = []
    for o in active:
        action = "B" if o["order_type"] == "buy" else "S"
        res = o["resource"][:3]
        remaining = addThousandSeparator(o["quantity_remaining"])
        parts.append("#{}{}{} {}".format(o["order_id"], action, res, remaining))
    status = "AutoTrader: {} | {}".format(" ".join(parts[:5]), getDateTime())
    session.setStatus(status)


def run_auto_trader(session, csv_path, interval_minutes, settings):
    """Background loop that manages marketplace orders.
    Re-reads CSV each cycle for hot-reload support.
    """
    # Build city lookup from commercial cities
    commercial_cities = getCommercialCities(session)
    city_lookup = {str(c["id"]): c for c in commercial_cities}

    # Startup notification
    orders = read_orders(csv_path)
    active_count = len([o for o in orders if o["status"] in ("pending", "active")])
    msg = "Auto Trader started: {} active orders, interval {} min".format(
        active_count, interval_minutes)
    sendToBot(session, msg)

    _update_status(session, orders)

    while True:
        wait(interval_minutes * 60, maxrandom=60)

        # Hot-reload CSV
        orders = read_orders(csv_path)
        if not any(o["status"] in ("pending", "active") for o in orders):
            sendToBot(session, "Auto Trader: all orders complete or paused.")
            return

        # Activate pending orders
        for o in orders:
            if o["status"] == "pending":
                o["status"] = "active"

        # Sort by priority then order_id
        orders.sort(key=lambda o: (o.get("priority", 2), o.get("order_id", 0)))

        # Check action points
        any_html = session.get(city_url + str(commercial_cities[0]["id"]))
        action_points = getActionPoints(any_html)

        # Group orders by city
        city_groups = {}
        for o in orders:
            cid = str(o.get("city_id", ""))
            if cid not in city_groups:
                city_groups[cid] = []
            city_groups[cid].append(o)

        all_notifications = []

        # Process each city's orders
        for cid, city_orders in city_groups.items():
            city = city_lookup.get(cid)
            if not city:
                continue

            city = _refresh_city(session, city)
            city_lookup[cid] = city

            # Process own_offer orders
            own_offer_orders = [o for o in city_orders if o["mode"] == "own_offer" and o["status"] == "active"]
            if own_offer_orders:
                _, notifs = process_own_offers(session, city, own_offer_orders)
                all_notifications.extend(notifs)

            # Process active trading orders (Chunk 4)
            active_orders = [o for o in city_orders if o["mode"] == "active" and o["status"] == "active"]
            if active_orders and action_points != 0:
                notifs = process_active_orders(session, city, active_orders)
                all_notifications.extend(notifs)
            elif active_orders and action_points == 0:
                all_notifications.append("Skipped active orders - 0 action points")

        # Send notifications
        if all_notifications:
            msg = "Auto Trader:\n" + "\n".join("  " + n for n in all_notifications)
            sendToBot(session, msg)

        # Write updated CSV
        write_orders(csv_path, orders)

        # Update status
        _update_status(session, orders)


# ============================================================
#  Active Trading + Smart Ship Batching
# ============================================================

def _set_resource_filter(session, city, resource_index):
    """Set marketplace to display offers for a specific resource.
    Must be called before getOffers() to get correct resource offers.
    """
    search_resource = "resource" if resource_index == 0 else str(resource_index)
    data = {
        "cityId": city["id"],
        "position": city["pos"],
        "view": "branchOffice",
        "activeTab": "bargain",
        "type": 444,
        "searchResource": search_resource,
        "range": city["rango"],
        "backgroundView": "city",
        "currentCityId": city["id"],
        "templateView": "branchOffice",
        "currentTab": "bargain",
        "actionRequest": actionRequest,
        "ajax": 1,
    }
    session.post(params=data)


def allocate_ships(orders, total_ships, ship_capacity):
    """Allocate ships across orders by priority, then proportionally.
    Returns dict mapping order_id -> allocated_ships.
    """
    if total_ships <= 0 or not orders:
        return {}

    allocation = {}
    remaining_ships = total_ships

    # Group by priority
    priority_groups = {}
    for o in orders:
        p = o.get("priority", 2)
        if p not in priority_groups:
            priority_groups[p] = []
        priority_groups[p].append(o)

    for priority in sorted(priority_groups.keys()):
        if remaining_ships <= 0:
            break
        group = priority_groups[priority]

        # Calculate how many ships each order ideally wants
        wants = {}
        total_want = 0
        for o in group:
            qty = min(o["quantity_remaining"], o["per_cycle"] if o["per_cycle"] > 0 else o["quantity_remaining"])
            ships_needed = max(1, -(-qty // ship_capacity))  # ceil division
            wants[o["order_id"]] = ships_needed
            total_want += ships_needed

        if total_want <= remaining_ships:
            # Enough ships for everyone in this priority
            for o in group:
                alloc = wants[o["order_id"]]
                allocation[o["order_id"]] = alloc
                remaining_ships -= alloc
        else:
            # Proportional split within this priority
            for o in group:
                if remaining_ships <= 0:
                    break
                proportion = wants[o["order_id"]] / total_want if total_want > 0 else 0
                alloc = max(1, int(remaining_ships * proportion))
                alloc = min(alloc, remaining_ships, wants[o["order_id"]])
                allocation[o["order_id"]] = alloc
                remaining_ships -= alloc

    return allocation


def process_active_orders(session, city, orders):
    """Process active buy/sell orders with smart ship batching.
    Returns list of notification strings.
    """
    notifications = []

    # Get available ships and capacity
    ships_available = getAvailableShips(session)
    if ships_available <= 0:
        return []

    ship_capacity, _ = getShipCapacity(session)
    if ship_capacity <= 0:
        return []

    # Separate buy and sell orders
    buy_orders = [o for o in orders if o["order_type"] == "buy" and o["quantity_remaining"] > 0]
    sell_orders = [o for o in orders if o["order_type"] == "sell" and o["quantity_remaining"] > 0]

    # Sort by priority
    buy_orders.sort(key=lambda o: (o["priority"], o["order_id"]))

    # Allocate ships across buy orders
    ship_allocation = allocate_ships(buy_orders, ships_available, ship_capacity)

    # Get gold once
    gold, _ = getGold(session, city)

    # Process buy orders
    for order in buy_orders:
        alloc_ships = ship_allocation.get(order["order_id"], 0)
        if alloc_ships <= 0:
            continue

        # Check daily budget
        if order["daily_budget"] > 0 and order["daily_spent"] >= order["daily_budget"]:
            continue

        res_idx = _resource_index(order["resource"])
        if res_idx < 0:
            continue

        # Set marketplace filter and get offers
        _set_resource_filter(session, city, res_idx)
        offers = getOffers(session, city)
        if not offers:
            continue

        # Apply strategy filter
        if order["strategy"] == "specific" and order.get("target_player"):
            offers = filter_offers_by_player(offers, order["target_player"])
        elif order["strategy"] == "closest":
            offers = sort_offers_by_distance(offers)

        # Filter by max price
        offers = filter_offers_by_max_price(offers, order["price"])

        if not offers:
            continue

        # Calculate how much we can buy this cycle
        capacity = alloc_ships * ship_capacity
        desired = min(order["quantity_remaining"], capacity)
        if order["per_cycle"] > 0:
            desired = min(desired, order["per_cycle"])

        # Check gold
        if order["price"] > 0:
            affordable = gold // order["price"]
            desired = min(desired, affordable)

        # Check daily budget remaining
        if order["daily_budget"] > 0:
            budget_remaining = order["daily_budget"] - order["daily_spent"]
            if order["price"] > 0:
                budget_affordable = budget_remaining // order["price"]
                desired = min(desired, budget_affordable)

        # Check warehouse space
        city = _refresh_city(session, city)
        free_space = city["freeSpaceForResources"][res_idx] if res_idx < len(city.get("freeSpaceForResources", [])) else 0
        desired = min(desired, free_space)

        if desired <= 0:
            continue

        # Buy from offers (process best offer first)
        amount_bought = 0
        for offer in offers:
            if desired <= 0:
                break
            if offer["amountAvailable"] <= 0:
                continue

            # Verify offer price is still acceptable
            if offer["precio"] > order["price"]:
                continue

            buy_amount = min(desired, offer["amountAvailable"])

            try:
                buy(session, city, offer, buy_amount, alloc_ships, ship_capacity)
                amount_bought += buy_amount
                desired -= buy_amount
                cost = buy_amount * offer["precio"]
                gold -= cost
                order["daily_spent"] += cost
                break  # One purchase per cycle per order to avoid overwhelming ships
            except Exception:
                order["notes"] = str(order.get("notes", "")) + ";buy_error:{}".format(getDateTime())
                break

        if amount_bought > 0:
            order["quantity_remaining"] -= amount_bought
            order["quantity_fulfilled"] += amount_bought
            order["last_activity"] = getDateTime()
            notifications.append("Bought {} {} @ {} from {}".format(
                addThousandSeparator(amount_bought), order["resource"],
                offer["precio"], offer.get("jugadorAComprar", "?")))

            if order["quantity_remaining"] <= 0:
                order["status"] = "complete"
                notifications.append("ORDER #{} COMPLETE: BUY {}".format(
                    order["order_id"], order["resource"]))

    # Process sell orders (browse buy offers from other players)
    for order in sell_orders:
        res_idx = _resource_index(order["resource"])
        if res_idx < 0:
            continue

        # Check city has resources to sell
        city = _refresh_city(session, city)
        city_avail = city["availableResources"][res_idx] if res_idx < len(city.get("availableResources", [])) else 0
        if city_avail <= 0:
            continue

        # For active selling, we browse buy offers (type 333) and sell to the highest bidder
        # This uses the same marketplace scan mechanism
        search_resource = "resource" if res_idx == 0 else str(res_idx)
        data = {
            "cityId": city["id"],
            "position": city["pos"],
            "view": "branchOffice",
            "activeTab": "bargain",
            "type": 333,
            "searchResource": search_resource,
            "range": city["rango"],
            "backgroundView": "city",
            "currentCityId": city["id"],
            "templateView": "branchOffice",
            "currentTab": "bargain",
            "actionRequest": actionRequest,
            "ajax": "1",
        }
        resp = session.post(params=data)
        try:
            html = json.loads(resp, strict=False)[1][1][1]
        except (json.JSONDecodeError, IndexError, KeyError):
            continue

        # Parse buy offers (people wanting to buy = we can sell to them)
        # Buy offers show: player wanting to buy, amount they want, price they'll pay
        buy_offer_prices = re.findall(r'white-space:nowrap;">(\d+)\s', html)
        if not buy_offer_prices:
            continue

        # Check if best buy offer meets our minimum price
        best_price = max(int(p) for p in buy_offer_prices)
        if best_price < order["price"]:
            continue

        # Active selling to buy offers would require different API calls
        # than what's currently available. For now, log that opportunity exists.
        order["notes"] = str(order.get("notes", "")) + ";sell_opportunity:{}@{}".format(
            order["resource"], best_price)

    return notifications
