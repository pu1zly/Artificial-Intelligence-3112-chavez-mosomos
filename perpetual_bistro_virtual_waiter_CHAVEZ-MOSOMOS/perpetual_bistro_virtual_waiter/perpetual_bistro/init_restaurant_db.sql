-- Perpetual Bistro schema + seed data (safe to re-run)
CREATE TABLE IF NOT EXISTS menu_items (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, category TEXT NOT NULL,
    price REAL NOT NULL, stock_qty INTEGER NOT NULL DEFAULT 0, allergens TEXT NOT NULL DEFAULT '',
    dietary_tags TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS table_sessions (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT, table_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'   -- open | confirmed | cancelled
);
CREATE TABLE IF NOT EXISTS order_line_items (
    line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES table_sessions(session_id),
    item_id INTEGER NOT NULL REFERENCES menu_items(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    special_instructions TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS combo_components (
    combo_item_id INTEGER NOT NULL REFERENCES menu_items(id),
    component_item_id INTEGER NOT NULL REFERENCES menu_items(id),
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    PRIMARY KEY (combo_item_id, component_item_id),
    CHECK (combo_item_id != component_item_id)
);
CREATE TABLE IF NOT EXISTS order_line_combo_choices (
    line_id INTEGER PRIMARY KEY REFERENCES order_line_items(line_id) ON DELETE CASCADE,
    side_item_id INTEGER NOT NULL REFERENCES menu_items(id),
    drink_item_id INTEGER NOT NULL REFERENCES menu_items(id)
);
CREATE TABLE IF NOT EXISTS kiosk_state (   -- single row: lets the kiosk resume after refresh/restart
    id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL
);
INSERT OR IGNORE INTO menu_items (id,name,category,price,stock_qty,allergens) VALUES
 (1,'Cheeseburger','Mains',180,20,'gluten,dairy,egg'),
 (2,'Chicken Inasal','Mains',210,15,'soy'),
 (3,'Veggie Burger','Mains',170,12,'gluten,soy'),
 (4,'Grilled Salmon','Mains',320,2,'fish'),
 (5,'Spaghetti Bolognese','Pasta',220,18,'gluten,egg'),
 (6,'Pesto Penne','Pasta',200,10,'gluten,nuts,dairy'),
 (7,'Mac and Cheese','Pasta',190,14,'gluten,dairy'),
 (8,'French Fries','Sides',90,30,''),
 (9,'Garden Salad','Sides',110,16,''),
 (10,'Iced Tea','Drinks',50,40,''),
 (11,'Mango Smoothie','Drinks',90,20,'dairy'),
 (12,'Bottled Water','Drinks',30,50,''),
 (13,'Chocolate Cake','Desserts',130,8,'gluten,dairy,egg'),
 (14,'Halo-Halo','Desserts',120,10,'dairy'),
 (101,'Bistro Burger Meal','Combos',290,0,'gluten,dairy,egg'),
 (102,'Chicken Inasal Meal','Combos',320,0,'soy'),
 (103,'Veggie Burger Meal','Combos',260,0,'gluten,soy'),
 (104,'Grilled Salmon Meal','Combos',390,0,'fish');
INSERT OR IGNORE INTO combo_components (combo_item_id,component_item_id,quantity) VALUES
 (101,1,1),(101,8,1),(101,10,1),
 (102,2,1),(102,8,1),(102,10,1),
 (103,3,1),(103,8,1),(103,10,1),
 (104,4,1),(104,8,1),(104,10,1);
