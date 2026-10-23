# Perpetual Bistro - Stateful Virtual Waiter

## Windows quick start
Install Python with Tcl/Tk support and Ollama, then double-click `run_waiter.bat`. It installs the Python requirements, starts Ollama if needed, downloads a supported model if necessary, and opens the Tkinter ordering kiosk. To erase the saved order and restore stock, run `run_waiter.bat reset` and confirm the prompt.

## Setup (manual)
1. Install Ollama (https://ollama.com) and keep it running. On startup, the app checks your installed models and tests actual tool calling before selecting one; it prefers Qwen 3. If you have no models installed, `run_waiter.bat` downloads `qwen3:8B`.
2. `pip install -r requirements.txt`
3. `python virtual_waiter_prototype.py` to open the Tkinter GUI.
   - `--cli` runs a terminal chat | `--reset-db` restores stock | optionally set `OLLAMA_MODEL` to test a specific installed model

## Try it
"Table 4" -> "What pastas do you have?" -> "2 cheeseburgers, no onions" -> "Does the pesto penne have nuts?"
-> "Remove one cheeseburger" -> "Add grilled salmon x3" (only 2 in stock) -> "That's all" -> "Confirm".

## Meals and add-ons
The **Browse menu** button separates discounted meal combos from individual a la carte dishes. Burger, chicken,
veggie burger, and salmon meals include a side and drink; choose from currently available options. French fries and
iced tea are the included defaults. More expensive substitutions show their price difference and are reflected in the
cart, inventory reservation, allergen information, and checkout.
With an eligible main on the tab, choose **Make it a meal** to upgrade it while keeping its quantity and customizations.
The suggested upgrade shows its extra cost and savings versus buying the included items separately; the chat offer
uses those same database-calculated amounts. The kiosk also suggests available desserts as optional add-ons. Combo
ingredients reserve and deduct the same SQLite
inventory as a la carte orders.
After an a la carte item is added in chat, the waiter sends the add confirmation with its recorded allergen information
first, then sends the optional meal upgrade as a separate follow-up message.
Accepting that offer (or choosing **Make it a meal**) asks for an available side and drink before upgrading the
existing order line; the tab changes only after both selections are provided.

## Lab features and demonstration
The menu has vegetarian and vegan filters that rely only on explicitly tagged catalog data, plus allergen-free
filters for dairy, gluten, egg, soy, fish, nuts, sesame, and shellfish. Chat requests such as
"show me meals without gluten" update the menu filter; results are based on listed allergens, not a safety guarantee,
so staff should verify ingredients and cross-contact. Combo side and drink choices are filtered by their listed
allergens as well. Allergen answers come
from SQLite. If an item is unavailable, the agent offers only in-stock alternatives from its category. Closing the
Tkinter window also exits the `run_waiter.bat` launcher.
Customers can edit exact cart lines by quantity, remove customized lines, review and confirm, or cancel the whole
unconfirmed tab after a yes/no confirmation. An unrelated menu question during a pending cancellation keeps the
order safe and resumes the conversation.

The **FSM** indicator shows the active workflow stage. **Activity** displays recent state transitions, tool calls,
and database outcomes for debugging and for demonstrating function-to-SQLite binding. The Tkinter and CLI interfaces
both call the same `WaiterAgent.handle_user()` conversation API, keeping input/output separate from ordering logic.

Suggested class demo: ask for vegetarian options, add a veggie burger with a customization, request three grilled
salmon portions to trigger in-stock alternatives, edit a cart quantity, ask whether pesto contains nuts, review and
edit the tab, then confirm and show the receipt. In a second run, request full-order cancellation and demonstrate
that a menu question does not accidentally cancel it. The generated `sample_conversation_log.json` records a
confirmed sample with tool calls and state transitions.

Run the local regression checks with `python -m unittest discover -s tests -v`.

## Memory
The order, conversation, table number and FSM state are saved in `restaurant.db` after every step. Close and
re-run the program and the kiosk resumes where it left off. Use
`--reset-db` to wipe everything and start fresh (also restores stock).

## FSM
GREETING -> TAKING_ORDER <-> REVIEW -> CONFIRMED; a separately confirmed cancellation closes an open tab and
returns to GREETING. Editing in REVIEW returns to TAKING_ORDER.

## Files
`init_restaurant_db.sql`, `db_tools.py`, `fsm_agent.py`, `virtual_waiter_prototype.py` (Tkinter GUI),
`pair_collaboration_log.md`, `sample_conversation_log.json` (auto-saved on confirm).
