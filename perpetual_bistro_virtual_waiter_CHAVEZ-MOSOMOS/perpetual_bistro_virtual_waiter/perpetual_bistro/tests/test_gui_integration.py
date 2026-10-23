import os
import tempfile
import time
import tkinter as tk
import unittest

import db_tools as db
import fsm_agent
from virtual_waiter_prototype import WaiterApp


class GuiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.temp.name, "restaurant.db")
        fsm_agent.LOG_PATH = os.path.join(self.temp.name, "conversation.json")
        db.init_db()
        self.agent = fsm_agent.WaiterAgent()
        self.agent.reset()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = WaiterApp(self.root, self.agent)
        self.root.update()

    def tearDown(self):
        self._flush(timeout=3.0)
        self.app.wait_for_idle(timeout=2.0)
        self.app.cleanup()
        try:
            self.root.destroy()
        except Exception:
            pass
        time.sleep(0.05)
        try:
            self.temp.cleanup()
        except Exception:
            pass

    def _flush(self, timeout=5.0):
        start = time.time()
        while time.time() - start < timeout:
            try:
                self.root.update()
            except Exception:
                break
            while not self.app.completed_actions.empty():
                self.app._poll_actions()
                try:
                    self.root.update()
                except Exception:
                    break
            if not self.app.busy and self.app.completed_actions.empty():
                break
            time.sleep(0.02)
        try:
            self.root.update()
        except Exception:
            pass

    def test_menu_window_opens_and_stays_synced_with_table(self):
        # Open menu window
        menu_win = self.app._show_menu()
        self.root.update()
        self.assertIsNotNone(menu_win)
        self.assertTrue(menu_win.winfo_exists())
        self.assertIn("chat window to start ordering", self.app.menu_notice.cget("text"))

        # Customer provides table number in main chat
        self.agent.handle_user("Table 5")
        self.app.render()
        self.root.update()
        self.assertIn("Connected to Table 5", self.app.menu_notice.cget("text"))

    def test_adding_item_from_menu_gui_updates_tab_and_chat(self):
        self.agent.handle_user("Table 5")
        self.app.render()
        menu_win = self.app._show_menu()
        self.root.update()

        # Add Cheeseburger via Menu GUI
        self.app._add_menu_item("Cheeseburger")
        self._flush()

        # Verify menu window stays open
        self.assertTrue(menu_win.winfo_exists())

        # Verify cart in Main GUI updated
        snapshot = self.agent.snapshot()
        items = snapshot["order"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Cheeseburger")

        # Verify chat transcript contains waiter acknowledgment with allergens
        latest_transcript = self.agent.transcript[-1]["text"]
        self.assertIn("Added 1 x Cheeseburger", latest_transcript)
        self.assertIn("Allergen information: dairy,egg,gluten", latest_transcript)

    def test_adding_combo_from_menu_gui_updates_tab_and_chat(self):
        self.agent.handle_user("Table 5")
        self.app.render()
        menu_win = self.app._show_menu()
        self.root.update()

        self.app._add_combo("Bistro Burger Meal", "French Fries", "Iced Tea")
        self._flush()

        self.assertTrue(menu_win.winfo_exists())
        items = self.agent.snapshot()["order"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Bistro Burger Meal")
        self.assertEqual(items[0]["side_name"], "French Fries")
        self.assertEqual(items[0]["drink_name"], "Iced Tea")
        self.assertIn("Added 1 x Bistro Burger Meal", self.agent.transcript[-1]["text"])

    def test_menu_gui_updates_stock_dynamically(self):
        self.agent.handle_user("Table 5")
        self.app.render()
        self.app._show_menu()
        self.root.update()

        # Grilled Salmon starts with 2 portions available
        self.app._add_menu_item("Grilled Salmon")
        self._flush()

        # The menu window should now reflect 1 portion left
        mains = [i for i in db.get_menu_by_category("Mains")["items"] if i["name"] == "Grilled Salmon"]
        self.assertEqual(mains[0]["available_qty"], 1)

        # Add second portion
        self.app._add_menu_item("Grilled Salmon")
        self._flush()

        mains = [i for i in db.get_menu_by_category("Mains")["items"] if i["name"] == "Grilled Salmon"]
        self.assertEqual(mains[0]["available_qty"], 0)

    def test_adding_item_in_review_transitions_fsm_to_taking_order(self):
        self.agent.handle_user("Table 5")
        self.agent._run_tool("add_item_to_order", {"item_name": "Cheeseburger", "qty": 1})
        self.agent.go_review()
        self.assertEqual(self.agent.state, fsm_agent.REVIEW)
        self.app.render()

        self.app._add_menu_item("French Fries")
        self._flush()

        self.assertEqual(self.agent.state, fsm_agent.TAKING_ORDER)

    def test_menu_filter_sync_from_chat_submission(self):
        self.agent.handle_user("Table 5")
        self.app.render()
        self.app._show_menu()
        self.root.update()

        # Submit dietary filter in composer
        self.app.message.insert(0, "Can I see the vegetarian options?")
        self.app._submit()
        self._flush()

        self.assertEqual(self.app.active_menu_filter, "Vegetarian")
        self.assertEqual(self.app.menu_filter.get(), "Vegetarian")

        # Submit full menu browse in composer
        self.app.message.insert(0, "Show me the full menu please")
        self.app._submit()
        self._flush()

        self.assertEqual(self.app.active_menu_filter, "All items")
        self.assertEqual(self.app.menu_filter.get(), "All items")

    def test_activity_window_single_instance(self):
        act1 = self.app._show_activity()
        self.root.update()
        act2 = self.app._show_activity()
        self.root.update()
        self.assertIs(act1, act2)


if __name__ == "__main__":
    unittest.main()
