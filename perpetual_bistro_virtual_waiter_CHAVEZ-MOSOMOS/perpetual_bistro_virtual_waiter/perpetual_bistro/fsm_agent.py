"""Finite-state controller + Ollama tool-calling loop with per-session memory."""
import datetime, json, os, re
import ollama
import db_tools as db

MODEL = os.environ.get("OLLAMA_MODEL", "").strip()
MODEL_PREFERENCE = ("qwen3:8B", "qwen3:8b", "qwen2.5:14b", "qwen2.5:7b",
                    "qwen2.5:3b", "qwen2.5:1.5b")
OLLAMA_CLIENT = ollama.Client(timeout=120)
GREETING, TAKING_ORDER, REVIEW, CONFIRMED = "STATE_GREETING", "STATE_TAKING_ORDER", "STATE_REVIEW", "STATE_CONFIRMED"
LOG_PATH = os.path.join(db.HERE, "sample_conversation_log.json")
CONFIRM_RE = re.compile(
    r"^\s*(?:(?:yes|yep|yeah)(?:[,.]?\s*(?:please|confirm(?: it| the order)?|go ahead|"
    r"let'?s confirm|let'?s do it))?|confirm(?: it| the order)?|confirmed|ok(?:ay)?|correct|"
    r"looks good|send it|that'?s right|please confirm(?: it)?)[.! ]*$", re.I
)
DECLINE_RE = re.compile(
    r"^\s*(?:no|no thanks|keep it|keep my order|don't cancel|do not cancel)[.! ]*$", re.I
)
UPGRADE_ACCEPT_RE = re.compile(
    r"^\s*(?:yes|yep|yeah|sure|okay|ok|please do|do it|upgrade(?: it)?(?: please)?|"
    r"make it a meal|let's do it|sounds good|i(?:'d| would) like that)(?:\b|[,!.])",
    re.I,
)
CANCEL_ORDER_RE = re.compile(
    r"^\s*(?:(?:(?:i want to|i would like to|i'd like to|please)\s+)?"
    r"(?:cancel|clear)\s+(?:(?:my|the|whole|entire)\s+)*(?:order|tab|everything)"
    r"|start over|start a new order)[.! ]*$", re.I
)
REVIEW_RE = re.compile(
    r"^\s*(?:that'?s all|that is all|i'?m done(?: ordering)?|im done(?: ordering)?|i am done(?: ordering)?|"
    r"done|i'?m finished(?: ordering)?|"
    r"finish(?: ordering)?|review(?: my order)?|show(?: me)? (?:my )?(?:bill|tab|order)|"
    r"(?:the )?bill|check ?out|nothing else|no thanks|that'?s everything|that is everything)"
    r"[.! ]*$", re.I
)


def _tool(name, desc, props=None, req=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props or {}, "required": list(req)}}}


def detect_menu_diet_filter(text):
    normalized = (text or "").casefold()
    menu_request = re.search(
        r"\b(?:what|which|show|list|menu|available|can i see|could i see|options?|meals?|combos?)\b",
        normalized,
    )
    if not menu_request:
        return None
    aliases = (
        ("tree nuts", "nuts"), ("peanuts", "nuts"), ("peanut", "nuts"),
        ("shellfish", "shellfish"), ("gluten", "gluten"), ("wheat", "gluten"),
        ("dairy", "dairy"), ("milk", "dairy"), ("eggs", "egg"), ("egg", "egg"),
        ("soy", "soy"), ("fish", "fish"), ("sesame", "sesame"), ("nuts", "nuts"), ("nut", "nuts"),
    )
    alias_pattern = "|".join(re.escape(label) for label, _ in aliases)
    exclusion = re.search(
        rf"\b(?:without(?:\s+any)?|no|free\s+of)\s+({alias_pattern})\b", normalized
    )
    if not exclusion:
        exclusion = re.search(rf"\b({alias_pattern})[\s-]*free\b", normalized)
    if exclusion:
        requested = exclusion.group(1).casefold()
        allergen = next(value for label, value in aliases if label == requested)
        return f"allergen-free:{allergen}"
    return next((diet for diet in ("vegetarian", "vegan") if diet in normalized), None)


def menu_filter_label(filter_key):
    if filter_key.startswith("allergen-free:"):
        allergen = filter_key.split(":", 1)[1]
        return f"No {allergen} (listed allergens)"
    return filter_key.title() if filter_key in ("vegetarian", "vegan") else "All items"


def allergen_filter_options():
    return tuple(menu_filter_label(f"allergen-free:{allergen}") for allergen in (
        "dairy", "egg", "gluten", "fish", "nuts", "sesame", "shellfish", "soy",
    ))


def menu_filter_key(filter_label):
    normalized = (filter_label or "").strip().casefold()
    if normalized.startswith("no ") and " (listed allergens)" in normalized:
        allergen = normalized[3:].split(" (listed allergens)", 1)[0]
        return f"allergen-free:{allergen}"
    if normalized in ("vegetarian", "vegan"):
        return normalized
    return "all"


def is_menu_browse_request(text):
    normalized = (text or "").casefold().strip()
    if re.match(r"^menu\s*[.!?]*$", normalized):
        return True
    return bool(re.search(
        r"\b(?:"
        r"(?:can|could|may)\s+i\s+(?:please\s+)?(?:see|view|browse|open)\s+"
        r"(?:the\s+)?(?:full\s+)?menu\b|"
        r"(?:show|open|display|browse)\s+(?:me\s+)?(?:the\s+)?(?:full\s+)?menu\b|"
        r"(?:let\s+me\s+)?(?:see|view|browse)\s+(?:the\s+)?menu\b|"
        r"menu\s+(?:please|screen)"
        r")\b",
        normalized,
    ))


def _format_menu_items(items, include_stock=False):
    category_order = (
        ("Combos", "Meal combos"), ("Mains", "Mains"), ("Pasta", "Pasta"),
        ("Sides", "Sides"), ("Drinks", "Drinks"), ("Desserts", "Desserts"),
    )
    groups = []
    for category, heading in category_order:
        category_items = [item for item in items if item["category"] == category]
        if not category_items:
            continue
        lines = [heading + ":"]
        for item in category_items:
            details = f"PHP {item['price']:.2f}"
            if include_stock:
                details += f" | {item['available_qty']} available"
            lines.append(f"  - {item['name']} ({details})")
        groups.append("\n".join(lines))
    return "\n\n".join(groups)


TOOLS = [
    _tool("get_menu_by_category", "List menu items (with ids, prices, stock, allergens). Use 'all' for everything.",
          {"category": {"type": "string", "description": "Mains, Pasta, Sides, Drinks, Desserts or all"}}, ["category"]),
    _tool("get_menu_by_dietary_filter", "List available vegetarian/vegan tagged dishes or exclude a named listed allergen using allergen-free:<tag>.",
          {"diet": {"type": "string"}}, ["diet"]),
    _tool("check_item_allergen", "Check the allergen record for the named menu item; pass its menu name, not a guessed id.",
          {"item_name": {"type": "string"}, "allergen": {"type": "string"}}, ["item_name", "allergen"]),
    _tool("add_item_to_order", "Add a menu item to the customer's tab.",
          {"item_name": {"type": "string"}, "qty": {"type": "integer"},
           "instructions": {"type": "string", "description": "e.g. 'no onions, extra cheese'"}}, ["item_name", "qty"]),
    _tool("remove_item_from_order", "Remove an item (or some quantity of it) from the tab.",
          {"item_name": {"type": "string"}, "qty": {"type": "integer", "description": "omit to remove all"},
           "instructions": {"type": "string", "description": "select a specific customized version if needed"}},
          ["item_name"]),
    _tool("set_line_quantity", "Set one exact order line to a quantity; use its line_id from the current tab. Quantity 0 removes it.",
          {"line_id": {"type": "integer"}, "quantity": {"type": "integer"}}, ["line_id", "quantity"]),
    _tool("find_available_alternatives", "Find in-stock items in the same menu category when a requested item is unavailable.",
          {"item_name": {"type": "string"}, "qty": {"type": "integer"}}, ["item_name"]),
    _tool("view_current_order", "Show the current tab with totals."),
    _tool("get_combo_recommendations", "Find meal combo upgrades for mains already in the customer's order."),
    _tool("upgrade_to_combo", "Turn an a la carte main into the matching meal combo, preserving its instructions.",
          {"line_id": {"type": "integer"}, "combo_name": {"type": "string"},
           "side_name": {"type": "string"}, "drink_name": {"type": "string"}}, ["line_id", "combo_name"]),
    _tool("add_combo_to_order", "Add a meal combo with the customer's selected side and drink.",
          {"combo_name": {"type": "string"}, "side_name": {"type": "string"},
           "drink_name": {"type": "string"}, "qty": {"type": "integer"}},
          ["combo_name", "side_name", "drink_name", "qty"]),
    _tool("proceed_to_review", "Call when the customer says they are finished ordering."),
]
ORDER_TOOL = next(tool for tool in TOOLS if tool["function"]["name"] == "add_item_to_order")
MUTATING = {"add_item_to_order", "add_combo_to_order", "remove_item_from_order",
            "upgrade_to_combo", "set_line_quantity"}


def _tool_reply(name, args, result):
    if not result.get("ok"):
        text = result.get("error", "That request could not be completed.")
        alternatives = result.get("alternatives", [])
        if alternatives:
            options = ", ".join(
                f"{item['name']} (PHP {item['price']:.2f})" for item in alternatives
            )
            text += f" Available alternatives: {options}."
        return text
    if name == "check_item_allergen":
        if result["contains"]:
            status = f"{result['allergen']} is listed for this item."
        else:
            status = (f"{result['allergen']} is not listed in this item's menu record; "
                      "that does not guarantee the item is free of it.")
        return (f"{result['item']}: {status} Listed allergens: {result['all_allergens']}. "
                "Please check with restaurant staff about ingredients and cross-contact.")
    if name == "get_menu_by_dietary_filter":
        if not result["items"]:
            if result.get("allergen"):
                return (f"No available items match the {result['allergen']}-free filter based on recorded allergen data. "
                        "Please check ingredients and cross-contact with restaurant staff.")
            return f"No available items are explicitly tagged {result['diet']} in the menu."
        options = _format_menu_items(result["items"])
        if result.get("allergen"):
            allergen = result["allergen"]
            return (f"Available items with no {allergen} listed in their allergen records:\n\n{options}\n\n"
                    f"Note: this is not a guarantee of being {allergen}-free. Please verify ingredients and "
                    "cross-contact with restaurant staff.")
        return (f"Available items tagged {result['diet']}:\n\n{options}\n\n"
                "Note: dietary tags are not an allergen or cross-contact guarantee.")
    if name == "get_menu_by_category":
        if not result["items"]:
            return "There are no items listed in that menu category."
        return f"Menu:\n\n{_format_menu_items(result['items'], include_stock=True)}"
    if name == "find_available_alternatives":
        if not result["alternatives"]:
            return f"There are no in-stock alternatives in the {result['requested_item']} category."
        options = ", ".join(
            f"{item['name']} (PHP {item['price']:.2f}; {item['available_qty']} available)"
            for item in result["alternatives"]
        )
        return f"In-stock alternatives: {options}."
    if name in ("add_item_to_order", "remove_item_from_order", "set_line_quantity", "upgrade_to_combo"):
        text = result["message"]
        allergens = result.get("allergens")
        if allergens:
            text += (f" Allergen information: {allergens}. "
                     "Please verify ingredients and cross-contact with restaurant staff.")
        return text
    if name == "add_combo_to_order":
        text = result["message"]
        allergens = result.get("allergens")
        if allergens:
            text += (f" Allergen information: {allergens}. "
                     "Please verify ingredients and cross-contact with restaurant staff.")
        return text
    if name == "view_current_order":
        return f"Current order total: PHP {result['total']:.2f}."
    if name == "proceed_to_review":
        return "Your order is ready for review."
    return None


class WaiterAgent:
    def __init__(self):
        self.model = None
        self._tool_capable_models = set()
        self.pending_combo_offer = None
        self.pending_combo_upgrade = None
        if not self._load():
            self.reset()

    def connect(self):
        """Find an installed model and verify it actually returns tool calls."""
        self.model = None
        try:
            installed = [model.model for model in OLLAMA_CLIENT.list().models]
        except Exception as e:
            host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
            raise ConnectionError(
                f"Could not connect to Ollama at {host}. Make sure it is running, then select Reconnect."
            ) from e
        if not installed:
            raise ConnectionError(
                "Ollama is running, but no models are installed. Run `ollama pull qwen3:8B`, then select Reconnect."
            )

        requested = MODEL
        if requested:
            candidates = [name for name in installed
                          if name == requested or name.startswith(requested + ":")]
            if not candidates:
                raise ConnectionError(
                    f"Model '{requested}' is not installed. Installed models: {', '.join(installed)}. "
                    "Clear OLLAMA_MODEL or set it to an installed model."
                )
        else:
            candidates = [
                *(name for preferred in MODEL_PREFERENCE
                  for name in installed if name.lower() == preferred.lower()),
                *(name for name in installed
                  if name.lower() not in {preferred.lower() for preferred in MODEL_PREFERENCE}),
            ]

        failures = []
        for candidate in candidates:
            if candidate in self._tool_capable_models:
                self.model = candidate
                return candidate
            try:
                response = OLLAMA_CLIENT.chat(
                    model=candidate,
                    messages=[
                        {"role": "system", "content":
                         "You are testing restaurant order handling. When asked to order food, call "
                         "add_item_to_order. Never claim you placed an order without calling the tool."},
                        {"role": "user", "content": "Add one Cheeseburger with no onions."},
                    ],
                    tools=[ORDER_TOOL],
                    think=False,
                    options={"temperature": 0, "num_predict": 128},
                )
                calls = response.message.tool_calls or []
                order_call = next((call for call in calls
                                   if call.function.name == "add_item_to_order"), None)
                if not order_call:
                    failures.append(f"{candidate}: did not call the order tool")
                    continue
                arguments = dict(order_call.function.arguments)
                if (str(arguments.get("item_name", "")).casefold() != "cheeseburger"
                        or int(arguments.get("qty", 0)) != 1
                        or "onion" not in str(arguments.get("instructions", "")).casefold()):
                    failures.append(f"{candidate}: returned incorrect order-tool arguments")
                    continue
            except Exception as e:
                failures.append(f"{candidate}: {str(e)[:120]}")
                continue
            self._tool_capable_models.add(candidate)
            self.model = candidate
            return candidate

        details = "; ".join(failures)
        raise ConnectionError(
            "No installed Ollama model passed the function-calling check. "
            f"Installed models: {', '.join(installed)}. "
            f"Install a model with tool support (for example `ollama pull qwen3:8B`). {details}"
        )

    # ------------------------------------------------------------ persistence (survives refresh + restart)
    def _persist(self):
        db.save_kiosk_state({"state": self.state, "session_id": self.session_id, "table_no": self.table_no,
                             "history": self.history, "transcript": self.transcript, "log": self.log,
                             "receipt": self.receipt, "pending_confirmation": self.pending_confirmation,
                             "pending_combo_offer": self.pending_combo_offer,
                             "pending_combo_upgrade": self.pending_combo_upgrade})

    def _load(self):
        try:
            d = db.load_kiosk_state()
            if not d or (d["session_id"] and not db.session_exists(d["session_id"])):
                return False
            self.state, self.session_id, self.table_no = d["state"], d["session_id"], d["table_no"]
            self.history, self.transcript, self.log, self.receipt = d["history"], d["transcript"], d["log"], d["receipt"]
            self.pending_confirmation = d.get("pending_confirmation")
            self.pending_combo_offer = d.get("pending_combo_offer")
            self.pending_combo_upgrade = d.get("pending_combo_upgrade")
            self.last_followups = []
            self._log("session_resumed")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------ session
    def reset(self):
        self.state, self.session_id, self.table_no = GREETING, None, None
        self.history, self.transcript, self.log, self.receipt = [], [], [], None
        self.pending_confirmation = None
        self.pending_combo_offer = None
        self.pending_combo_upgrade = None
        self.last_followups = []
        greeting = "Welcome to Perpetual Bistro! Which table number are you at?"
        self.transcript.append({"role": "waiter", "text": greeting})
        self._log("session_reset")

    def _log(self, event, **data):
        self.log.append({"time": datetime.datetime.now().isoformat(timespec="seconds"),
                         "state": self.state, "event": event, **data})
        self._persist()

    def _go(self, new, reason):
        self._log("state_transition", from_state=self.state, to_state=new, reason=reason)
        self.state = new
        self._persist()

    def snapshot(self):
        order = db.view_current_order(self.session_id) if self.session_id else None
        return {"state": self.state, "table_no": self.table_no, "transcript": self.transcript,
                "order": order, "receipt": self.receipt}

    def save_log(self):
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            json.dump({"table_no": self.table_no, "session_id": self.session_id, "events": self.log}, f, indent=2)

    # ------------------------------------------------------------ actions
    def say(self, text):
        self.transcript.append({"role": "waiter", "text": text})
        self._persist()
        return text

    def go_review(self):
        if self.state != TAKING_ORDER:
            return
        if not self.session_id or not db.view_current_order(self.session_id)["items"]:
            self.say("Your tab is empty. Tell me what you'd like first.")
            return
        self._go(REVIEW, "customer finished ordering")
        self.say(self._summary() + " Is everything correct? Say 'confirm' or press Confirm order.")

    def confirm(self):
        if self.state != REVIEW:
            return
        if not self.session_id:
            return
        res = db.confirm_order(self.session_id)
        self._log("confirm_order", result=res)
        if not res["ok"]:
            self._go(TAKING_ORDER, "confirmation failed")
            self.say(f"Sorry, I couldn't confirm: {res['error']} Let's adjust your order.")
            return
        self.receipt = res["receipt"]
        self._go(CONFIRMED, "customer confirmed")
        self._log("db_state", order=self.receipt)
        self.say(f"Order confirmed and sent to the kitchen. Total {self.receipt['total']:.2f} PHP. Thank you!")
        self.save_log()

    def adjust(self, line_id, delta):
        if self.state == CONFIRMED:
            return {"ok": False, "error": "This order is already confirmed."}
        res = db.adjust_line(int(line_id), int(delta))
        if res["ok"] and self.state == REVIEW:
            self._go(TAKING_ORDER, "order edited during review")
            self.say("Your order was adjusted. Let me know when you're ready to review again.")
        self._log("manual_adjust", line_id=line_id, delta=delta, result=res)
        return res

    def begin_combo_upgrade(self, line_id, combo_name, combo_id=None):
        if self.state == CONFIRMED:
            return self.say("This order is already confirmed.")
        if not self.session_id:
            return self.say("Please provide a table number first.")
        if self.state == REVIEW:
            self._go(TAKING_ORDER, "combo upgrade requested during review")
        if not combo_id:
            combos = db.get_menu_by_category("Combos")["items"]
            combo = next((item for item in combos if item["name"].casefold() == combo_name.casefold()), None)
            if not combo:
                return self.say(f"I couldn't find the {combo_name} meal. Your tab was not changed.")
            combo_id = combo["id"]
        self.pending_combo_offer = None
        self.pending_combo_upgrade = {
            "line_id": int(line_id), "combo_name": combo_name, "combo_id": int(combo_id),
            "side_name": None, "drink_name": None,
        }
        self._persist()
        return self._handle_combo_choice("")

    def _handle_combo_choice(self, text):
        pending = self.pending_combo_upgrade
        options = db.get_combo_options(pending["combo_id"])
        if not options.get("ok"):
            self.pending_combo_upgrade = None
            self._persist()
            return self.say(f"I couldn't load the meal choices: {options.get('error', 'Unknown error')}")

        normalized = (text or "").casefold()
        chosen = {}
        choice_aliases = {
            "French Fries": ("fries",),
            "Garden Salad": ("salad",),
            "Iced Tea": ("tea",),
            "Bottled Water": ("water",),
            "Mango Smoothie": ("smoothie",),
        }
        for category, field in (("sides", "side_name"), ("drinks", "drink_name")):
            available = [item for item in options[category] if item["available_qty"] > 0]
            matches = [
                item for item in available
                if item["name"].casefold() in normalized
                or any(re.search(rf"\b{re.escape(alias)}\b", normalized)
                       for alias in choice_aliases.get(item["name"], ()))
            ]
            if len(matches) == 1:
                chosen[field] = matches[0]["name"]
            elif len(matches) > 1:
                names = ", ".join(item["name"] for item in matches)
                return self.say(f"Which {category[:-1]} did you mean: {names}?")
        for field, value in chosen.items():
            pending[field] = value

        missing = []
        for category, field, label in (("sides", "side_name", "side"), ("drinks", "drink_name", "drink")):
            if pending[field]:
                continue
            available = [item for item in options[category] if item["available_qty"] > 0]
            if not available:
                return self.say(f"There are no {category} currently available for that meal. Your tab is unchanged.")
            names = ", ".join(
                f"{item['name']} ({'+' if item['price_adjustment'] > 0 else ''}"
                f"PHP {item['price_adjustment']:.2f})"
                if item["price_adjustment"] else item["name"]
                for item in available
            )
            missing.append(f"{label}: {names}")
        if missing:
            self._persist()
            if len(missing) == 2:
                return self.say(
                    f"Sure. Which side and drink would you like for the {pending['combo_name']}? "
                    f"Available {missing[0]}; {missing[1]}."
                )
            return self.say(f"Which {missing[0]} would you like?")

        result = self._run_tool("upgrade_to_combo", {
            "line_id": pending["line_id"], "combo_name": pending["combo_name"],
            "side_name": pending["side_name"], "drink_name": pending["drink_name"],
        })
        if not result["ok"]:
            return self.say(f"I couldn't complete the upgrade: {result['error']} Your tab is unchanged.")
        self.pending_combo_upgrade = None
        self.pending_combo_offer = None
        self._persist()
        return self.say(
            f"{result['message']} Side: {pending['side_name']}; drink: {pending['drink_name']}. "
            "Your tab has been updated."
        )

    def _summary(self):
        o = db.view_current_order(self.session_id)
        lines = ", ".join(
            f"{i['quantity']}x {i['name']}"
            + (f" with {i['side_name']} and {i['drink_name']}" if i.get("side_name") else "")
            + (f" ({i['instructions']})" if i["instructions"] else "")
            for i in o["items"]
        )
        return f"Your tab: {lines}. Subtotal {o['subtotal']:.2f}, service fee {o['service_fee']:.2f}, total {o['total']:.2f} PHP."

    def _catalog_query(self, text):
        normalized = text.casefold()
        if re.search(r"\b(?:add|order|remove|cancel|change|update|make it|set quantity)\b", normalized):
            return None

        if re.search(r"\b(?:allerg(?:y|ies|en|ens)|contain(?:s)?|have|free of)\b", normalized):
            items = self._run_tool("get_menu_by_category", {"category": "all"})["items"]
            item = next((item for item in sorted(items, key=lambda entry: len(entry["name"]), reverse=True)
                         if item["name"].casefold() in normalized), None)
            if item:
                allergen_aliases = (
                    ("tree nuts", "nuts"), ("peanuts", "nuts"), ("peanut", "nuts"),
                    ("shellfish", "shellfish"), ("gluten", "gluten"), ("wheat", "gluten"),
                    ("dairy", "dairy"), ("milk", "dairy"), ("egg", "egg"),
                    ("soy", "soy"), ("fish", "fish"), ("sesame", "sesame"), ("nuts", "nuts"),
                )
                requested = next((value for label, value in allergen_aliases
                                  if re.search(rf"\b{re.escape(label)}\b", normalized)), None)
                if requested:
                    result = self._run_tool("check_item_allergen", {
                        "item_name": item["name"], "allergen": requested,
                    })
                    return _tool_reply("check_item_allergen", {}, result)
                return (f"{item['name']} lists these allergens: {item['allergens'] or 'none listed'}. "
                        "An unlisted allergen is not a safety guarantee; please check with restaurant staff.")

        menu_question = re.search(
            r"\b(?:what|which|show|list|menu|do you have|what do you|what is available|"
            r"any .+ options|tell me about|can i see|could i see|meals?|options?)\b", normalized
        )
        if not menu_question:
            return None
        diet = detect_menu_diet_filter(normalized)
        if diet:
            result = self._run_tool("get_menu_by_dietary_filter", {"diet": diet})
            if re.search(r"\b(?:meal|meals|combo|combos)\b", normalized):
                result["items"] = [item for item in result["items"] if item["is_combo"]]
                if not result["items"] and result.get("allergen"):
                    return (f"No available meal combos match the {result['allergen']}-free filter based on "
                            "recorded allergen data. "
                            "Please check ingredients and cross-contact with restaurant staff.")
            return _tool_reply("get_menu_by_dietary_filter", {}, result)

        category_aliases = (
            ("dessert", "Desserts"), ("cake", "Desserts"), ("halo-halo", "Desserts"),
            ("drink", "Drinks"), ("beverage", "Drinks"), ("tea", "Drinks"),
            ("smoothie", "Drinks"), ("water", "Drinks"), ("side", "Sides"),
            ("fries", "Sides"), ("salad", "Sides"), ("pasta", "Pasta"),
            ("spaghetti", "Pasta"), ("penne", "Pasta"), ("combo", "Combos"),
            ("meal", "Combos"), ("main", "Mains"), ("burger", "Mains"),
            ("salmon", "Mains"), ("chicken", "Mains"),
        )
        category = next((value for alias, value in category_aliases if alias in normalized), "all")
        result = self._run_tool("get_menu_by_category", {"category": category})
        return _tool_reply("get_menu_by_category", {}, result)

    def _respond_to_user(self, text):
        reply = self._llm_turn(text)
        self.say(reply)
        for followup in self.last_followups:
            self.say(followup)
        return reply

    # ------------------------------------------------------------ conversation
    def handle_user(self, text):
        self.transcript.append({"role": "user", "text": text})
        self._log("user_message", text=text)
        if self.state == GREETING:
            m = re.search(r"\d+", text)
            if not m:
                catalog_reply = self._catalog_query(text)
                if catalog_reply:
                    return self.say(catalog_reply + "\n\nWhich table number are you at today?")
                return self.say("I didn't catch a table number. What number is on your table?")
            self.table_no = int(m.group())
            self.session_id = db.create_session(self.table_no)
            self._go(TAKING_ORDER, f"table {self.table_no} captured")
            return self.say(f"Welcome to Table {self.table_no}! I can show the menu, check allergens, and take your order. What would you like?")
        if self.state == CONFIRMED:
            return self.say("Your order is already confirmed. Press 'New table' to start another.")
        if self.pending_combo_upgrade:
            if DECLINE_RE.match(text):
                self.pending_combo_upgrade = None
                self._persist()
                return self.say("No problem. I kept the original item on your tab.")
            return self._handle_combo_choice(text)
        if self.pending_combo_offer:
            if UPGRADE_ACCEPT_RE.match(text):
                offer = self.pending_combo_offer
                self.pending_combo_offer = None
                self.pending_combo_upgrade = {
                    "line_id": offer["line_id"], "combo_name": offer["combo_name"],
                    "combo_id": offer["combo_id"], "side_name": None, "drink_name": None,
                }
                self._persist()
                return self._handle_combo_choice(text)
            if DECLINE_RE.match(text):
                self.pending_combo_offer = None
                self._persist()
                return self.say("No problem. I kept the original item on your tab.")
            self.pending_combo_offer = None
            self._persist()
        if self.pending_confirmation == "cancel_order":
            if CONFIRM_RE.match(text):
                result = db.cancel_order(self.session_id)
                self._log("cancel_order", result=result)
                if result["ok"]:
                    self._go(GREETING, "customer confirmed order cancellation")
                    self.session_id, self.table_no, self.history = None, None, []
                    self.pending_confirmation = None
                    return self.say("Your order has been cancelled and the reserved stock released. What table number are you at?")
                self.pending_confirmation = None
                return self.say(f"I couldn't cancel the order: {result['error']}")
            if DECLINE_RE.match(text):
                self.pending_confirmation = None
                self._persist()
                return self.say("Okay, I kept your order. What would you like to do next?")
            catalog_reply = self._catalog_query(text)
            if catalog_reply:
                self.pending_confirmation = None
                self._persist()
                return self.say("I kept your order while answering that. " + catalog_reply)
            self.pending_confirmation = None
            self._persist()
            self.say("I kept your order while answering that.")
            return self._respond_to_user(text)
        if CANCEL_ORDER_RE.match(text) and self.state in (TAKING_ORDER, REVIEW):
            self.pending_confirmation = "cancel_order"
            self._persist()
            return self.say("Do you want to cancel your entire unconfirmed order? Reply yes to cancel or no to keep it.")
        if self.state == REVIEW and CONFIRM_RE.match(text):
            self.confirm()
            return self.transcript[-1]["text"]
        if self.state == TAKING_ORDER and REVIEW_RE.search(text) and not re.search(r"\b(add|remove|no|extra)\b", text, re.I):
            self.go_review()
            return self.transcript[-1]["text"]
        catalog_reply = self._catalog_query(text)
        if catalog_reply:
            return self.say(catalog_reply)
        return self._respond_to_user(text)

    def _system_prompt(self):
        return (f"You are the virtual waiter at Perpetual Bistro, serving Table {self.table_no}. Current state: {self.state}.\n"
                "Be a warm, attentive waiter. Reply directly in natural, concise language; ask one clear question "
                "when an item, quantity, or modification is ambiguous. Keep replies to 1-3 short sentences.\n"
                "Stay focused on Perpetual Bistro orders. Politely redirect unrelated questions to the menu or order.\n"
                "Ground every menu, price, availability, and allergen claim in tool results. Never invent menu items "
                "or claim an item is allergen-free; 'none listed' only means no allergens are recorded. Call the menu "
                "tool before recommending items or answering general menu questions. For a specific allergen question, "
                "call the allergen tool with the item name from the customer's question; do not guess a menu id.\n"
                "Call a tool to add, remove, or inspect order items. Never claim a change until its tool succeeds. "
                "Preserve requested quantities and put every customization in the instructions argument. If a tool "
                "returns an out-of-stock error, use find_available_alternatives and only offer items in its result. "
                "Use set_line_quantity for quantity edits to a specific existing line; use the exact line_id from "
                "the tab, and ask which line if the request is ambiguous. A removal error with "
                "clarification_required means ask the customer which customized line they mean. Vegetarian and vegan "
                "filters return only explicitly tagged items; allergen-free filters exclude items listing the "
                "requested allergen from dishes and configurable combo components, but that is not a safety "
                "guarantee. If no results exist, say no verified match is listed; never infer "
                "dietary tags from an item name. Allergen-free menu filters exclude listed allergens from dishes "
                "and configurable combo components, but do not guarantee safety. For allergen questions, distinguish "
                "'not listed' from a guarantee of safety and "
                "recommend checking with restaurant staff about cross-contact.\n"
                "Combos are discounted meals made from the named main plus fries and iced tea. After a customer "
                "orders an a la carte main, check get_combo_recommendations and briefly mention a matching available "
                "upgrade and its savings when one exists. State combo_price as the new total and upgrade_cost as the "
                "extra amount over the main already ordered; savings is only versus buying all combo components "
                "separately. Never confuse the upgrade cost with combo price or savings. Keep the suggestion optional "
                "and never repeat it after the customer declines. When the customer accepts a meal-upgrade offer, "
                "ask which available side and drink they want, then wait for their choices before calling "
                "upgrade_to_combo with its line_id, side_name, and drink_name; this preserves special instructions. "
                "If they provide only one choice, ask only for the missing choice. Never use defaults without asking "
                "or describe a combo as added until the tool succeeds. Meal combos allow one available side and one "
                "available drink; never substitute a choice without asking.\n"
                "During review, order changes return the customer to order editing; do not imply the order is confirmed. "
                "Prices are in PHP.\n"
                f"Ground truth of the tab right now: {json.dumps(db.view_current_order(self.session_id))}")

    def _llm_turn(self, text):
        self.history.append({"role": "user", "content": text})
        self.history = self.history[-30:]
        combo_recommendations = []
        tool_feedback = []
        self.last_followups = []
        try:
            if not self.model:
                self.connect()
            for _ in range(6):
                resp = OLLAMA_CLIENT.chat(model=self.model, tools=TOOLS, think=False,
                                          messages=[{"role": "system", "content": self._system_prompt()}] + self.history)
                msg = resp.message
                calls = msg.tool_calls or []
                self.history.append({"role": "assistant", "content": msg.content or "",
                                     **({"tool_calls": [{"function": {"name": c.function.name,
                                         "arguments": dict(c.function.arguments)}} for c in calls]} if calls else {})})
                if not calls:
                    content = (msg.content or "").strip()
                    if tool_feedback:
                        content = " ".join(tool_feedback)
                    if content:
                        offers = [item for item in combo_recommendations if item["available"]]
                        if offers:
                            sentence_start = max(content.rfind(". "), content.rfind("! "),
                                                 content.rfind("? ")) + 2
                            if re.search(r"upgrade|combo|meal|savings?", content[sentence_start:],
                                         flags=re.IGNORECASE):
                                content = content[:sentence_start].strip()
                            offer = offers[0]
                            self.pending_combo_offer = {
                                "line_id": offer["line_id"], "combo_name": offer["combo_name"],
                                "combo_id": offer["combo_id"],
                            }
                            self._persist()
                            self.last_followups.append(
                                f"Optional meal upgrade: {offer['combo_name']} is PHP {offer['combo_price']:.2f} total, "
                                f"PHP {offer['upgrade_cost']:.2f} extra per meal, and saves "
                                f"PHP {offer['savings']:.2f} versus buying its items separately. "
                                "Would you like me to upgrade it?"
                            )
                        return content
                    raise RuntimeError("The model returned an empty response. Try reconnecting or selecting another model.")
                for c in calls:
                    args = dict(c.function.arguments)
                    result = self._run_tool(c.function.name, args)
                    if c.function.name == "get_combo_recommendations":
                        combo_recommendations.extend(result.get("recommendations", []))
                    elif c.function.name in ("add_item_to_order", "add_combo_to_order") and result.get("ok"):
                        order_items = result["order"]["items"]
                        ordered_item = next(
                            (item for item in reversed(order_items)
                             if item["name"].casefold() == str(args.get("item_name", args.get("combo_name", ""))).casefold()),
                            None,
                        )
                        if ordered_item:
                            allergens = db.get_order_item_allergens(ordered_item["line_id"])
                            if allergens["ok"]:
                                result["allergens"] = allergens["allergens"]
                    if c.function.name == "add_item_to_order" and result.get("ok"):
                        rec_result = self._run_tool("get_combo_recommendations", {})
                        item_name = str(args.get("item_name", "")).casefold()
                        matching = [item for item in rec_result["recommendations"]
                                    if item["item_name"].casefold() == item_name]
                        result["combo_recommendations"] = matching
                        combo_recommendations.extend(matching)
                    feedback = _tool_reply(c.function.name, args, result)
                    if feedback:
                        tool_feedback.append(feedback)
                    self.history.append({"role": "tool", "tool_name": c.function.name, "content": json.dumps(result)})
            return "Sorry, I got tangled up - could you repeat that?"
        except Exception as e:  # Ollama offline, model missing, etc.
            self._log("llm_error", error=str(e))
            if isinstance(e, ConnectionError):
                return str(e)
            status = getattr(e, "status_code", None)
            if status == 404:
                missing_model = self.model
                self.model = None
                return f"Ollama could not find the selected model '{missing_model}'. Select Reconnect to choose an installed model."
            if isinstance(e, (TimeoutError,)) or "timeout" in str(e).lower():
                return f"The response from '{self.model}' took too long. Try again or choose a smaller installed model."
            return f"Ollama request failed for '{self.model}': {e}"

    def _run_tool(self, name, args):
        args.pop("session_id", None)  # the LLM never controls the session
        fn = {"get_menu_by_category": db.get_menu_by_category, "check_item_allergen": db.check_item_allergen,
              "add_item_to_order": db.add_item_to_order, "remove_item_from_order": db.remove_item_from_order,
              "upgrade_to_combo": db.upgrade_to_combo, "set_line_quantity": db.set_line_quantity,
              "find_available_alternatives": db.find_available_alternatives,
              "get_menu_by_dietary_filter": db.get_menu_by_dietary_filter,
              "add_combo_to_order": db.add_combo_to_order}.get(name)
        try:
            if name == "proceed_to_review":
                if db.view_current_order(self.session_id)["items"]:
                    self._go(REVIEW, "LLM called proceed_to_review")
                    result = {"ok": True, "message": "Now in review. Summarise the tab for the customer."}
                else:
                    result = {"ok": False, "error": "The tab is empty."}
            elif name == "view_current_order":
                result = {"ok": True, **db.view_current_order(self.session_id)}
            elif name == "get_combo_recommendations":
                result = db.get_combo_recommendations(self.session_id)
            elif fn:
                if name in MUTATING:
                    if not self.session_id:
                        return {"ok": False, "error": "Please provide a table number first."}
                    if self.state == CONFIRMED:
                        return {"ok": False, "error": "This order is already confirmed. Start a new table to order."}
                    args["session_id"] = self.session_id
                result = fn(**args)
                if name in ("add_item_to_order", "add_combo_to_order") and result.get("ok"):
                    order_items = result.get("order", {}).get("items", [])
                    target_name = str(args.get("item_name", args.get("combo_name", ""))).casefold()
                    ordered_item = next(
                        (item for item in reversed(order_items)
                         if item["name"].casefold() == target_name),
                        None,
                    )
                    if ordered_item:
                        allergens = db.get_order_item_allergens(ordered_item["line_id"])
                        if allergens.get("ok"):
                            result["allergens"] = allergens["allergens"]
                if (name == "add_item_to_order" and not result.get("ok")
                        and any(word in result.get("error", "").casefold()
                                for word in ("out of stock", "available", "only "))):
                    result["alternatives"] = db.find_available_alternatives(
                        args.get("item_name", ""), args.get("qty", 1)
                    ).get("alternatives", [])
                if name in MUTATING and result.get("ok") and self.state == REVIEW:
                    self._go(TAKING_ORDER, "order edited during review")
            else:
                result = {"ok": False, "error": f"Unknown tool '{name}'."}
        except (TypeError, ValueError, KeyError) as e:
            result = {"ok": False, "error": f"Bad arguments: {e}"}
        self._log("tool_call", tool=name, arguments=args, result=result)
        self._log("db_state", order=db.view_current_order(self.session_id))
        return result
