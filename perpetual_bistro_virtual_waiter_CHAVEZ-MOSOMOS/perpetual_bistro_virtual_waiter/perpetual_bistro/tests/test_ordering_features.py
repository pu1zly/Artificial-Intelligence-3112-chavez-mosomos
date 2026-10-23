import os
import sqlite3
import tempfile
import unittest

import db_tools as db
import fsm_agent


class OrderingFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.temp.name, "restaurant.db")
        fsm_agent.LOG_PATH = os.path.join(self.temp.name, "conversation.json")
        db.init_db()
        self.agent = fsm_agent.WaiterAgent()
        self.agent.handle_user("Table 7")
        self.session_id = self.agent.session_id

    def tearDown(self):
        self.temp.cleanup()

    def test_dietary_filter_only_returns_explicit_tags(self):
        result = db.get_menu_by_dietary_filter("vegetarian")
        names = {item["name"] for item in result["items"]}
        self.assertIn("Veggie Burger", names)
        self.assertIn("Veggie Burger Meal", names)
        self.assertNotIn("Chicken Inasal", names)
        self.assertEqual(db.get_menu_by_dietary_filter("vegan")["items"], [])

    def test_dairy_free_filter_excludes_listed_dairy_and_updates_chat_menu(self):
        result = db.get_menu_by_dietary_filter("dairy-free")
        names = {item["name"] for item in result["items"]}
        self.assertIn("Chicken Inasal Meal", names)
        self.assertIn("Veggie Burger Meal", names)
        self.assertNotIn("Bistro Burger Meal", names)
        self.assertNotIn("Mango Smoothie", names)
        self.assertTrue(all("dairy" not in item["allergens"].split(",") for item in result["items"]))

        reply = self.agent.handle_user("Can I see the meals without dairy?")
        self.assertIn("Chicken Inasal Meal", reply)
        self.assertIn("Veggie Burger Meal", reply)
        self.assertNotIn("Bistro Burger Meal", reply)
        self.assertIn("not a guarantee", reply)

        all_matches = self.agent.handle_user("Show me dairy-free options")
        self.assertIn("\n\nMains:\n", all_matches)
        self.assertIn("\n\nMeal combos:\n", all_matches)
        self.assertIn("\n  - Chicken Inasal (PHP 210.00)", all_matches)
        self.assertLess(all_matches.index("Meal combos:"), all_matches.index("Mains:"))

    def test_menu_filter_supports_any_listed_allergen(self):
        cases = {
            "Can I see meals without gluten?": "gluten",
            "Show me egg-free meals": "egg",
            "Which meals have no soy?": "soy",
            "List meals without peanuts": "nuts",
            "Can I see meals free of milk?": "dairy",
        }
        for request, allergen in cases.items():
            with self.subTest(request=request):
                self.assertEqual(
                    fsm_agent.detect_menu_diet_filter(request), f"allergen-free:{allergen}"
                )
        result = db.get_menu_by_dietary_filter("allergen-free:gluten")
        names = {item["name"] for item in result["items"]}
        self.assertIn("Chicken Inasal Meal", names)
        self.assertNotIn("Bistro Burger Meal", names)
        self.assertTrue(all(
            "gluten" not in {value.strip().casefold() for value in item["allergens"].split(",")}
            for item in result["items"] if not item["is_combo"]
        ))
        reply = self.agent.handle_user("Can I see meals without gluten?")
        self.assertIn("no gluten listed", reply.lower())
        self.assertIn("Chicken Inasal Meal", reply)

    def test_filter_label_round_trips_allergen_choice(self):
        label = fsm_agent.menu_filter_label("allergen-free:sesame")
        self.assertEqual(label, "No sesame (listed allergens)")
        self.assertEqual(fsm_agent.menu_filter_key(label), "allergen-free:sesame")

    def test_menu_filter_detection_does_not_change_item_allergen_questions(self):
        self.assertEqual(fsm_agent.detect_menu_diet_filter("show me dairy-free meals"), "allergen-free:dairy")
        self.assertEqual(fsm_agent.detect_menu_diet_filter("meals without dairy"), "allergen-free:dairy")
        self.assertIsNone(fsm_agent.detect_menu_diet_filter("I want a dairy-free cheeseburger"))
        self.assertIsNone(fsm_agent.detect_menu_diet_filter("does the cheeseburger have dairy?"))

    def test_full_menu_browse_request_detection(self):
        for text in (
            "Can I see the menu?",
            "Could I please view the full menu",
            "Show me the menu",
            "Open menu",
            "Let me browse the menu",
        ):
            with self.subTest(text=text):
                self.assertTrue(fsm_agent.is_menu_browse_request(text))
        self.assertFalse(fsm_agent.is_menu_browse_request("What drinks do you have?"))
        self.assertFalse(fsm_agent.is_menu_browse_request("Does the cheeseburger have dairy?"))

    def test_database_startup_migrates_existing_menu_schema(self):
        legacy_path = os.path.join(self.temp.name, "legacy.db")
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            "CREATE TABLE menu_items(id INTEGER PRIMARY KEY,name TEXT,category TEXT,price REAL,"
            "stock_qty INTEGER,allergens TEXT)"
        )
        connection.execute(
            "INSERT INTO menu_items VALUES (1,'Legacy dish','Mains',100,3,'')"
        )
        connection.commit()
        connection.close()
        db.DB_PATH = legacy_path
        db.init_db()
        with db.conn() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(menu_items)")}
            tags = connection.execute("SELECT dietary_tags FROM menu_items WHERE id=1").fetchone()[0]
        self.assertIn("dietary_tags", columns)
        self.assertEqual(tags, "")

    def test_out_of_stock_add_returns_available_same_category_options(self):
        result = self.agent._run_tool("add_item_to_order", {"item_name": "Grilled Salmon", "qty": 3})
        self.assertFalse(result["ok"])
        self.assertTrue("out of stock" in result["error"].lower() or "only 2" in result["error"].lower())
        alternatives = {item["name"] for item in result["alternatives"]}
        self.assertTrue(alternatives)
        self.assertNotIn("Grilled Salmon", alternatives)

    def test_menu_and_allergen_answers_are_database_grounded(self):
        drinks = self.agent.handle_user("What drinks do you have?")
        self.assertIn("Mango Smoothie", drinks)
        self.assertNotIn("Soft Drinks", drinks)
        allergen = self.agent.handle_user("Does the pesto penne contain nuts?")
        self.assertIn("nuts is listed", allergen.lower())
        self.assertNotIn("not listed", allergen.lower())

    def test_combo_side_and_drink_selection_affects_price_and_inventory(self):
        result = db.add_combo_to_order(
            self.session_id, "Bistro Burger Meal", side_name="Garden Salad", drink_name="Mango Smoothie"
        )
        self.assertTrue(result["ok"], result)
        item = result["order"]["items"][0]
        self.assertEqual((item["side_name"], item["drink_name"]), ("Garden Salad", "Mango Smoothie"))
        self.assertEqual(item["unit_price"], 350)
        with db.conn() as connection:
            reserved = db._reserved_components(connection)
        self.assertEqual(reserved[9], 1)
        self.assertEqual(reserved[11], 1)
        self.assertNotIn(8, reserved)
        receipt = db.confirm_order(self.session_id)["receipt"]
        self.assertEqual(receipt["items"][0]["line_total"], 350)
        with db.conn() as connection:
            stock = {row["name"]: row["stock_qty"] for row in
                     connection.execute("SELECT name,stock_qty FROM menu_items")}
        self.assertEqual(stock["Garden Salad"], 15)
        self.assertEqual(stock["Mango Smoothie"], 19)
        self.assertEqual(stock["French Fries"], 30)
        self.assertEqual(stock["Iced Tea"], 40)

    def test_combo_choices_create_separate_lines_and_reject_wrong_categories(self):
        first = db.add_combo_to_order(self.session_id, "Bistro Burger Meal",
                                      side_name="Garden Salad", drink_name="Bottled Water")
        second = db.add_combo_to_order(self.session_id, "Bistro Burger Meal",
                                       side_name="French Fries", drink_name="Iced Tea")
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(second["order"]["items"]), 2)
        invalid = db.add_combo_to_order(self.session_id, "Bistro Burger Meal",
                                        side_name="Mango Smoothie", drink_name="Iced Tea")
        self.assertFalse(invalid["ok"])
        self.assertIn("not a sides option", invalid["error"])

    def test_combo_selection_allergens_include_selected_drink(self):
        result = db.add_combo_to_order(self.session_id, "Veggie Burger Meal",
                                       side_name="Garden Salad", drink_name="Mango Smoothie")
        self.assertTrue(result["ok"], result)
        line = result["order"]["items"][0]
        allergens = db.get_order_item_allergens(line["line_id"])
        self.assertIn("dairy", allergens["allergens"].split(","))
        self.assertIn("gluten", allergens["allergens"].split(","))

    def test_combo_upgrade_keeps_customization_and_selected_meal_options(self):
        added = db.add_item_to_order(self.session_id, "Cheeseburger", 1, "no onions")
        line_id = added["order"]["items"][0]["line_id"]
        result = db.upgrade_to_combo(
            self.session_id, line_id, "Bistro Burger Meal",
            side_name="Garden Salad", drink_name="Mango Smoothie",
        )
        self.assertTrue(result["ok"], result)
        item = result["order"]["items"][0]
        self.assertEqual(item["instructions"], "no onions")
        self.assertEqual((item["side_name"], item["drink_name"]), ("Garden Salad", "Mango Smoothie"))
        self.assertEqual(item["unit_price"], 350)

    def test_accepting_chat_combo_offer_prompts_for_and_applies_choices(self):
        added = db.add_item_to_order(self.session_id, "Cheeseburger", 1, "no onions")
        line_id = added["order"]["items"][0]["line_id"]
        recommendation = next(
            item for item in db.get_combo_recommendations(self.session_id)["recommendations"]
            if item["line_id"] == line_id
        )
        self.agent.pending_combo_offer = {
            "line_id": line_id, "combo_name": recommendation["combo_name"],
            "combo_id": recommendation["combo_id"],
        }

        prompt = self.agent.handle_user("Yes, upgrade it")
        self.assertIn("Which side and drink", prompt)
        self.assertEqual(db.view_current_order(self.session_id)["items"][0]["name"], "Cheeseburger")

        drink_prompt = self.agent.handle_user("Garden Salad")
        self.assertIn("drink", drink_prompt.lower())
        unchanged = db.view_current_order(self.session_id)["items"][0]
        self.assertEqual(unchanged["name"], "Cheeseburger")
        self.assertIsNone(unchanged.get("side_name"))

        upgraded = self.agent.handle_user("Bottled Water")
        self.assertIn("tab has been updated", upgraded)
        item = db.view_current_order(self.session_id)["items"][0]
        self.assertEqual(item["name"], "Bistro Burger Meal")
        self.assertEqual((item["side_name"], item["drink_name"]), ("Garden Salad", "Bottled Water"))
        self.assertEqual(item["instructions"], "no onions")
        self.assertIsNone(self.agent.pending_combo_upgrade)

    def test_acceptance_with_both_meal_choices_upgrades_immediately(self):
        added = db.add_item_to_order(self.session_id, "Chicken Inasal", 1)
        line_id = added["order"]["items"][0]["line_id"]
        recommendation = next(
            item for item in db.get_combo_recommendations(self.session_id)["recommendations"]
            if item["line_id"] == line_id
        )
        self.agent.pending_combo_offer = {
            "line_id": line_id, "combo_name": recommendation["combo_name"],
            "combo_id": recommendation["combo_id"],
        }
        reply = self.agent.handle_user("Yes, Garden Salad and Bottled Water")
        self.assertIn("tab has been updated", reply)
        item = db.view_current_order(self.session_id)["items"][0]
        self.assertEqual(item["name"], "Chicken Inasal Meal")
        self.assertEqual((item["side_name"], item["drink_name"]), ("Garden Salad", "Bottled Water"))

    def test_quantity_edit_and_ambiguous_customized_removal(self):
        first = db.add_item_to_order(self.session_id, "Cheeseburger", 1, "no onions")
        second = db.add_item_to_order(self.session_id, "Cheeseburger", 1, "extra cheese")
        lines = db.view_current_order(self.session_id)["items"]
        ambiguous = db.remove_item_from_order(self.session_id, "Cheeseburger", 1)
        self.assertTrue(ambiguous["clarification_required"])
        updated = db.set_line_quantity(self.session_id, lines[0]["line_id"], 2)
        self.assertTrue(updated["ok"])
        items = db.view_current_order(self.session_id)["items"]
        self.assertEqual(items[0]["quantity"], 2)
        self.assertEqual(items[1]["instructions"], "extra cheese")

    def test_cancellation_requires_confirmation_and_releases_stock(self):
        self.agent._run_tool("add_item_to_order", {"item_name": "Grilled Salmon", "qty": 1})
        reply = self.agent.handle_user("Cancel my whole order")
        self.assertIn("Reply yes", reply)
        waiting = fsm_agent.WaiterAgent()
        self.assertEqual(waiting.pending_confirmation, "cancel_order")
        self.assertEqual(len(db.view_current_order(self.session_id)["items"]), 1)
        cancelled = waiting.handle_user("yes")
        self.assertIn("order has been cancelled", cancelled)
        self.assertIsNone(waiting.session_id)
        with db.conn() as connection:
            status = connection.execute("SELECT status FROM table_sessions WHERE session_id=?",
                                        (self.session_id,)).fetchone()[0]
        self.assertEqual(status, "cancelled")
        self.assertEqual(db.get_menu_by_category("Mains")["items"][-1]["available_qty"], 2)

    def test_non_yes_no_interrupt_does_not_cancel_order(self):
        self.agent._run_tool("add_item_to_order", {"item_name": "Cheeseburger", "qty": 1})
        self.agent.handle_user("cancel my order")
        response = self.agent.handle_user("What drinks do you have?")
        self.assertIn("Iced Tea", response)
        self.assertNotIn("Soft Drinks", response)
        self.assertEqual(len(db.view_current_order(self.session_id)["items"]), 1)

    def test_db_mutating_functions_require_active_open_session(self):
        res1 = db.add_item_to_order(None, "Cheeseburger", 1)
        self.assertFalse(res1["ok"])
        self.assertIn("No active table session", res1["error"])

        res2 = db.add_combo_to_order(None, "Bistro Burger Meal", "French Fries", "Iced Tea", 1)
        self.assertFalse(res2["ok"])
        self.assertIn("No active table session", res2["error"])

        res3 = db.remove_item_from_order(None, "Cheeseburger")
        self.assertFalse(res3["ok"])
        self.assertIn("No active table session", res3["error"])

        res4 = db.confirm_order(None)
        self.assertFalse(res4["ok"])
        self.assertIn("No active table session", res4["error"])

    def test_db_confirm_twice_rejected(self):
        db.add_item_to_order(self.session_id, "Cheeseburger", 1)
        first = db.confirm_order(self.session_id)
        self.assertTrue(first["ok"])
        with db.conn() as c:
            stock_after_first = c.execute("SELECT stock_qty FROM menu_items WHERE name='Cheeseburger'").fetchone()[0]
        second = db.confirm_order(self.session_id)
        self.assertFalse(second["ok"])
        with db.conn() as c:
            stock_after_second = c.execute("SELECT stock_qty FROM menu_items WHERE name='Cheeseburger'").fetchone()[0]
        self.assertEqual(stock_after_first, stock_after_second)

    def test_agent_mutating_tool_without_session_returns_error(self):
        agent = fsm_agent.WaiterAgent()
        agent.reset()
        res = agent._run_tool("add_item_to_order", {"item_name": "Cheeseburger", "qty": 1})
        self.assertFalse(res["ok"])
        self.assertIn("Please provide a table number first", res["error"])

    def test_greeting_catalog_query_answers_before_table_no(self):
        agent = fsm_agent.WaiterAgent()
        agent.reset()
        reply = agent.handle_user("What drinks do you have?")
        self.assertIn("Mango Smoothie", reply)
        self.assertIn("Which table number", reply)
        self.assertEqual(agent.state, fsm_agent.GREETING)

    def test_is_menu_browse_request_standalone_menu(self):
        for phrase in ("menu", "Menu", "MENU", "menu please", "menu!"):
            with self.subTest(phrase=phrase):
                self.assertTrue(fsm_agent.is_menu_browse_request(phrase))

    def test_adjust_line_during_review_transitions_and_notifies(self):
        self.agent._run_tool("add_item_to_order", {"item_name": "Cheeseburger", "qty": 1})
        self.agent.go_review()
        self.assertEqual(self.agent.state, fsm_agent.REVIEW)
        line_id = db.view_current_order(self.session_id)["items"][0]["line_id"]
        res = self.agent.adjust(line_id, 1)
        self.assertTrue(res["ok"])
        self.assertEqual(self.agent.state, fsm_agent.TAKING_ORDER)
        self.assertIn("Your order was adjusted", self.agent.transcript[-1]["text"])


if __name__ == "__main__":
    unittest.main()
