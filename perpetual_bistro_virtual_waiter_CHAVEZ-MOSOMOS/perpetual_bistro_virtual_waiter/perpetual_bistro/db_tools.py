"""SQLite tool functions the LLM can call. All return JSON-serialisable dicts."""
import difflib, json, os, sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "restaurant.db")
SERVICE_RATE = 0.10


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def conn():
    c = sqlite3.connect(DB_PATH, factory=_ClosingConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db(reset=False):
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    with conn() as c, open(os.path.join(HERE, "init_restaurant_db.sql"), encoding="utf-8") as f:
        c.executescript(f.read())
        columns = {row["name"] for row in c.execute("PRAGMA table_info(menu_items)")}
        if "dietary_tags" not in columns:
            c.execute("ALTER TABLE menu_items ADD COLUMN dietary_tags TEXT NOT NULL DEFAULT ''")
        c.execute("UPDATE menu_items SET dietary_tags='vegetarian' "
                  "WHERE id IN (3,8,9,10) AND dietary_tags=''")


def save_kiosk_state(data):
    with conn() as c:
        c.execute("INSERT INTO kiosk_state(id,data) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                  (json.dumps(data),))


def load_kiosk_state():
    with conn() as c:
        r = c.execute("SELECT data FROM kiosk_state WHERE id=1").fetchone()
    return json.loads(r[0]) if r else None


def session_exists(session_id):
    with conn() as c:
        return c.execute("SELECT 1 FROM table_sessions WHERE session_id=?", (session_id,)).fetchone() is not None


def create_session(table_no):
    with conn() as c:
        return c.execute("INSERT INTO table_sessions(table_no) VALUES (?)", (int(table_no),)).lastrowid


def _find_item(c, name):
    """Return (row, suggestions). Never invents items: unknown names return suggestions only."""
    rows = c.execute("SELECT * FROM menu_items").fetchall()
    n = (name or "").strip().lower()
    for r in rows:
        if r["name"].lower() == n:
            return r, []
    subs = [r for r in rows if n and (n in r["name"].lower() or r["name"].lower() in n)]
    if len(subs) == 1:
        return subs[0], []
    names = [r["name"] for r in rows]
    close = difflib.get_close_matches(name or "", names, n=1, cutoff=0.85)
    if close:
        return next(r for r in rows if r["name"] == close[0]), []
    sugg = [r["name"] for r in subs] or difflib.get_close_matches(name or "", names, n=3, cutoff=0.5)
    return None, sugg


def _in_cart(c, session_id, item_id):
    return c.execute("SELECT COALESCE(SUM(quantity),0) FROM order_line_items WHERE session_id=? AND item_id=?",
                     (session_id, item_id)).fetchone()[0]


def _components(c, item_id, line_id=None):
    rows = c.execute("SELECT component_item_id,quantity FROM combo_components WHERE combo_item_id=?",
                     (item_id,)).fetchall()
    components = {r["component_item_id"]: r["quantity"] for r in rows} or {item_id: 1}
    if line_id is not None:
        choice = c.execute("SELECT side_item_id,drink_item_id FROM order_line_combo_choices WHERE line_id=?",
                           (line_id,)).fetchone()
        if choice:
            for category, selected_id in (("Sides", choice["side_item_id"]), ("Drinks", choice["drink_item_id"])):
                for component_id in list(components):
                    component = c.execute("SELECT category FROM menu_items WHERE id=?", (component_id,)).fetchone()
                    if component and component["category"] == category:
                        del components[component_id]
                components[selected_id] = components.get(selected_id, 0) + 1
    return list(components.items())


def _component_price(c, item_id, line_id=None):
    price = c.execute("SELECT price FROM menu_items WHERE id=?", (item_id,)).fetchone()[0]
    if line_id is None:
        return price
    choice = c.execute("SELECT side_item_id,drink_item_id FROM order_line_combo_choices WHERE line_id=?",
                       (line_id,)).fetchone()
    if not choice:
        return price
    base_components = _components(c, item_id)
    adjustments = 0
    for category, selected_id in (("Sides", choice["side_item_id"]), ("Drinks", choice["drink_item_id"])):
        base_id = next((component_id for component_id, _ in base_components
                        if c.execute("SELECT category FROM menu_items WHERE id=?", (component_id,)).fetchone()[0]
                        == category), None)
        if base_id is not None:
            adjustments += c.execute("SELECT price FROM menu_items WHERE id=?", (selected_id,)).fetchone()[0]
            adjustments -= c.execute("SELECT price FROM menu_items WHERE id=?", (base_id,)).fetchone()[0]
    return round(price + adjustments, 2)


def _dietary_tags(c, item_id):
    components = _components(c, item_id)
    tags = None
    for component_id, _ in components:
        row = c.execute("SELECT dietary_tags FROM menu_items WHERE id=?", (component_id,)).fetchone()
        item_tags = {tag.strip().casefold() for tag in row["dietary_tags"].split(",") if tag.strip()}
        tags = item_tags if tags is None else tags & item_tags
    return sorted(tags or ())


def _allergens(c, item_id, line_id=None):
    allergens = set()
    for component_id, _ in _components(c, item_id, line_id):
        row = c.execute("SELECT allergens FROM menu_items WHERE id=?", (component_id,)).fetchone()
        allergens.update(tag.strip().casefold() for tag in row["allergens"].split(",") if tag.strip())
    return ",".join(sorted(allergens)) or "none listed"


def _reserved_components(c, exclude_session=None, exclude_line=None):
    query = """SELECT l.line_id,l.session_id,l.item_id,l.quantity FROM order_line_items l
               JOIN table_sessions s ON s.session_id=l.session_id WHERE s.status='open'"""
    params = []
    if exclude_session is not None:
        query += " AND l.session_id != ?"
        params.append(exclude_session)
    if exclude_line is not None:
        query += " AND l.line_id != ?"
        params.append(exclude_line)
    reserved = {}
    for line in c.execute(query, params):
        for component_id, component_qty in _components(c, line["item_id"], line["line_id"]):
            reserved[component_id] = reserved.get(component_id, 0) + line["quantity"] * component_qty
    return reserved


def _available_qty(c, item_id, exclude_session=None, exclude_line=None, components=None):
    reserved = _reserved_components(c, exclude_session, exclude_line)
    available = []
    candidates = components if components is not None else _components(c, item_id)
    for component_id, component_qty in candidates:
        stock = c.execute("SELECT stock_qty FROM menu_items WHERE id=?", (component_id,)).fetchone()[0]
        available.append(max(stock - reserved.get(component_id, 0), 0) // component_qty)
    return min(available, default=0)


# ---------------------------------------------------------------- LLM tools
def get_menu_by_category(category="all"):
    with conn() as c:
        cats = [r[0] for r in c.execute(
            "SELECT category FROM menu_items GROUP BY category ORDER BY CASE WHEN category='Combos' THEN 0 ELSE 1 END,id"
        )]
        cat = next((x for x in cats if x.lower() == (category or "all").strip().lower()), None)
        if (category or "all").lower() != "all" and not cat:
            return {"ok": False, "error": f"Unknown category '{category}'.", "categories": cats}
        q = "SELECT id,name,category,price,stock_qty,allergens FROM menu_items"
        if cat:
            q += " WHERE category=?"
        rows = c.execute(q, (cat,) if cat else ()).fetchall()
        items = []
        for row in rows:
            components = _components(c, row["id"])
            is_combo = components != [(row["id"], 1)]
            available_qty = _available_qty(c, row["id"])
            included = []
            individual_price = 0
            for component_id, component_qty in components:
                component = c.execute("SELECT name,price FROM menu_items WHERE id=?",
                                      (component_id,)).fetchone()
                included.extend([component["name"]] * component_qty)
                individual_price += component["price"] * component_qty
            items.append(dict(row, is_combo=is_combo, dietary_tags=_dietary_tags(c, row["id"]),
                              allergens=_allergens(c, row["id"]),
                              available_qty=available_qty,
                              available=available_qty > 0, included=included,
                              savings=round(individual_price - row["price"], 2) if is_combo else 0))
        if cat and cat.lower() != "combos":
            items = [item for item in items if not item["is_combo"]]
        return {"ok": True, "categories": cats, "items": items}


def get_menu_by_dietary_filter(diet, available_only=True):
    requested = (diet or "").strip().casefold()
    if requested in ("all", ""):
        result = get_menu_by_category()
        items = [item for item in result["items"] if not available_only or item["available"]]
        return {"ok": True, "diet": "all", "allergen": "", "items": items, "message": ""}
    allergen = ""
    if requested.startswith("allergen-free:"):
        allergen = requested.split(":", 1)[1]
    elif requested == "dairy-free":
        allergen = "dairy"
    if requested not in ("vegetarian", "vegan", "dairy-free") and not requested.startswith("allergen-free:"):
        return {"ok": False, "error": "Choose vegetarian, vegan, or a supported allergen-free filter."}
    if requested.startswith("allergen-free:") and not allergen:
        return {"ok": False, "error": "Specify an allergen to exclude."}
    result = get_menu_by_category()
    if allergen:
        items = []
        for item in result["items"]:
            if available_only and not item["available"]:
                continue
            if item["is_combo"]:
                options = get_combo_options(item["id"])
                main_allergens = {value.strip().casefold() for value in options["main_allergens"].split(",")}
                side_options = [
                    option for option in options["sides"]
                    if (not available_only or option["available_qty"] > 0)
                    and allergen not in {value.strip().casefold() for value in option["allergens"].split(",")}
                ]
                drink_options = [
                    option for option in options["drinks"]
                    if (not available_only or option["available_qty"] > 0)
                    and allergen not in {value.strip().casefold() for value in option["allergens"].split(",")}
                ]
                if allergen not in main_allergens and side_options and drink_options:
                    items.append(item)
            elif allergen not in {
                value.strip().casefold() for value in item["allergens"].split(",")
            }:
                items.append(item)
    else:
        items = [item for item in result["items"]
                 if requested in item["dietary_tags"] and (not available_only or item["available"])]
    return {"ok": True, "diet": requested, "allergen": allergen, "items": items,
            "message": f"No available items match the {requested} filter." if not items else ""}


def get_combo_options(combo_id):
    with conn() as c:
        combo = c.execute("SELECT id,name FROM menu_items WHERE id=? AND category='Combos'",
                          (int(combo_id),)).fetchone()
        if not combo:
            return {"ok": False, "error": "Meal combo not found."}
        defaults = {}
        main_id = None
        for item_id, _ in _components(c, combo["id"]):
            category = c.execute("SELECT category FROM menu_items WHERE id=?",
                                 (item_id,)).fetchone()["category"]
            if category in ("Sides", "Drinks"):
                defaults[category] = item_id
            elif category == "Mains":
                main_id = item_id
        options = {}
        for category, key in (("Sides", "sides"), ("Drinks", "drinks")):
            rows = c.execute("SELECT id,name,price,allergens FROM menu_items WHERE category=? ORDER BY name",
                             (category,)).fetchall()
            options[key] = []
            for row in rows:
                options[key].append(dict(
                    row, available_qty=_available_qty(c, row["id"]),
                    price_adjustment=round(
                        row["price"] - c.execute("SELECT price FROM menu_items WHERE id=?",
                                                  (defaults.get(category, row["id"]),)).fetchone()[0], 2
                    ),
                ))
        main_allergens = (
            c.execute("SELECT allergens FROM menu_items WHERE id=?", (main_id,)).fetchone()["allergens"]
            if main_id is not None else ""
        )
        return {"ok": True, "combo_id": combo["id"], "combo_name": combo["name"],
                "main_allergens": main_allergens,
                "main_available_qty": _available_qty(c, main_id) if main_id is not None else 0, **options}


def _combo_selection(c, combo_id, side_name=None, drink_name=None):
    base_components = _components(c, combo_id)
    selection = {}
    for category, requested in (("Sides", side_name), ("Drinks", drink_name)):
        current = next((item_id for item_id, _ in base_components
                        if c.execute("SELECT category FROM menu_items WHERE id=?", (item_id,)).fetchone()[0]
                        == category), None)
        item, suggestions = _find_item(c, requested) if requested else (None, [])
        if item and item["category"] != category:
            return None, f"{item['name']} is not a {category.lower()} option."
        if requested and not item:
            return None, f"'{requested}' is not a menu item in {category.lower()}."
        selected_id = item["id"] if item else current
        if selected_id is None:
            return None, f"The {combo_id} meal does not include a selectable {category.lower()}."
        selection[category] = selected_id
    components = {item_id: qty for item_id, qty in base_components}
    for category, selected_id in selection.items():
        for item_id in list(components):
            row = c.execute("SELECT category FROM menu_items WHERE id=?", (item_id,)).fetchone()
            if row and row["category"] == category:
                del components[item_id]
        components[selected_id] = components.get(selected_id, 0) + 1
    return {"components": list(components.items()), "side_item_id": selection["Sides"],
            "drink_item_id": selection["Drinks"]}, None


def add_combo_to_order(session_id, combo_name, side_name=None, drink_name=None, qty=1):
    if not session_id:
        return {"ok": False, "error": "No active table session."}
    qty = int(qty)
    if qty < 1:
        return {"ok": False, "error": "Quantity must be at least 1."}
    with conn() as c:
        session = c.execute("SELECT status FROM table_sessions WHERE session_id=?", (session_id,)).fetchone()
        if not session or session["status"] != "open":
            return {"ok": False, "error": "This table session is not open for ordering."}
        combo, suggestions = _find_item(c, combo_name)
        if not combo or combo["category"] != "Combos":
            return {"ok": False, "error": f"'{combo_name}' is not an available meal combo.",
                    "did_you_mean": suggestions}
        selection, error = _combo_selection(c, combo["id"], side_name, drink_name)
        if error:
            return {"ok": False, "error": error}
        available = _available_qty(c, combo["id"], components=selection["components"])
        if qty > available:
            return {"ok": False, "error": f"Only {available} x {combo['name']} can be made with those choices."}
        instr = ""
        line = c.execute("SELECT line_id FROM order_line_items WHERE session_id=? AND item_id=? "
                         "AND special_instructions=?", (session_id, combo["id"], instr)).fetchone()
        if line:
            line_id = line["line_id"]
            existing = c.execute("SELECT side_item_id,drink_item_id FROM order_line_combo_choices WHERE line_id=?",
                                 (line_id,)).fetchone()
            defaults = {}
            for component_id, _ in _components(c, combo["id"]):
                category = c.execute("SELECT category FROM menu_items WHERE id=?",
                                     (component_id,)).fetchone()["category"]
                if category in ("Sides", "Drinks"):
                    defaults[category] = component_id
            current_side = existing["side_item_id"] if existing else defaults.get("Sides")
            current_drink = existing["drink_item_id"] if existing else defaults.get("Drinks")
            if (current_side != selection["side_item_id"]
                    or current_drink != selection["drink_item_id"]):
                c.execute("INSERT INTO order_line_items(session_id,item_id,quantity,special_instructions) "
                          "VALUES (?,?,?,?)", (session_id, combo["id"], qty, instr))
                line_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                c.execute("INSERT INTO order_line_combo_choices(line_id,side_item_id,drink_item_id) VALUES (?,?,?)",
                          (line_id, selection["side_item_id"], selection["drink_item_id"]))
            else:
                c.execute("UPDATE order_line_items SET quantity=quantity+? WHERE line_id=?", (qty, line_id))
                c.execute("INSERT OR REPLACE INTO order_line_combo_choices(line_id,side_item_id,drink_item_id) "
                          "VALUES (?,?,?)", (line_id, selection["side_item_id"], selection["drink_item_id"]))
        else:
            c.execute("INSERT INTO order_line_items(session_id,item_id,quantity,special_instructions) "
                      "VALUES (?,?,?,?)", (session_id, combo["id"], qty, instr))
            line_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO order_line_combo_choices(line_id,side_item_id,drink_item_id) VALUES (?,?,?)",
                      (line_id, selection["side_item_id"], selection["drink_item_id"]))
        side = c.execute("SELECT name FROM menu_items WHERE id=?", (selection["side_item_id"],)).fetchone()[0]
        drink = c.execute("SELECT name FROM menu_items WHERE id=?", (selection["drink_item_id"],)).fetchone()[0]
    return {"ok": True, "message": f"Added {qty} x {combo['name']} with {side} and {drink}.",
            "order": view_current_order(session_id)}


def find_available_alternatives(item_name, qty=1):
    qty = int(qty)
    if qty < 1:
        return {"ok": False, "error": "Quantity must be at least 1."}
    with conn() as c:
        item, suggestions = _find_item(c, item_name)
        if not item:
            return {"ok": False, "error": f"'{item_name}' is not on the menu.", "did_you_mean": suggestions}
        rows = c.execute("SELECT id,name,category,price,allergens FROM menu_items "
                         "WHERE category=? AND id!=? ORDER BY price", (item["category"], item["id"])).fetchall()
        alternatives = []
        for row in rows:
            available = _available_qty(c, row["id"])
            if available >= qty:
                alternatives.append(dict(row, available_qty=available))
    return {"ok": True, "requested_item": item["name"], "quantity": qty, "alternatives": alternatives}


def check_item_allergen(item_id=None, allergen="", item_name=None):
    with conn() as c:
        if item_name:
            r, suggestions = _find_item(c, item_name)
            if not r:
                return {"ok": False, "error": f"'{item_name}' is not on the menu.", "did_you_mean": suggestions}
        elif item_id is not None:
            r = c.execute("SELECT name,allergens FROM menu_items WHERE id=?", (int(item_id),)).fetchone()
        else:
            return {"ok": False, "error": "Provide a menu item name to check its allergen record."}
        if not r:
            return {"ok": False, "error": f"No menu item with id {item_id}."}
        item_allergens = _allergens(c, r["id"])
        has = (allergen or "").strip().casefold() in item_allergens.split(",")
        return {"ok": True, "item": r["name"], "allergen": allergen, "contains": has,
                "all_allergens": item_allergens,
                "note": "Not listed does not guarantee allergen-free; verify with restaurant staff."}


def add_item_to_order(session_id, item_name, qty=1, instructions=""):
    if not session_id:
        return {"ok": False, "error": "No active table session."}
    qty = int(qty)
    if qty < 1:
        return {"ok": False, "error": "Quantity must be at least 1."}
    with conn() as c:
        session = c.execute("SELECT status FROM table_sessions WHERE session_id=?", (session_id,)).fetchone()
        if not session or session["status"] != "open":
            return {"ok": False, "error": "This table session is not open for ordering."}
        item, sugg = _find_item(c, item_name)
        if not item:
            return {"ok": False, "error": f"'{item_name}' is not on the menu.", "did_you_mean": sugg}
        left = _available_qty(c, item["id"])
        if left < qty:
            return {"ok": False, "error": f"Only {max(left,0)} x {item['name']} available." if left > 0
                    else f"{item['name']} is out of stock."}
        instr = (instructions or "").strip()
        line = c.execute("SELECT line_id FROM order_line_items WHERE session_id=? AND item_id=? "
                         "AND special_instructions=?", (session_id, item["id"], instr)).fetchone()
        if line:
            c.execute("UPDATE order_line_items SET quantity=quantity+? WHERE line_id=?", (qty, line[0]))
        else:
            c.execute("INSERT INTO order_line_items(session_id,item_id,quantity,special_instructions) "
                       "VALUES (?,?,?,?)", (session_id, item["id"], qty, instr))
    return {"ok": True, "message": f"Added {qty} x {item['name']}.", "order": view_current_order(session_id)}


def get_combo_recommendations(session_id):
    if not session_id:
        return {"ok": True, "recommendations": []}
    with conn() as c:
        lines = c.execute("""SELECT l.line_id,l.item_id,l.quantity,l.special_instructions,m.name,m.price
                             FROM order_line_items l JOIN menu_items m ON m.id=l.item_id
                             JOIN table_sessions s ON s.session_id=l.session_id
                             WHERE l.session_id=? AND s.status='open' AND m.category!='Combos'
                             AND EXISTS (SELECT 1 FROM combo_components cc
                                         WHERE cc.combo_item_id IN
                                         (SELECT id FROM menu_items WHERE category='Combos')
                                         AND cc.component_item_id=l.item_id)""", (session_id,)).fetchall()
        recommendations = []
        for line in lines:
            combos = c.execute("""SELECT m.id,m.name,m.price FROM menu_items m
                                  JOIN combo_components cc ON cc.combo_item_id=m.id
                                  JOIN menu_items main ON main.id=cc.component_item_id AND main.category='Mains'
                                  WHERE m.category='Combos' AND cc.component_item_id=?
                                  ORDER BY m.price""", (line["item_id"],)).fetchall()
            for combo in combos:
                components = _components(c, combo["id"])
                component_total = 0
                for component_id, component_qty in components:
                    row = c.execute("SELECT price FROM menu_items WHERE id=?", (component_id,)).fetchone()
                    component_total += row["price"] * component_qty
                available = _available_qty(c, combo["id"], exclude_line=line["line_id"])
                recommendations.append({
                    "line_id": line["line_id"], "item_id": line["item_id"], "item_name": line["name"],
                    "quantity": line["quantity"], "instructions": line["special_instructions"],
                    "combo_id": combo["id"], "combo_name": combo["name"], "combo_price": combo["price"],
                    "upgrade_cost": round(combo["price"] - line["price"], 2),
                    "individual_price": component_total,
                    "savings": round(component_total - combo["price"], 2),
                    "available": available >= line["quantity"],
                })
        return {"ok": True, "recommendations": recommendations}


def upgrade_to_combo(session_id, line_id, combo_name, side_name=None, drink_name=None):
    with conn() as c:
        line = c.execute("""SELECT l.*,m.name,m.category FROM order_line_items l
                            JOIN menu_items m ON m.id=l.item_id
                            JOIN table_sessions s ON s.session_id=l.session_id
                            WHERE l.line_id=? AND l.session_id=? AND s.status='open'""",
                         (int(line_id), session_id)).fetchone()
        if not line:
            return {"ok": False, "error": "That order item is no longer available to upgrade."}
        if line["category"] == "Combos":
            return {"ok": False, "error": "That item is already a meal combo."}
        combo, suggestions = _find_item(c, combo_name)
        if not combo or combo["category"] != "Combos":
            return {"ok": False, "error": f"'{combo_name}' is not an available meal combo.",
                    "did_you_mean": suggestions}
        components = _components(c, combo["id"])
        is_combo_main = c.execute(
            "SELECT 1 FROM combo_components cc JOIN menu_items m ON m.id=cc.component_item_id "
            "WHERE cc.combo_item_id=? AND cc.component_item_id=? AND m.category='Mains'",
            (combo["id"], line["item_id"]),
        ).fetchone()
        if not is_combo_main:
            return {"ok": False, "error": f"{combo['name']} does not include {line['name']}."}
        selection, error = _combo_selection(c, combo["id"], side_name, drink_name)
        if error:
            return {"ok": False, "error": error}
        available = _available_qty(c, combo["id"], exclude_line=line["line_id"],
                                   components=selection["components"])
        if line["quantity"] > available:
            return {"ok": False, "error": f"Only {available} x {combo['name']} can be made with the remaining stock."}
        c.execute("UPDATE order_line_items SET item_id=? WHERE line_id=?", (combo["id"], line["line_id"]))
        c.execute("INSERT OR REPLACE INTO order_line_combo_choices(line_id,side_item_id,drink_item_id) "
                  "VALUES (?,?,?)", (line["line_id"], selection["side_item_id"], selection["drink_item_id"]))
        savings = 0
        for component_id, component_qty in components:
            price = c.execute("SELECT price FROM menu_items WHERE id=?", (component_id,)).fetchone()[0]
            savings += price * component_qty
        savings = round((savings - combo["price"]) * line["quantity"], 2)
    return {"ok": True, "message": f"Upgraded {line['quantity']} x {line['name']} to {combo['name']}.",
            "combo": combo["name"], "savings": savings, "order": view_current_order(session_id)}


def remove_item_from_order(session_id, item_name, qty=None, instructions=""):
    if not session_id:
        return {"ok": False, "error": "No active table session."}
    if qty is not None and int(qty) < 1:
        return {"ok": False, "error": "Quantity must be at least 1."}
    with conn() as c:
        session = c.execute("SELECT status FROM table_sessions WHERE session_id=?", (session_id,)).fetchone()
        if not session or session["status"] != "open":
            return {"ok": False, "error": "This table session is not open for ordering."}
        item, sugg = _find_item(c, item_name)
        if not item:
            return {"ok": False, "error": f"'{item_name}' is not on the menu.", "did_you_mean": sugg}
        lines = c.execute("SELECT line_id,quantity,special_instructions FROM order_line_items "
                          "WHERE session_id=? AND item_id=? ORDER BY line_id", (session_id, item["id"])).fetchall()
        if not lines:
            return {"ok": False, "error": f"{item['name']} is not in the current order."}
        if instructions:
            lines = [line for line in lines if line["special_instructions"].casefold() == instructions.strip().casefold()]
            if not lines:
                return {"ok": False, "error": f"No {item['name']} in the order has those instructions."}
        elif qty is not None and len({line["special_instructions"] for line in lines}) > 1:
            return {"ok": False, "clarification_required": True,
                    "choices": [{"line_id": line["line_id"], "quantity": line["quantity"],
                                 "instructions": line["special_instructions"] or "no special instructions"}
                                for line in lines],
                    "error": f"Which {item['name']} should I remove from? The ordered versions have different instructions."}
        todo = int(qty) if qty else sum(l["quantity"] for l in lines)
        for l in lines:
            if todo <= 0:
                break
            take = min(todo, l["quantity"])
            if take == l["quantity"]:
                c.execute("DELETE FROM order_line_items WHERE line_id=?", (l["line_id"],))
            else:
                c.execute("UPDATE order_line_items SET quantity=quantity-? WHERE line_id=?", (take, l["line_id"]))
            todo -= take
    return {"ok": True, "message": f"Removed {item['name']}.", "order": view_current_order(session_id)}


def set_line_quantity(session_id, line_id, quantity):
    quantity = int(quantity)
    if quantity < 0:
        return {"ok": False, "error": "Quantity cannot be negative; use zero to remove the item."}
    with conn() as c:
        line = c.execute("""SELECT l.*,m.name FROM order_line_items l
                            JOIN menu_items m ON m.id=l.item_id
                            JOIN table_sessions s ON s.session_id=l.session_id
                            WHERE l.line_id=? AND l.session_id=? AND s.status='open'""",
                         (int(line_id), session_id)).fetchone()
        if not line:
            return {"ok": False, "error": "That order line is no longer available to edit."}
        if quantity > line["quantity"]:
            components = _components(c, line["item_id"], line["line_id"])
            available = _available_qty(c, line["item_id"], exclude_line=line["line_id"],
                                       components=components)
            if quantity - line["quantity"] > available:
                return {"ok": False, "error": f"Only {line['quantity'] + available} x {line['name']} can be ordered."}
        if quantity == 0:
            c.execute("DELETE FROM order_line_items WHERE line_id=?", (line["line_id"],))
        else:
            c.execute("UPDATE order_line_items SET quantity=? WHERE line_id=?", (quantity, line["line_id"]))
    return {"ok": True, "message": f"Set {line['name']} quantity to {quantity}.",
            "order": view_current_order(session_id)}


def cancel_order(session_id):
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        session = c.execute("SELECT status FROM table_sessions WHERE session_id=?", (session_id,)).fetchone()
        if not session or session["status"] != "open":
            return {"ok": False, "error": "Only an open order can be cancelled."}
        c.execute("DELETE FROM order_line_items WHERE session_id=?", (session_id,))
        c.execute("UPDATE table_sessions SET status='cancelled' WHERE session_id=?", (session_id,))
    return {"ok": True, "message": "The order was cancelled. Reserved stock is available again."}


def view_current_order(session_id):
    with conn() as c:
        rows = c.execute("""SELECT l.line_id,l.item_id,m.name,m.category,l.quantity,
                            l.special_instructions AS instructions,lc.side_item_id,lc.drink_item_id
                            FROM order_line_items l JOIN menu_items m ON m.id=l.item_id
                            LEFT JOIN order_line_combo_choices lc ON lc.line_id=l.line_id
                            WHERE l.session_id=? ORDER BY l.line_id""", (session_id,)).fetchall()
        items = []
        for row in rows:
            unit_price = _component_price(c, row["item_id"], row["line_id"])
            item = dict(row, unit_price=unit_price, is_combo=row["category"] == "Combos",
                        line_total=row["quantity"] * unit_price)
            if row["side_item_id"] is not None:
                item["side_name"] = c.execute("SELECT name FROM menu_items WHERE id=?",
                                              (row["side_item_id"],)).fetchone()[0]
                item["drink_name"] = c.execute("SELECT name FROM menu_items WHERE id=?",
                                               (row["drink_item_id"],)).fetchone()[0]
            item.pop("side_item_id")
            item.pop("drink_item_id")
            items.append(item)
    sub = sum(i["line_total"] for i in items)
    fee = round(sub * SERVICE_RATE, 2)
    return {"items": items, "subtotal": sub, "service_fee": fee, "total": round(sub + fee, 2), "currency": "PHP"}


def get_order_item_allergens(line_id):
    with conn() as c:
        line = c.execute("SELECT item_id FROM order_line_items WHERE line_id=?", (int(line_id),)).fetchone()
        if not line:
            return {"ok": False, "error": "Order line not found."}
        return {"ok": True, "allergens": _allergens(c, line["item_id"], int(line_id))}


def adjust_line(line_id, delta):
    """UI +/- buttons (human-in-the-loop override)."""
    with conn() as c:
        l = c.execute("SELECT * FROM order_line_items WHERE line_id=?", (line_id,)).fetchone()
        if not l:
            return {"ok": False, "error": "Line not found."}
        session = c.execute("SELECT status FROM table_sessions WHERE session_id=?", (l["session_id"],)).fetchone()
        if not session or session["status"] != "open":
            return {"ok": False, "error": "This order is already closed."}
        new = l["quantity"] + int(delta)
        if new <= 0:
            c.execute("DELETE FROM order_line_items WHERE line_id=?", (line_id,))
        else:
            components = _components(c, l["item_id"], l["line_id"])
            if delta > 0 and _available_qty(c, l["item_id"], exclude_line=l["line_id"],
                                             components=components) < delta:
                return {"ok": False, "error": "Not enough stock."}
            c.execute("UPDATE order_line_items SET quantity=? WHERE line_id=?", (new, line_id))
        return {"ok": True, "session_id": l["session_id"]}


def confirm_order(session_id):
    """Transactionally re-check stock, deduct it, close the session, return the receipt."""
    if not session_id:
        return {"ok": False, "error": "No active table session."}
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        session = c.execute("SELECT status, table_no FROM table_sessions WHERE session_id=?", (session_id,)).fetchone()
        if not session or session["status"] != "open":
            return {"ok": False, "error": "This order is not open for confirmation."}
        lines = c.execute("SELECT line_id,item_id,quantity FROM order_line_items WHERE session_id=?",
                          (session_id,)).fetchall()
        tot = {}
        for line in lines:
            for component_id, component_qty in _components(c, line["item_id"], line["line_id"]):
                tot[component_id] = tot.get(component_id, 0) + line["quantity"] * component_qty
        if not tot:
            return {"ok": False, "error": "The order is empty."}
        other_reserved = _reserved_components(c, exclude_session=session_id)
        for component_id, quantity in tot.items():
            item = c.execute("SELECT name,stock_qty FROM menu_items WHERE id=?", (component_id,)).fetchone()
            available = item["stock_qty"] - other_reserved.get(component_id, 0)
            if available < quantity:
                return {"ok": False, "error": f"{item['name']} just ran low (only {max(available, 0)} left)."}
        for component_id, quantity in tot.items():
            c.execute("UPDATE menu_items SET stock_qty=stock_qty-? WHERE id=?", (quantity, component_id))
        c.execute("UPDATE table_sessions SET status='confirmed' WHERE session_id=?", (session_id,))
        table = session["table_no"]
    return {"ok": True, "receipt": {"session_id": session_id, "table_no": table, **view_current_order(session_id)}}
