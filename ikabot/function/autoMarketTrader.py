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
#  Setup Flow (Chunk 2 - stub)
# ============================================================

def autoMarketTrader(session, event, stdin_fd, predetermined_input):
    """Entry point for the auto market trader."""
    sys.stdin = os.fdopen(stdin_fd)
    config.predetermined_input = predetermined_input
    try:
        banner()
        print("Auto Market Trader - CSV-based order ledger")
        print("(Setup flow not yet implemented - Chunk 2)")
        enter()
        event.set()
        return
    except KeyboardInterrupt:
        event.set()
        return
