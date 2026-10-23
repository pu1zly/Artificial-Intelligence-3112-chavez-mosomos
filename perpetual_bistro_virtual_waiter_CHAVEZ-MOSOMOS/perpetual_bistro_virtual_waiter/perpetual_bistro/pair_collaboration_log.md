# Pair Collaboration Log - Lab 6.1

**Pair:** Student A Chavez, Floyd & Student B Mosomos, Jairus  |  **Dates:** 10/23/2026

| Session | Driver | Navigator | Task | Notes / decisions |
|---|---|---|---|---|
| 1 (first half) | Chavez, Floyd | Mosomos, Jairus | `init_restaurant_db.sql`, `db_tools.py` | Implemented SQLite schema with foreign keys, stock validation, and fuzzy item matching using difflib to prevent LLM hallucinations. |
| 1 (first half) | Chavez, Floyd | Mosomos, Jairus | `fsm_agent.py` tool schemas + FSM states | Defined strict function-calling JSON schemas; ensured `session_id` is securely injected in code rather than hallucinated by the model. |
| 2 (second half) | Mosomos, Jairus | Chavez, Floyd | GUI & CLI (`virtual_waiter_prototype.py`) | Developed responsive Tkinter UI mirroring table-top kiosk specifications (live synchronized SQLite cart, badges, and async agent thread). |
| 2 (second half) | Mosomos, Jairus | Chavez, Floyd | Edge-case testing & Conversation Logging | Validated inventory guardrails, allergen filtering, combo substitutions, and generated `sample_conversation_log.json`. |

## Key design decisions
1. Stock is only deducted at `STATE_CONFIRMED`, inside one SQLite transaction.
2. The live tab is re-injected into the system prompt every turn (memory + anti-hallucination).
3. Dietary filters use explicit menu tags, allergen answers and prices are grounded in SQLite, and uncertain
   customized-line removals ask for clarification instead of guessing.
4. Out-of-stock substitutions use live same-category inventory; cancelling a full tab requires a separate yes/no
   confirmation so interruptions cannot discard an order.

## What each person learned
- Chavez, Floyd: Learned how to ground local LLM tool-calling in strict relational database transactions. Designing the FSM showed me the importance of keeping state transitions deterministic in Python rather than relying purely on prompt steering, especially when synchronizing cart inventory and preventing hallucinations during item selection.
- Mosomos, Jairus: Learned how to bridge asynchronous AI agent loops with real-time UI/CLI event loops. Handling edge cases like out-of-stock items, customized instruction badges, and user confirmation rollbacks gave me a clear understanding of practical human-in-the-loop validation in conversational AI systems.
