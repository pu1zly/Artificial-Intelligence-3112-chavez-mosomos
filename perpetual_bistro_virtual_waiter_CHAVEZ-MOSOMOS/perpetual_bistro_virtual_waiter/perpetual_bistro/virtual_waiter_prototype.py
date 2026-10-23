"""Perpetual Bistro virtual waiter. Run: python virtual_waiter_prototype.py"""
import argparse
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import db_tools as db
from fsm_agent import (
    WaiterAgent, allergen_filter_options, detect_menu_diet_filter, is_menu_browse_request,
    menu_filter_key, menu_filter_label,
)


COLORS = {
    "background": "#4a0020",
    "panel": "#5f0029",
    "panel_soft": "#7b1238",
    "paper": "#fbf7ef",
    "paper_ink": "#3b0a1b",
    "line": "#8c2c52",
    "green": "#fccc0c",
    "green_ink": "#4a0020",
    "gold": "#e5c785",
    "muted": "#e3c3cf",
}
STATE_LABELS = {
    "STATE_GREETING": "Welcome",
    "STATE_TAKING_ORDER": "Taking your order",
    "STATE_REVIEW": "Review your order",
    "STATE_CONFIRMED": "Order confirmed",
}


class WaiterApp:
    def __init__(self, root, agent):
        self.root = root
        self.agent = agent
        self.busy = False
        self._agent_lock = threading.Lock()
        self.completed_actions = queue.Queue()
        self.connection_results = queue.Queue()
        self.shown_receipt_id = None
        self.chat_bubbles = []
        self.chat_follow_latest = True
        self.model_name = None
        self.active_menu_filter = "All items"
        self.menu_window = None
        self.menu_filter = None
        self.menu_refresh = None
        self.menu_notice = None
        self.menu_status_label = None
        self.activity_window = None
        self.worker_thread = None
        self.connection_thread = None
        self._poll_actions_id = None
        self._poll_connection_id = None

        root.title("Perpetual Bistro | Virtual Waiter")
        root.geometry("1120x760")
        root.minsize(760, 580)
        root.configure(bg=COLORS["background"])
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._styles()
        self._layout()
        self.render()
        self._poll_actions_id = self.root.after(50, self._poll_actions)
        self._poll_connection_id = self.root.after(150, self._poll_connection)
        self._connect_in_background()

    def _styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background=COLORS["background"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("Paper.TFrame", background=COLORS["paper"])
        style.configure("Title.TLabel", background=COLORS["background"], foreground="#f2f1eb",
                        font=("Georgia", 24, "bold"))
        style.configure("Eyebrow.TLabel", background=COLORS["background"], foreground=COLORS["gold"],
                        font=("Consolas", 10))
        style.configure("Heading.TLabel", background=COLORS["panel"], foreground="#f2f1eb",
                        font=("Georgia", 17, "bold"))
        style.configure("Muted.TLabel", background=COLORS["panel"], foreground=COLORS["muted"],
                        font=("Segoe UI", 9))
        style.configure("Model.TLabel", background=COLORS["panel"], foreground=COLORS["gold"],
                        font=("Segoe UI", 9))
        style.configure("TEntry", fieldbackground="#3a0018", foreground="#f2f1eb",
                        insertcolor="#f2f1eb", padding=9, font=("Segoe UI", 11))
        style.configure("State.TLabel", background=COLORS["background"], foreground=COLORS["muted"],
                        font=("Consolas", 10))
        style.configure("PaperHeading.TLabel", background=COLORS["paper"], foreground=COLORS["paper_ink"],
                        font=("Georgia", 19, "bold"))
        style.configure("PaperText.TLabel", background=COLORS["paper"], foreground=COLORS["paper_ink"],
                        font=("Segoe UI", 10))
        style.configure("Total.TLabel", background=COLORS["paper"], foreground=COLORS["paper_ink"],
                        font=("Segoe UI", 12, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 7))
        style.configure("Primary.TButton", background=COLORS["green"], foreground=COLORS["green_ink"],
                        font=("Segoe UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#ffdc4a"), ("disabled", "#8a6a74")])

    def _layout(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, style="App.TFrame", padding=(24, 16, 24, 12))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="A BETTER MEAL STARTS HERE", style="Eyebrow.TLabel").pack(anchor="w")
        ttk.Label(header, text="Perpetual Bistro", style="Title.TLabel").pack(anchor="w")
        self.table_label = ttk.Label(header, text="Local service | Table -", style="State.TLabel")
        self.table_label.place(relx=1.0, rely=0.58, anchor="e")

        content = ttk.Frame(self.root, style="App.TFrame", padding=(16, 0, 16, 14))
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=0, minsize=380)
        content.rowconfigure(0, weight=1)
        self._conversation_panel(content)
        self._order_panel(content)
        self._bottom_bar()

    def _conversation_panel(self, parent):
        panel = ttk.Frame(parent, style="Panel.TFrame", padding=18)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(4, weight=1)

        conversation_header = ttk.Frame(panel, style="Panel.TFrame")
        conversation_header.grid(row=0, column=0, sticky="ew")
        conversation_header.columnconfigure(0, weight=1)
        ttk.Label(conversation_header, text="Conversation", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.activity_button = ttk.Button(conversation_header, text="Activity", command=self._show_activity)
        self.activity_button.grid(row=0, column=1, sticky="e", padx=(0, 6))
        self.reconnect_button = ttk.Button(conversation_header, text="Reconnect", command=self._connect_in_background)
        self.reconnect_button.grid(row=0, column=2, sticky="e")
        ttk.Label(panel, text="Ask about the menu or tell us what you would like",
                  style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 10))
        self.model_status = ttk.Label(panel, text="Checking Ollama...", style="Model.TLabel")
        self.model_status.configure(wraplength=620, justify="left")
        self.model_status.grid(row=2, column=0, sticky="w", pady=(0, 8))
        self.fsm_label = ttk.Label(panel, text="", style="Muted.TLabel", wraplength=620)
        self.fsm_label.grid(row=3, column=0, sticky="w", pady=(0, 7))

        chat_area = ttk.Frame(panel, style="Panel.TFrame")
        chat_area.grid(row=4, column=0, sticky="nsew")
        chat_area.columnconfigure(0, weight=1)
        chat_area.rowconfigure(0, weight=1)
        self.chat = tk.Canvas(chat_area, background=COLORS["panel"], highlightthickness=0, bd=0)
        self.chat.grid(row=0, column=0, sticky="nsew")
        chat_scrollbar = ttk.Scrollbar(chat_area, orient="vertical", command=self._scroll_chatbar)
        chat_scrollbar.grid(row=0, column=1, sticky="ns")
        self.chat.configure(yscrollcommand=chat_scrollbar.set)
        self.chat_frame = ttk.Frame(self.chat, style="Panel.TFrame")
        self.chat_window = self.chat.create_window((0, 0), window=self.chat_frame, anchor="nw")
        self.root.bind_class("ChatScroll", "<MouseWheel>", self._scroll_chat)
        self.root.bind_class("ChatScroll", "<Button-4>", self._scroll_chat)
        self.root.bind_class("ChatScroll", "<Button-5>", self._scroll_chat)
        self.chat_frame.bind("<Configure>", self._update_chat_scroll)
        self.chat.bind("<Configure>", self._resize_chat)
        self._enable_chat_scroll(self.chat)
        self._enable_chat_scroll(self.chat_frame)

        composer = ttk.Frame(panel, style="Panel.TFrame")
        composer.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        composer.columnconfigure(0, weight=1)
        self.message = ttk.Entry(composer)
        self.message.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.message.bind("<Return>", self._submit_event)
        self.message.bind("<KP_Enter>", self._submit_event)
        self.send_button = ttk.Button(composer, text="Send", style="Primary.TButton", command=self._submit)
        self.send_button.grid(row=0, column=1)

    def _order_panel(self, parent):
        panel = ttk.Frame(parent, style="Paper.TFrame", padding=18)
        panel.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=1)

        heading = ttk.Frame(panel, style="Paper.TFrame")
        heading.grid(row=0, column=0, sticky="ew")
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text="Your tab", style="PaperHeading.TLabel").grid(row=0, column=0, sticky="w")
        self.menu_button = ttk.Button(heading, text="Browse menu", command=self._show_menu)
        self.menu_button.grid(row=0, column=1, sticky="e")
        self.order_table = ttk.Label(panel, text="Waiting for your table number", style="PaperText.TLabel")
        self.order_table.grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Label(panel, text="Live order | Service fee 10%", style="PaperText.TLabel").grid(
            row=2, column=0, sticky="w", pady=(2, 7)
        )

        self.cart = ttk.Frame(panel, style="Paper.TFrame")
        self.cart.grid(row=3, column=0, sticky="nsew")
        self.cart.columnconfigure(0, weight=1)

        ttk.Separator(panel).grid(row=4, column=0, sticky="ew", pady=(7, 5))
        ttk.Label(panel, text="Pairs well with", style="PaperText.TLabel",
                  font=("Segoe UI", 10, "bold")).grid(row=5, column=0, sticky="w", pady=(0, 3))
        self.recommendations = ttk.Frame(panel, style="Paper.TFrame")
        self.recommendations.grid(row=6, column=0, sticky="ew")
        self.recommendations.columnconfigure(0, weight=1)
        self.totals = ttk.Frame(panel, style="Paper.TFrame", padding=(0, 10, 0, 0))
        self.totals.grid(row=7, column=0, sticky="ew")
        self.totals.columnconfigure(0, weight=1)

    def _bottom_bar(self):
        bar = ttk.Frame(self.root, style="App.TFrame", padding=(24, 10, 24, 16))
        bar.grid(row=2, column=0, sticky="ew")
        self.state_label = ttk.Label(bar, text="Welcome", style="State.TLabel")
        self.state_label.pack(side="left", expand=True, anchor="w")
        self.review_button = ttk.Button(bar, text="Review order", command=self._review)
        self.review_button.pack(side="left", padx=(0, 8))
        self.confirm_button = ttk.Button(bar, text="Confirm order", style="Primary.TButton",
                                         command=self._confirm)
        self.confirm_button.pack(side="left")

    def _submit_event(self, _event):
        self._submit()
        return "break"

    def _submit(self):
        text = self.message.get().strip()
        if not text or self.busy:
            return
        self.message.delete(0, tk.END)
        requested_filter = detect_menu_diet_filter(text)
        if requested_filter:
            self._apply_menu_filter(menu_filter_label(requested_filter))
        elif re.search(r"\b(?:full\s+menu|entire\s+menu|all\s+items|whole\s+menu)\b", text, re.I):
            self._apply_menu_filter("All items")
        if is_menu_browse_request(text):
            self._show_menu()
        self._run_agent(lambda: self.agent.handle_user(text), user_text=text, status="The waiter is preparing a reply...")

    def _apply_menu_filter(self, filter_name):
        self.active_menu_filter = filter_name
        if self.menu_filter is not None and self.menu_window is not None and self.menu_window.winfo_exists():
            self.menu_filter.set(filter_name)
            if self.menu_refresh:
                self.menu_refresh()

    def _review(self):
        self._run_agent(self.agent.go_review, status="Preparing your order summary...")

    def _confirm(self):
        self._run_agent(self.agent.confirm, status="Confirming your order...")

    def _adjust(self, line_id, delta):
        def adjust():
            result = self.agent.adjust(line_id, delta)
            if not result["ok"]:
                raise ValueError(result["error"])
        self._run_agent(adjust, status="Updating your tab...")

    def _add_menu_item(self, item_name):
        if not self.agent.session_id:
            messagebox.showinfo(
                "Table number needed",
                "Please enter your table number in the chat first so we know where to serve your order.",
                parent=self.menu_window if (self.menu_window and self.menu_window.winfo_exists()) else self.root,
            )
            self.message.focus_set()
            return
        if self.agent.state == "STATE_CONFIRMED":
            messagebox.showinfo(
                "Order confirmed",
                "This order is already confirmed. Click 'Start a new table' on the receipt to place a new order.",
                parent=self.menu_window if (self.menu_window and self.menu_window.winfo_exists()) else self.root,
            )
            return

        def add():
            result = self.agent._run_tool("add_item_to_order", {"item_name": item_name, "qty": 1})
            if not result["ok"]:
                raise ValueError(result["error"])
            feedback = result["message"]
            allergens = result.get("allergens")
            if allergens:
                feedback += (f" Allergen information: {allergens}. "
                             "Please verify ingredients and cross-contact with restaurant staff.")
            self.agent.say(feedback)
            if self.agent.state == "STATE_REVIEW":
                self.agent._go("STATE_TAKING_ORDER", "order edited during review")
        self._run_agent(add, status=f"Adding {item_name}...")

    def _add_combo(self, combo_name, side_name, drink_name):
        if not self.agent.session_id:
            messagebox.showinfo(
                "Table number needed",
                "Please enter your table number in the chat first so we know where to serve your order.",
                parent=self.menu_window if (self.menu_window and self.menu_window.winfo_exists()) else self.root,
            )
            self.message.focus_set()
            return
        if self.agent.state == "STATE_CONFIRMED":
            messagebox.showinfo(
                "Order confirmed",
                "This order is already confirmed. Click 'Start a new table' on the receipt to place a new order.",
                parent=self.menu_window if (self.menu_window and self.menu_window.winfo_exists()) else self.root,
            )
            return

        def add():
            result = self.agent._run_tool("add_combo_to_order", {
                "combo_name": combo_name, "side_name": side_name, "drink_name": drink_name, "qty": 1,
            })
            if not result["ok"]:
                raise ValueError(result["error"])
            feedback = result["message"]
            allergens = result.get("allergens")
            if allergens:
                feedback += (f" Allergen information: {allergens}. "
                             "Please verify ingredients and cross-contact with restaurant staff.")
            self.agent.say(feedback)
            if self.agent.state == "STATE_REVIEW":
                self.agent._go("STATE_TAKING_ORDER", "order edited during review")
        self._run_agent(add, status=f"Adding {combo_name}...")

    def _upgrade_combo(self, line_id, combo_name, combo_id=None):
        self._run_agent(
            lambda: self.agent.begin_combo_upgrade(line_id, combo_name, combo_id),
            status=f"Choosing sides for {combo_name}...",
        )

    def _show_menu(self):
        if self.menu_window is not None and self.menu_window.winfo_exists():
            self.menu_window.deiconify()
            self.menu_window.lift()
            if self.menu_filter is not None and self.menu_filter.get() != self.active_menu_filter:
                self.menu_filter.set(self.active_menu_filter)
            if self.menu_refresh:
                self.menu_refresh()
            return self.menu_window
        window = tk.Toplevel(self.root)
        window.title("Perpetual Bistro | Menu")
        window.geometry("950x630")
        window.minsize(680, 440)
        window.configure(bg=COLORS["background"])
        window.transient(self.root)
        body = ttk.Frame(window, style="App.TFrame", padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Choose your meal", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Meal combos include the main, French fries, and iced tea.",
                  style="State.TLabel").pack(anchor="w", pady=(3, 4))
        self.menu_notice = ttk.Label(body, text="", style="State.TLabel", foreground=COLORS["gold"])
        self.menu_notice.pack(anchor="w", pady=(0, 8))
        filter_row = ttk.Frame(body, style="App.TFrame")
        filter_row.pack(fill="x", pady=(0, 8))
        ttk.Label(filter_row, text="Dietary filter:", style="State.TLabel").pack(side="left")
        filter_values = ["All items", "Vegetarian", "Vegan", *allergen_filter_options()]
        if self.active_menu_filter not in filter_values:
            filter_values.append(self.active_menu_filter)
        dietary_filter = ttk.Combobox(filter_row, state="readonly", values=filter_values, width=29)
        dietary_filter.set(self.active_menu_filter)
        dietary_filter.pack(side="left", padx=(8, 10))
        ttk.Label(filter_row, text="Allergen filters use listed data; verify with staff.",
                  style="State.TLabel").pack(side="left")
        self.menu_window = window
        self.menu_filter = dietary_filter
        tabs = ttk.Notebook(body)
        tabs.pack(fill="both", expand=True)
        combo_tab = ttk.Frame(tabs, padding=10)
        tabs.add(combo_tab, text="Combos")
        combo_content = self._scrollable_menu(combo_tab)
        ala_carte = ttk.Frame(tabs, padding=10)
        tabs.add(ala_carte, text="A la carte")
        ala_carte_content = self._scrollable_menu(ala_carte)

        footer = ttk.Frame(body, style="App.TFrame", padding=(0, 10, 0, 0))
        footer.pack(fill="x")
        self.menu_status_label = ttk.Label(footer, text="", style="State.TLabel")
        self.menu_status_label.pack(side="left")
        ttk.Button(footer, text="Close menu", command=window.destroy).pack(side="right")

        def refresh_menu(_event=None):
            self.active_menu_filter = dietary_filter.get()
            if self.menu_notice and self.menu_notice.winfo_exists():
                if not self.agent.session_id:
                    notice = "Please enter your table number in the chat window to start ordering."
                elif self.agent.state == "STATE_CONFIRMED":
                    notice = "This order is confirmed. Start a new table in the main window to order more items."
                elif self.busy:
                    notice = "Updating order, please wait..."
                else:
                    notice = f"Connected to Table {self.agent.table_no} | Click 'Add' to place items on your tab."
                self.menu_notice.configure(text=notice)
            for content in (combo_content, ala_carte_content):
                for child in content.winfo_children():
                    child.destroy()
            filter_key = menu_filter_key(dietary_filter.get())
            allergen_filter = filter_key.split(":", 1)[1] if filter_key.startswith("allergen-free:") else None
            if filter_key == "all":
                menu_items = db.get_menu_by_category()["items"]
            else:
                menu_items = db.get_menu_by_dietary_filter(filter_key)["items"]
            combos = [item for item in menu_items if item["is_combo"]]
            ala_items = [item for item in menu_items if not item["is_combo"]]
            self._render_menu_items(
                combo_content, combos, "No available combos match this filter.",
                allergen_filter=allergen_filter,
            )
            if not ala_items:
                ttk.Label(ala_carte_content, text="No available menu items match this filter.",
                          style="PaperText.TLabel", wraplength=720).pack(anchor="w", pady=12)
            else:
                for category in ("Mains", "Pasta", "Sides", "Drinks", "Desserts"):
                    category_items = [item for item in ala_items if item["category"] == category]
                    if category_items:
                        ttk.Label(ala_carte_content, text=category, style="PaperHeading.TLabel").pack(
                            anchor="w", pady=(9, 3)
                        )
                        self._render_menu_items(
                            ala_carte_content, category_items,
                            allergen_filter=allergen_filter,
                        )
            if self.menu_status_label and self.menu_status_label.winfo_exists():
                self.menu_status_label.configure(
                    text=f"{len(combos)} combo(s), {len(ala_items)} a la carte item(s)"
                )

        dietary_filter.bind("<<ComboboxSelected>>", refresh_menu)
        self.menu_refresh = refresh_menu
        window.bind("<Destroy>", lambda event: self._clear_menu_window(event, window))
        refresh_menu()
        return window

    def _clear_menu_window(self, event, window):
        if event.widget is window:
            self.menu_window = None
            self.menu_filter = None
            self.menu_refresh = None
            self.menu_notice = None
            self.menu_status_label = None

    def _render_menu_items(self, content, items, empty_message="", allergen_filter=None):
        if not items:
            if empty_message:
                ttk.Label(content, text=empty_message, style="PaperText.TLabel", wraplength=720).pack(
                    anchor="w", pady=12
                )
            return
        for item in items:
            self._menu_row(content, item, allergen_filter)

    def _scrollable_menu(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        canvas = tk.Canvas(parent, background=COLORS["paper"], highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        content = ttk.Frame(canvas, style="Paper.TFrame")
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))

        def _on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<MouseWheel>", _on_wheel)
        content.bind("<MouseWheel>", _on_wheel)
        content.canvas_ref = canvas
        return content

    def _menu_row(self, parent, item, allergen_filter=None):
        row = ttk.Frame(parent, style="Paper.TFrame", padding=(5, 7))
        row.pack(fill="x")
        row.columnconfigure(0, weight=1)
        name = item["name"]
        if item["is_combo"]:
            details = f"Includes {', '.join(item['included'])}"
            if item["savings"] > 0:
                details += f" | Save {item['savings']:.2f} PHP"
            if item.get("available_qty", 0) > 0:
                details += f" | {item['available_qty']} available"
            else:
                details += " | Out of stock"
        else:
            details = f"{item['category']} | {item['available_qty']} available"
        if item["dietary_tags"]:
            details += f" | {', '.join(item['dietary_tags'])}"
        if item["allergens"]:
            details += f" | Allergens: {item['allergens']}"
        ttk.Label(row, text=name, style="PaperText.TLabel", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(row, text=f"{item['price']:.2f} PHP", style="PaperText.TLabel", anchor="e").grid(
            row=0, column=1, sticky="e", padx=8
        )
        detail_row = 1
        side_choice = drink_choice = None
        side_labels = {}
        drink_labels = {}
        can_order = (
            self.agent.session_id is not None
            and self.agent.state != "STATE_CONFIRMED"
            and not self.busy
        )
        if item["is_combo"]:
            options = db.get_combo_options(item["id"])
            if not options.get("ok"):
                return
            side_options = [option for option in options["sides"] if option["available_qty"] > 0]
            drink_options = [option for option in options["drinks"] if option["available_qty"] > 0]
            if allergen_filter:
                side_options = [
                    option for option in side_options
                    if allergen_filter not in {
                        value.strip().casefold() for value in option["allergens"].split(",")
                    }
                ]
                drink_options = [
                    option for option in drink_options
                    if allergen_filter not in {
                        value.strip().casefold() for value in option["allergens"].split(",")
                    }
                ]
            side_labels = {
                (f"{option['name']} ({'+' if option['price_adjustment'] > 0 else ''}"
                 f"PHP {option['price_adjustment']:.2f})"
                 if option["price_adjustment"] != 0 else option["name"]): option["name"]
                for option in side_options
            }
            drink_labels = {
                (f"{option['name']} ({'+' if option['price_adjustment'] > 0 else ''}"
                 f"PHP {option['price_adjustment']:.2f})"
                 if option["price_adjustment"] != 0 else option["name"]): option["name"]
                for option in drink_options
            }
            side_values, drink_values = list(side_labels), list(drink_labels)
            ttk.Label(row, text="Side", style="PaperText.TLabel").grid(
                row=detail_row, column=0, sticky="w", pady=(5, 0)
            )
            ttk.Label(row, text="Drink", style="PaperText.TLabel").grid(
                row=detail_row, column=1, sticky="w", pady=(5, 0)
            )
            side_choice = ttk.Combobox(row, state="readonly", values=side_values, width=22)
            drink_choice = ttk.Combobox(row, state="readonly", values=drink_values, width=22)
            if side_labels:
                side_choice.set(next((label for label, value in side_labels.items() if value == "French Fries"),
                                     side_values[0]))
            if drink_labels:
                drink_choice.set(next((label for label, value in drink_labels.items() if value == "Iced Tea"),
                                      drink_values[0]))
            side_choice.grid(row=detail_row + 1, column=0, sticky="ew", pady=(1, 5))
            drink_choice.grid(row=detail_row + 1, column=1, sticky="ew", pady=(1, 5))
            detail_row += 2
        ttk.Label(row, text=details, style="PaperText.TLabel", foreground="#7d5f6b").grid(
            row=detail_row, column=0, columnspan=2, sticky="w"
        )

        if item["is_combo"]:
            combo_available = bool(options.get("main_available_qty", 0) > 0 and side_values and drink_values)
            btn_state = "normal" if combo_available and can_order else "disabled"
            btn_text = "Add" if combo_available else "Out of stock"
            ttk.Button(
                row, text=btn_text, state=btn_state,
                command=lambda n=name, s=side_choice, d=drink_choice, sl=side_labels, dl=drink_labels:
                    self._on_click_add_combo(n, s, d, sl, dl),
            ).grid(row=0, column=2, rowspan=detail_row + 1, padx=(8, 2))
        else:
            btn_state = "normal" if item["available"] and can_order else "disabled"
            btn_text = "Add" if item["available"] else "Out of stock"
            ttk.Button(
                row, text=btn_text, state=btn_state,
                command=lambda n=name: self._add_menu_item(n),
            ).grid(
                row=0, column=2, rowspan=2, padx=(8, 2)
            )
        ttk.Separator(parent).pack(fill="x")

        if hasattr(parent, "canvas_ref"):
            c = parent.canvas_ref
            row.bind("<MouseWheel>", lambda e: c.yview_scroll(int(-e.delta / 120), "units"), add="+")
            for w in row.winfo_children():
                if not isinstance(w, (ttk.Combobox, ttk.Button)):
                    w.bind("<MouseWheel>", lambda e: c.yview_scroll(int(-e.delta / 120), "units"), add="+")

    def _on_click_add_combo(self, name, side_widget, drink_widget, side_labels, drink_labels):
        side = side_labels.get(side_widget.get()) if side_widget else None
        drink = drink_labels.get(drink_widget.get()) if drink_widget else None
        if not side or not drink:
            messagebox.showwarning(
                "Incomplete selection",
                f"Please choose both a side and a drink for the {name}.",
                parent=self.menu_window if (self.menu_window and self.menu_window.winfo_exists()) else self.root,
            )
            return
        self._add_combo(name, side, drink)

    def _run_agent(self, action, user_text=None, status="Updating your order..."):
        if self.busy:
            return
        self.busy = True
        if user_text:
            self._append_message("You", user_text, "user")
        self.state_label.configure(text=status)
        self._set_controls()
        if self.menu_window is not None and self.menu_window.winfo_exists() and self.menu_refresh:
            self.menu_refresh()

        def work():
            try:
                with self._agent_lock:
                    action()
                    snapshot = self.agent.snapshot()
                self.completed_actions.put((snapshot, None))
            except Exception as error:
                self.completed_actions.put((None, error))

        thread = threading.Thread(target=work, daemon=True)
        self.worker_thread = thread
        thread.start()

    def wait_for_idle(self, timeout=5.0):
        if getattr(self, "worker_thread", None) and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=timeout)
        if getattr(self, "connection_thread", None) and self.connection_thread.is_alive():
            self.connection_thread.join(timeout=timeout)

    def cleanup(self):
        if getattr(self, "_poll_actions_id", None):
            try:
                self.root.after_cancel(self._poll_actions_id)
            except Exception:
                pass
            self._poll_actions_id = None
        if getattr(self, "_poll_connection_id", None):
            try:
                self.root.after_cancel(self._poll_connection_id)
            except Exception:
                pass
            self._poll_connection_id = None
        if getattr(self, "menu_window", None) and self.menu_window.winfo_exists():
            try:
                self.menu_window.destroy()
            except Exception:
                pass
        if getattr(self, "activity_window", None) and self.activity_window.winfo_exists():
            try:
                self.activity_window.destroy()
            except Exception:
                pass

    def _on_close(self):
        self.cleanup()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _poll_actions(self):
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
            return
        while True:
            try:
                snapshot, error = self.completed_actions.get_nowait()
            except queue.Empty:
                break
            if error:
                self._fail(error)
            else:
                self._finish(snapshot)
        try:
            if self.root.winfo_exists():
                self._poll_actions_id = self.root.after(50, self._poll_actions)
        except Exception:
            pass

    def _finish(self, snapshot):
        self.busy = False
        self.render(snapshot)

    def _fail(self, error):
        self.busy = False
        self._append_message("Waiter", str(error), "error")
        self.state_label.configure(text=STATE_LABELS.get(self.agent.state, "Ready"))
        self._set_controls()
        if self.menu_window is not None and self.menu_window.winfo_exists() and self.menu_refresh:
            self.menu_refresh()

    def _append_message(self, speaker, text, tag):
        self._append_bubble(speaker, text, tag)

    def _append_bubble(self, speaker, text, role):
        self.chat_follow_latest = True
        is_user = role == "user"
        is_error = role == "error"
        color = COLORS["green"] if is_user else COLORS["panel_soft"]
        foreground = COLORS["green_ink"] if is_user else ("#ffb6a8" if is_error else "#f2f1eb")
        row = tk.Frame(self.chat_frame, bg=COLORS["panel"])
        row.pack(fill="x", padx=8, pady=(3, 8))
        self._enable_chat_scroll(row)
        bubble = tk.Frame(row, bg=color, padx=13, pady=9)
        bubble.pack(side="right" if is_user else "left", anchor="e" if is_user else "w")
        self._enable_chat_scroll(bubble)
        heading = tk.Label(bubble, text=speaker, bg=color, fg=foreground,
                           font=("Segoe UI", 9, "bold"), anchor="e" if is_user else "w")
        heading.pack(fill="x", anchor="e" if is_user else "w")
        self._enable_chat_scroll(heading)
        message = tk.Label(bubble, text=text, bg=color, fg=foreground,
                           font=("Segoe UI", 11), justify="left", anchor="w")
        message.pack(fill="x", pady=(3, 0))
        self._enable_chat_scroll(message)
        self.chat_bubbles.append((bubble, message))
        self._resize_bubbles()
        self._schedule_chat_scroll()

    def _update_chat_scroll(self, _event=None):
        self.chat.configure(scrollregion=self.chat.bbox("all"))
        if self.chat_follow_latest:
            self._schedule_chat_scroll()

    def _schedule_chat_scroll(self):
        try:
            if self.root.winfo_exists():
                self.root.after_idle(self._scroll_chat_to_latest)
        except Exception:
            pass

    def _scroll_chat_to_latest(self):
        try:
            if self.root.winfo_exists():
                self.chat.configure(scrollregion=self.chat.bbox("all"))
                if self.chat_follow_latest:
                    self.chat.yview_moveto(1.0)
        except Exception:
            pass

    def _scroll_chatbar(self, *args):
        self.chat_follow_latest = False
        self.chat.yview(*args)

    def _resize_chat(self, event):
        self.chat.itemconfigure(self.chat_window, width=event.width)
        self._resize_bubbles()

    def _resize_bubbles(self):
        width = max(180, min(680, int(self.chat.winfo_width() * 0.76)))
        for bubble, message in self.chat_bubbles:
            message.configure(wraplength=width - 26)

    def _scroll_chat(self, event):
        self.chat_follow_latest = False
        if getattr(event, "num", None) == 4:
            direction = -1
        elif getattr(event, "num", None) == 5:
            direction = 1
        else:
            direction = -1 if event.delta > 0 else 1
            if abs(event.delta) >= 120:
                self.chat.yview_scroll(direction * max(1, abs(event.delta) // 120), "units")
                return "break"
        self.chat.yview_scroll(direction * 3, "units")
        return "break"

    @staticmethod
    def _enable_chat_scroll(widget):
        tags = widget.bindtags()
        if "ChatScroll" not in tags:
            widget.bindtags(("ChatScroll",) + tags)

    def _connect_in_background(self):
        if getattr(self, "checking_connection", False):
            return
        self.checking_connection = True
        self.model_status.configure(text="Connecting to Ollama...")
        self.reconnect_button.configure(state="disabled")

        def check():
            try:
                model = self.agent.connect()
                self.connection_results.put((model, None))
            except Exception as error:
                self.connection_results.put((None, error))

        thread = threading.Thread(target=check, daemon=True)
        self.connection_thread = thread
        thread.start()

    def _poll_connection(self):
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
            return
        try:
            model, error = self.connection_results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.checking_connection = False
            self.reconnect_button.configure(state="normal")
            if error:
                self.model_name = None
                self.model_status.configure(text=str(error), foreground="#ffb6a8")
            else:
                self.model_name = model
                self.model_status.configure(text=f"Connected to Ollama | {model}", foreground=COLORS["green"])
        try:
            if self.root.winfo_exists():
                self._poll_connection_id = self.root.after(150, self._poll_connection)
        except Exception:
            pass

    def render(self, snapshot=None):
        snapshot = snapshot or self.agent.snapshot()
        self.table_label.configure(text=f"Local service | Table {snapshot['table_no'] or '-'}")
        self.order_table.configure(
            text=f"Table {snapshot['table_no']}" if snapshot["table_no"] else "Waiting for your table number"
        )
        self.state_label.configure(text=STATE_LABELS.get(snapshot["state"], "Ready"))
        steps = ("Welcome", "Taking your order", "Review your order", "Order confirmed")
        active_step = {
            "STATE_GREETING": 0, "STATE_TAKING_ORDER": 1,
            "STATE_REVIEW": 2, "STATE_CONFIRMED": 3,
        }.get(snapshot["state"], 0)
        self.fsm_label.configure(
            text="FSM: " + "  >  ".join(
                f"[{step}]" if index == active_step else step for index, step in enumerate(steps)
            )
        )

        for child in self.chat_frame.winfo_children():
            child.destroy()
        self.chat_bubbles.clear()
        for message in snapshot["transcript"]:
            speaker = "You" if message["role"] == "user" else "Perpetual Bistro"
            self._append_bubble(speaker, message["text"], message["role"])
        self.chat_follow_latest = True
        self._update_chat_scroll()
        self._schedule_chat_scroll()

        for child in self.cart.winfo_children():
            child.destroy()
        for child in self.totals.winfo_children():
            child.destroy()
        for child in self.recommendations.winfo_children():
            child.destroy()

        order = snapshot["order"]
        items = order["items"] if order else []
        if not items:
            ttk.Label(self.cart, text="Your tab is empty.\nAsk about the menu or tell us what you would like.",
                      style="PaperText.TLabel", wraplength=290, justify="left").grid(
                row=0, column=0, sticky="nw", pady=16
            )
        else:
            for index, item in enumerate(items):
                self._cart_row(index, item, snapshot["state"])
            self._total_row(0, "Subtotal", order["subtotal"])
            self._total_row(1, "Service fee (10%)", order["service_fee"])
            self._total_row(2, "Grand total", order["total"], total=True)

        self._render_recommendations(snapshot, items)
        self._set_controls(snapshot["state"], bool(items))
        if self.menu_window is not None and self.menu_window.winfo_exists() and self.menu_refresh:
            self.menu_refresh()
        receipt = snapshot["receipt"]
        if receipt and str(receipt["session_id"]) != self.shown_receipt_id:
            self.shown_receipt_id = str(receipt["session_id"])
            self.root.after(100, lambda receipt=receipt: self._show_receipt(receipt))

    def _render_recommendations(self, snapshot, items):
        if not items or not snapshot["order"] or snapshot["state"] == "STATE_CONFIRMED":
            ttk.Label(self.recommendations, text="Add a meal to see combo upgrades and extras.",
                      style="PaperText.TLabel", wraplength=325).grid(row=0, column=0, sticky="w", pady=3)
            return

        suggestions = []
        if self.agent.session_id:
            suggestions = db.get_combo_recommendations(self.agent.session_id)["recommendations"]
        row_index = 0
        for suggestion in suggestions:
            if not suggestion["available"]:
                continue
            text = (f"Upgrade +PHP {suggestion['upgrade_cost'] * suggestion['quantity']:.2f} | Save "
                    f"PHP {suggestion['savings'] * suggestion['quantity']:.2f}*")
            ttk.Label(self.recommendations, text=f"Make {suggestion['item_name']} a meal?",
                      style="PaperText.TLabel", wraplength=325).grid(
                row=row_index, column=0, sticky="w", pady=(3, 0)
            )
            ttk.Button(
                self.recommendations, text=text,
                state="disabled" if self.busy else "normal",
                command=lambda line_id=suggestion["line_id"], name=suggestion["combo_name"],
                               combo_id=suggestion["combo_id"]:
                    self._upgrade_combo(line_id, name, combo_id),
            ).grid(row=row_index + 1, column=0, sticky="ew", pady=(2, 5))
            row_index += 2

        if row_index:
            ttk.Label(self.recommendations, text="*Savings versus buying the included items separately.",
                      style="PaperText.TLabel", wraplength=325).grid(
                row=row_index, column=0, sticky="w", pady=(0, 5)
            )
            row_index += 1

        in_cart = {item["item_id"] for item in items}
        extras = [item for item in db.get_menu_by_category("Desserts")["items"]
                  if item["available"] and item["id"] not in in_cart][:2]
        if extras:
            ttk.Label(self.recommendations, text="Add a little something extra?",
                      style="PaperText.TLabel").grid(row=row_index, column=0, sticky="w", pady=(4, 0))
            row_index += 1
            for item in extras:
                button = ttk.Button(
                    self.recommendations,
                    text=f"+ {item['name']}  |  {item['price']:.2f} PHP",
                    state="disabled" if self.busy else "normal",
                    command=lambda name=item["name"]: self._add_menu_item(name),
                )
                button.grid(row=row_index, column=0, sticky="ew", pady=2)
                row_index += 1

        if not row_index:
            ttk.Label(self.recommendations, text="No extra suggestions right now.",
                      style="PaperText.TLabel").grid(row=0, column=0, sticky="w", pady=3)

    def _cart_row(self, index, item, state):
        row = ttk.Frame(self.cart, style="Paper.TFrame", padding=(0, 9, 0, 9))
        row.grid(row=index, column=0, sticky="ew")
        row.columnconfigure(0, weight=1)
        row.columnconfigure(2, weight=1)
        ttk.Label(row, text=item["name"], style="PaperText.TLabel",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        if item.get("side_name"):
            ttk.Label(row, text=f"Side: {item['side_name']}  |  Drink: {item['drink_name']}",
                      style="PaperText.TLabel", wraplength=300).grid(
                row=1, column=0, columnspan=3, sticky="w", pady=(3, 0)
            )
        controls = ttk.Frame(row, style="Paper.TFrame")
        controls.grid(row=0, column=1, padx=5)
        btn_state = "disabled" if state == "STATE_CONFIRMED" or self.busy else "normal"
        ttk.Button(controls, text="-", width=2, command=lambda: self._adjust(item["line_id"], -1),
                   state=btn_state).pack(side="left")
        ttk.Label(controls, text=str(item["quantity"]), style="PaperText.TLabel", width=3,
                  anchor="center").pack(side="left")
        ttk.Button(controls, text="+", width=2, command=lambda: self._adjust(item["line_id"], 1),
                   state=btn_state).pack(side="left")
        ttk.Button(controls, text="Remove", command=lambda: self._adjust(item["line_id"], -item["quantity"]),
                   state=btn_state).pack(side="left", padx=(4, 0))
        ttk.Label(row, text=f"{item['line_total']:.2f} PHP", style="PaperText.TLabel").grid(
            row=0, column=2, sticky="e"
        )
        if item["instructions"]:
            ttk.Label(row, text=item["instructions"], style="PaperText.TLabel",
                      wraplength=300).grid(row=2 if item.get("side_name") else 1,
                                           column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Separator(row).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))

    def _total_row(self, index, label, value, total=False):
        style = "Total.TLabel" if total else "PaperText.TLabel"
        ttk.Label(self.totals, text=label, style=style).grid(row=index, column=0, sticky="w", pady=2)
        ttk.Label(self.totals, text=f"{value:.2f} PHP", style=style).grid(row=index, column=1, sticky="e", pady=2)

    def _set_controls(self, state=None, has_items=None):
        state = state or self.agent.state
        if has_items is None:
            order = self.agent.snapshot()["order"]
            has_items = bool(order and order["items"])
        self.send_button.configure(state="disabled" if self.busy else "normal")
        self.message.configure(state="disabled" if self.busy else "normal")
        self.menu_button.configure(
            state="disabled" if self.busy else "normal"
        )
        self.review_button.configure(
            state="normal" if not self.busy and state == "STATE_TAKING_ORDER" and has_items else "disabled"
        )
        self.confirm_button.configure(state="normal" if not self.busy and state == "STATE_REVIEW" else "disabled")

    def _show_receipt(self, receipt):
        if not self.root.winfo_exists():
            return
        window = tk.Toplevel(self.root)
        window.title("Order confirmed")
        window.configure(bg=COLORS["paper"])
        window.transient(self.root)
        window.grab_set()
        body = ttk.Frame(window, style="Paper.TFrame", padding=24)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Order confirmed", style="PaperHeading.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"Perpetual Bistro | Table {receipt['table_no']}",
                  style="PaperText.TLabel").pack(anchor="w", pady=(5, 0))
        ttk.Label(body, text=f"Order #{receipt['session_id']}", style="PaperText.TLabel").pack(anchor="w")
        ttk.Separator(body).pack(fill="x", pady=12)
        for item in receipt["items"]:
            detail = f"{item['quantity']}x {item['name']}  {item['line_total']:.2f} PHP"
            if item.get("side_name"):
                detail += f"\n   Side: {item['side_name']} | Drink: {item['drink_name']}"
            if item["instructions"]:
                detail += f"\n   {item['instructions']}"
            ttk.Label(body, text=detail, style="PaperText.TLabel", justify="left").pack(anchor="w", pady=3)
        ttk.Separator(body).pack(fill="x", pady=12)
        ttk.Label(body, text=f"Subtotal  {receipt['subtotal']:.2f} PHP",
                  style="PaperText.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"Service fee  {receipt['service_fee']:.2f} PHP",
                  style="PaperText.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"TOTAL  {receipt['total']:.2f} PHP",
                  style="Total.TLabel").pack(anchor="w", pady=(5, 12))
        buttons = ttk.Frame(body, style="Paper.TFrame")
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Close receipt", command=window.destroy).pack(fill="x", pady=(0, 6))
        ttk.Button(buttons, text="Start a new table", style="Primary.TButton",
                   command=lambda: self._new_table(window)).pack(fill="x")

    def _show_activity(self):
        if self.activity_window is not None and self.activity_window.winfo_exists():
            self.activity_window.deiconify()
            self.activity_window.lift()
            return self.activity_window
        window = tk.Toplevel(self.root)
        self.activity_window = window
        window.bind("<Destroy>", lambda event: setattr(self, "activity_window", None) if event.widget is window else None)
        window.title("FSM and tool activity")
        window.geometry("820x420")
        window.transient(self.root)
        body = ttk.Frame(window, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        heading = ttk.Frame(body)
        heading.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Label(heading, text="Conversation workflow activity",
                  font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(heading, text="Recent state transitions, tool calls, and database outcomes.",
                  font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        columns = ("time", "state", "event", "detail")
        table = ttk.Treeview(body, columns=columns, show="headings")
        for column, title, width in (
                ("time", "Time", 145), ("state", "FSM state", 150),
                ("event", "Activity", 150), ("detail", "Details", 340)):
            table.heading(column, text=title)
            table.column(column, width=width, anchor="w")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scrollbar.set)
        table.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")
        for event in reversed(self.agent.log[-100:]):
            detail = event.get("reason") or event.get("text") or event.get("tool", "")
            result = event.get("result")
            if isinstance(result, dict):
                detail = result.get("message") or result.get("error") or detail
                if "to_state" in event:
                    detail = f"{event.get('from_state', '')} -> {event['to_state']}: {detail}"
            table.insert("", "end", values=(
                event.get("time", ""), event.get("state", "").replace("STATE_", ""),
                event.get("event", ""), str(detail)[:180],
            ))
        actions = ttk.Frame(body)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="Reset order & restore stock",
                   command=lambda: self._reset_from_activity(window)).pack(side="right")
        return window

    def _reset_from_activity(self, activity_window):
        if self.busy:
            return
        confirmed = messagebox.askyesno(
            "Reset restaurant data",
            "This permanently removes the saved order and conversation, then restores menu stock. Continue?",
            parent=activity_window,
            icon="warning",
        )
        if not confirmed:
            return

        activity_window.destroy()
        self.shown_receipt_id = None

        def reset_database():
            db.init_db(reset=True)
            self.agent.reset()

        self._run_agent(reset_database, status="Resetting the order and restoring menu stock...")

    def _new_table(self, receipt_window):
        receipt_window.destroy()
        self.shown_receipt_id = None
        self._run_agent(self.agent.reset, status="Starting a new table...")


def run_cli(agent):
    try:
        print(f"Connected to Ollama using {agent.connect()}")
    except ConnectionError as error:
        print(f"[!] {error}")
    print(agent.transcript[-1]["text"])
    while agent.state != "STATE_CONFIRMED":
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text:
            print("Waiter:", agent.handle_user(text), f"[{agent.state}]")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", action="store_true", help="run a plain terminal chat instead of the GUI")
    parser.add_argument("--reset-db", action="store_true", help="re-create restaurant.db (restores stock)")
    args = parser.parse_args()

    db.init_db(reset=args.reset_db)
    agent = WaiterAgent()
    if args.cli:
        run_cli(agent)
    else:
        root = tk.Tk()
        WaiterApp(root, agent)
        root.mainloop()


if __name__ == "__main__":
    main()
